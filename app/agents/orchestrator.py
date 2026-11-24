"""AGENT ORCHESTRATOR (spec s98, s99).

Sequences the agents and -- critically -- knows when to STOP and ask a human.

    Input -> Document -> Extraction -> Validation -> Classification
          -> Entitlement -> Matching -> Reconciliation -> Verification
          -> Anomaly -> Explanation -> Human Review -> Final Ledger

The confidence gate (spec s99)
------------------------------
    strong evidence + high confidence  -> AUTO-PROCESS (confirmed allocation)
    medium confidence                  -> SUGGEST + REVIEW (unconfirmed)
    low confidence                     -> DO NOT AUTO-POST (leave unallocated)
    conflicting financial evidence     -> STOP + REQUEST REVIEW

Nothing here writes a financial fact that a person cannot see, trace and undo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..config import settings
from ..enums import (
    AllocationMethod,
    AllocationStatus,
    AnomalyCode,
    Direction,
    JobStatus,
    PaymentClassification,
    RecommendationStatus,
    Severity,
    TransactionStatus,
)
from ..models import (
    AIJob,
    AIRecommendation,
    BankTransaction,
    Employee,
    SalaryPeriod,
)
from . import (
    allocation_agent,
    anomaly_agent,
    classification_agent,
    entitlement_agent,
    month_matching_agent,
    reconciliation_agent,
)

AGENT_NAME = "orchestrator"


@dataclass
class TransactionOutcome:
    transaction: BankTransaction
    classification: str = ""
    confidence: float = 0.0
    allocations_created: int = 0
    auto_confirmed: bool = False
    needs_review: bool = False
    review_reason: str = ""
    unallocated_paise: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class ProcessSummary:
    """What the one-click monthly process actually did (spec s102)."""

    processed: int = 0
    reconciled: int = 0
    review_required: int = 0
    short_payments: int = 0
    excess_payments: int = 0
    duplicates_suspected: int = 0
    unallocated_transactions: int = 0
    anomalies_created: int = 0
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single transaction pipeline
# ---------------------------------------------------------------------------
def process_transaction(
    db: Session,
    txn: BankTransaction,
    employee: Optional[Employee] = None,
    actor_id: Optional[int] = None,
) -> TransactionOutcome:
    """Classify, month-match and allocate one bank transaction."""
    employee = employee or txn.employee
    outcome = TransactionOutcome(transaction=txn)

    # --- Debits: reversal linkage, then stop. A debit is not income. ---
    if txn.direction == Direction.DEBIT.value:
        classification_agent.apply(db, txn, employee)
        original = allocation_agent.link_reversal(db, txn)
        if original:
            outcome.notes.append(
                f"Linked as a reversal of the credit received on {original.txn_date.isoformat()}."
            )
        outcome.classification = txn.classification
        return outcome

    if employee is None:
        txn.status = TransactionStatus.REVIEW_REQUIRED.value
        outcome.needs_review = True
        outcome.review_reason = (
            "This transaction is not linked to an employee, so it cannot be reconciled."
        )
        return outcome

    # --- 1. Classification ---
    classification = classification_agent.apply(db, txn, employee)
    outcome.classification = classification.classification
    outcome.confidence = classification.confidence

    if txn.is_reversed:
        outcome.notes.append("This credit was later reversed; it is not counted as received.")
        return outcome

    # Non-income credits (refunds, scholarships, benefits) are recorded and
    # classified, but never allocated to a salary month.
    from ..enums import INCOME_CLASSIFICATIONS

    if classification.classification not in INCOME_CLASSIFICATIONS:
        txn.status = TransactionStatus.UNALLOCATED.value
        outcome.unallocated_paise = txn.amount_paise
        if classification.classification == PaymentClassification.UNKNOWN.value:
            outcome.needs_review = True
            outcome.review_reason = (
                "It is not clear what this credit is for, so it has not been counted "
                "as salary. Please confirm what it relates to."
            )
            _recommend(db, txn, employee,
                       kind="CLASSIFY_TRANSACTION",
                       title="Confirm what this credit is for",
                       detail=outcome.review_reason,
                       confidence=classification.confidence)
        return outcome

    # --- 2. Salary month matching ---
    match = month_matching_agent.match(db, txn, employee)
    month_matching_agent.store_candidates(db, txn, match)

    if not match.candidates:
        txn.status = TransactionStatus.UNALLOCATED.value
        outcome.unallocated_paise = txn.amount_paise
        outcome.needs_review = True
        outcome.review_reason = match.note or "The salary month for this credit could not be determined."
        _recommend(db, txn, employee,
                   kind="ASSIGN_MONTH",
                   title="Confirm which salary month this payment belongs to",
                   detail=outcome.review_reason, confidence=0.0)
        return outcome

    allocation_agent.clear_auto_allocations(db, txn)

    # --- 3. Multi-month split (spec s10) ---
    if match.multi_period:
        outcome.notes.append(
            "This credit appears to cover more than one month; a split has been proposed."
        )
        for period, amount, reason in match.multi_period:
            headroom = allocation_agent.remaining_paise(db, txn)
            if headroom <= 0:
                break
            amount = min(amount, headroom)
            if amount <= 0:
                continue
            score = allocation_agent.score_allocation(
                db, txn, employee, period,
                month_probability=next((c.probability for c in match.candidates
                                        if c.period.id == period.id), 0.0),
                month_explicit=match.explicit,
            )
            kind = (PaymentClassification.ARREAR.value
                    if classification.classification == PaymentClassification.ARREAR.value
                    else PaymentClassification.SALARY.value)
            allocation_agent.allocate(
                db, txn, employee, period, amount,
                kind=kind, method=AllocationMethod.AUTO.value,
                status=AllocationStatus.SUGGESTED.value,
                score=score, confidence=classification.confidence,
                created_by_id=actor_id, reasoning=reason,
            )
            outcome.allocations_created += 1

        outcome.needs_review = True
        outcome.review_reason = (
            "A multi-month split has been proposed. Please confirm the amounts before "
            "they are treated as final."
        )
        outcome.unallocated_paise = allocation_agent.remaining_paise(db, txn)
        return outcome

    # --- 4. Single-month allocation, subject to the confidence gate ---
    best = match.best
    score = allocation_agent.score_allocation(
        db, txn, employee, best.period,
        month_probability=best.probability, month_explicit=match.explicit,
    )
    kind = (PaymentClassification.ARREAR.value
            if classification.classification == PaymentClassification.ARREAR.value
            else PaymentClassification.SALARY.value)

    # The gate governs confidence in the MONTH, not agreement on the amount.
    # When the narration names the month outright, the credit is recorded
    # against that month even if the amount differs -- a short or excess
    # payment is precisely the finding this system exists to surface, and
    # withholding the allocation would hide it as "not paid".
    weak_month_evidence = (not match.confident) or (
        not match.explicit and score.total < settings.review_allocate_score
    )
    if weak_month_evidence:
        # DO NOT AUTO-POST. Record the suggestion, leave the money unallocated
        # and visible, and ask a person (spec s9, s70, s99).
        txn.status = TransactionStatus.REVIEW_REQUIRED.value
        outcome.unallocated_paise = txn.amount_paise
        outcome.needs_review = True
        outcome.review_reason = match.note or (
            f"The evidence for assigning this credit to {best.period.label} is weak "
            f"(match score {score.total}/100)."
        )
        _recommend(
            db, txn, employee,
            kind="ASSIGN_MONTH",
            title=f"Confirm the salary month for {txn.txn_date.isoformat()} credit",
            detail=outcome.review_reason,
            confidence=best.probability,
            proposed={"period_id": best.period.id, "period": best.period.label,
                      "amount_paise": txn.amount_paise, "match_score": score.total,
                      "alternatives": [
                          {"period": c.period.label, "period_id": c.period.id,
                           "probability": c.probability, "reasons": c.reasons}
                          for c in match.candidates[:4]
                      ]},
        )
        anomaly_agent.raise_anomaly(
            db, employee.organization_id,
            code=AnomalyCode.UNCERTAIN_MONTH_MAPPING.value,
            severity=Severity.MEDIUM.value,
            entity_type="BankTransaction", entity_id=txn.id,
            employee_id=employee.id, period_id=best.period.id,
            title_en="Salary month for this payment is not confirmed",
            title_hi="इस भुगतान का वेतन माह पुष्ट नहीं है",
            detail_en=outcome.review_reason,
            detail_hi="इस भुगतान का वेतन माह उपलब्ध प्रमाणों से निश्चित नहीं किया जा सका।",
            amount_paise=txn.amount_paise,
        )
        return outcome

    auto_confirm = (
        score.total >= settings.auto_allocate_score
        and match.explicit
        and classification.confidence >= 0.6
    )
    allocation_agent.allocate(
        db, txn, employee, best.period, txn.amount_paise,
        kind=kind,
        method=AllocationMethod.AUTO.value,
        status=(AllocationStatus.CONFIRMED.value if auto_confirm
                else AllocationStatus.SUGGESTED.value),
        score=score,
        confidence=min(classification.confidence, best.probability),
        created_by_id=actor_id,
    )
    outcome.allocations_created = 1
    outcome.auto_confirmed = auto_confirm
    outcome.needs_review = not auto_confirm
    if not auto_confirm:
        outcome.review_reason = (
            f"Suggested for {best.period.label} with a match score of {score.total}/100. "
            "Confirm to make it final."
        )

    # --- 5. Duplicate check (flag only, never delete) ---
    duplicates = allocation_agent.find_possible_duplicates(db, txn)
    if duplicates:
        top = duplicates[0]
        anomaly_agent.raise_anomaly(
            db, employee.organization_id,
            code=AnomalyCode.POSSIBLE_DUPLICATE.value,
            severity=Severity.HIGH.value,
            entity_type="BankTransaction", entity_id=txn.id,
            employee_id=employee.id,
            title_en="Possible duplicate payment",
            title_hi="संभावित दोहरा भुगतान",
            detail_en=(
                f"This credit of {txn.amount_paise / 100:.2f} on {txn.txn_date.isoformat()} "
                f"resembles another credit on {top.other.txn_date.isoformat()}. "
                f"Reasons: {'; '.join(top.reasons)}. "
                "Nothing has been removed -- please confirm whether this is a genuine duplicate."
            ),
            detail_hi=(
                "यह जमा एक अन्य जमा से मिलती-जुलती है। कोई रिकॉर्ड हटाया नहीं गया है — "
                "कृपया जाँच कर पुष्टि करें।"
            ),
            amount_paise=txn.amount_paise,
        )
        outcome.notes.append("A possible duplicate payment was flagged for review.")

    # --- 6. Settle arrears when this was an arrear payment ---
    if kind == PaymentClassification.ARREAR.value:
        for allocation in txn.allocations:
            if allocation.kind == PaymentClassification.ARREAR.value:
                allocation_agent.settle_arrears(db, allocation)

    outcome.unallocated_paise = allocation_agent.remaining_paise(db, txn)
    return outcome


def _recommend(db: Session, txn: BankTransaction, employee: Employee, *,
               kind: str, title: str, detail: str, confidence: float,
               proposed: Optional[dict] = None) -> AIRecommendation:
    """Queue a suggestion for human approval, de-duplicated per entity+kind."""
    existing = (
        db.query(AIRecommendation)
        .filter(AIRecommendation.entity_type == "BankTransaction",
                AIRecommendation.entity_id == txn.id,
                AIRecommendation.kind == kind,
                AIRecommendation.status == RecommendationStatus.PENDING.value)
        .first()
    )
    if existing:
        existing.title_en = title
        existing.detail = detail
        existing.confidence = confidence
        existing.proposed_json = proposed or {}
        db.flush()
        return existing

    rec = AIRecommendation(
        organization_id=employee.organization_id,
        employee_id=employee.id,
        entity_type="BankTransaction",
        entity_id=txn.id,
        kind=kind,
        title_en=title,
        detail=detail,
        confidence=confidence,
        proposed_json=proposed or {},
        agent=AGENT_NAME,
        status=RecommendationStatus.PENDING.value,
    )
    db.add(rec)
    db.flush()
    return rec


# ---------------------------------------------------------------------------
# Batch pipelines
# ---------------------------------------------------------------------------
def process_employee(db: Session, employee: Employee,
                     actor_id: Optional[int] = None) -> ProcessSummary:
    """Run the full pipeline for one employee: transactions, then periods."""
    summary = ProcessSummary()

    # --- 1. Entitlements FIRST. ---
    # Month matching and match scoring both compare a credit against the
    # expected amount, so the expected amounts have to exist before any
    # transaction is examined. Running this after matching silently starves
    # the matcher of its strongest signal.
    periods = (
        db.query(SalaryPeriod)
        .filter(SalaryPeriod.organization_id == employee.organization_id)
        .all()
    )
    ordered_periods = sorted(periods, key=lambda p: p.sort_key)
    for period in ordered_periods:
        entitlement_agent.calculate_and_store(db, employee, period)

    # --- 2. Transactions, oldest first ---
    transactions = (
        db.query(BankTransaction)
        .filter(BankTransaction.employee_id == employee.id)
        .order_by(BankTransaction.txn_date, BankTransaction.id)
        .all()
    )
    for txn in transactions:
        result = process_transaction(db, txn, employee, actor_id)
        summary.processed += 1
        if result.needs_review:
            summary.review_required += 1
        if result.unallocated_paise > 0 and txn.direction == Direction.CREDIT.value:
            summary.unallocated_transactions += 1

    from ..enums import ReconOutcome, SHORTFALL_OUTCOMES

    for recon in reconciliation_agent.reconcile_employee(db, employee):
        if recon.outcome == ReconOutcome.FULLY_RECONCILED.value:
            summary.reconciled += 1
        if recon.outcome in SHORTFALL_OUTCOMES:
            summary.short_payments += 1
        if recon.outcome == ReconOutcome.EXCESS_CREDIT.value:
            summary.excess_payments += 1

    created = anomaly_agent.scan_employee(db, employee)
    summary.anomalies_created += len(created)
    summary.duplicates_suspected += sum(
        1 for a in created if a.code == AnomalyCode.POSSIBLE_DUPLICATE.value
    )
    return summary


def process_organization(db: Session, organization_id: int, job: Optional[AIJob] = None,
                         actor_id: Optional[int] = None) -> ProcessSummary:
    """One-click processing for a whole organisation (spec s102)."""
    summary = ProcessSummary()
    employees = db.query(Employee).filter(Employee.organization_id == organization_id).all()

    if job is not None:
        job.total = len(employees)
        job.status = JobStatus.PROCESSING.value
        db.flush()

    for index, employee in enumerate(employees, start=1):
        part = process_employee(db, employee, actor_id)
        summary.processed += part.processed
        summary.reconciled += part.reconciled
        summary.review_required += part.review_required
        summary.short_payments += part.short_payments
        summary.excess_payments += part.excess_payments
        summary.duplicates_suspected += part.duplicates_suspected
        summary.unallocated_transactions += part.unallocated_transactions
        summary.anomalies_created += part.anomalies_created

        if job is not None:
            job.progress = index
            job.message = f"Processed {index} of {len(employees)} employees"
            db.flush()

    summary.messages.append(
        f"{summary.processed} records processed, {summary.reconciled} reconciled, "
        f"{summary.review_required} require review, {summary.short_payments} short payment(s), "
        f"{summary.duplicates_suspected} possible duplicate(s)."
    )

    audit_record(
        db,
        action="PROCESS_ORGANIZATION",
        entity_type="Organization",
        entity_id=organization_id,
        organization_id=organization_id,
        new_value={
            "processed": summary.processed,
            "reconciled": summary.reconciled,
            "review_required": summary.review_required,
            "short_payments": summary.short_payments,
        },
        source=AuditSource.agent(AGENT_NAME),
        reason="One-click processing run",
    )
    return summary
