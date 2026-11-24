"""AGENT 8 - Deduction Analysis (spec s12, s31).

Central rule: **a smaller credit is not proof of a legitimate deduction.**

When money is missing, this agent looks for an authorising record. If it finds
one, the deduction is DOCUMENTED. If it does not, the gap is recorded as an
UNEXPLAINED deduction -- never quietly accepted, and never described as fraud
(spec s58).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..enums import (
    ComponentKind,
    DeductionCategory,
    DeductionStatus,
    VerificationLevel,
)
from ..models import (
    AttendanceRecord,
    Deduction,
    Employee,
    Entitlement,
    SalaryPeriod,
)

AGENT_NAME = "deduction_agent"


@dataclass
class DeductionAnalysis:
    documented_paise: int = 0
    expected_paise: int = 0
    unexplained_paise: int = 0
    records: list[Deduction] = field(default_factory=list)
    possible_explanations: list[str] = field(default_factory=list)

    @property
    def total_paise(self) -> int:
        return self.documented_paise + self.expected_paise + self.unexplained_paise


def existing_deductions(db: Session, employee: Employee, period: SalaryPeriod) -> list[Deduction]:
    return (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id, Deduction.period_id == period.id)
        .all()
    )


def sync_rule_deductions(db: Session, employee: Employee, period: SalaryPeriod,
                         entitlement: Optional[Entitlement]) -> list[Deduction]:
    """Mirror the deduction components of the applicable rule into the ledger.

    These are EXPECTED deductions: the rule authorises them, so they are
    explained by definition, and they must not later be counted as a mystery
    shortfall.
    """
    if entitlement is None:
        return []

    created: list[Deduction] = []
    current = {d.rule_reference: d for d in existing_deductions(db, employee, period)
               if d.detected_by == "RULE"}

    for component in entitlement.components:
        if component.kind != ComponentKind.DEDUCTION.value or component.amount_paise <= 0:
            continue
        key = f"{entitlement.rule.label if entitlement.rule else 'rule'}:{component.code}"
        record = current.get(key)
        if record is None:
            record = Deduction(
                employee_id=employee.id,
                period_id=period.id,
                category=_category_for(component.code),
                amount_paise=component.amount_paise,
                reason=component.label_en,
                rule_reference=key,
                authority=component.source_reference,
                status=DeductionStatus.EXPECTED.value,
                detected_by="RULE",
                verification_level=(
                    VerificationLevel.OFFICIAL_DOCUMENT if component.source_reference
                    else VerificationLevel.USER_CLAIM
                ),
                notes="Authorised by the applicable salary rule.",
            )
            db.add(record)
            created.append(record)
        else:
            record.amount_paise = component.amount_paise
            record.reason = component.label_en

    db.flush()
    return created


def _category_for(code: str) -> str:
    lookup = {
        "PF": DeductionCategory.PF.value,
        "EPF": DeductionCategory.PF.value,
        "ESI": DeductionCategory.ESI.value,
        "TDS": DeductionCategory.TDS.value,
        "PT": DeductionCategory.PROFESSIONAL_TAX.value,
        "ADVANCE": DeductionCategory.ADVANCE_RECOVERY.value,
        "RECOVERY": DeductionCategory.OVERPAYMENT_RECOVERY.value,
        "LOAN": DeductionCategory.LOAN_RECOVERY.value,
        "ABSENCE": DeductionCategory.ABSENCE_DEDUCTION.value,
        "INSURANCE": DeductionCategory.INSURANCE.value,
    }
    return lookup.get((code or "").upper(), DeductionCategory.OTHER_AUTHORISED.value)


def analyse_gap(
    db: Session,
    employee: Employee,
    period: SalaryPeriod,
    entitlement: Optional[Entitlement],
    credited_paise: int,
    period_due: bool = True,
) -> DeductionAnalysis:
    """Explain (or refuse to explain) the gap between expected and received.

    Returns the split between deductions that are documented, deductions the
    rule expects, and money that is missing with no authorisation on record.

    ``period_due`` guards against the commonest false positive: a month whose
    payment date has not yet arrived has nothing missing, so no unexplained
    deduction may be raised against it.
    """
    analysis = DeductionAnalysis()
    records = existing_deductions(db, employee, period)
    analysis.records = records

    for record in records:
        if record.status == DeductionStatus.DOCUMENTED.value:
            analysis.documented_paise += record.amount_paise
        elif record.status == DeductionStatus.EXPECTED.value:
            analysis.expected_paise += record.amount_paise
        elif record.status == DeductionStatus.UNEXPLAINED.value:
            analysis.unexplained_paise += record.amount_paise

    if entitlement is None:
        return analysis

    # The gap that remains once every authorised deduction is accounted for.
    accounted = analysis.documented_paise + analysis.expected_paise
    gap = entitlement.net_paise - credited_paise - 0
    unexplained_gap = gap - analysis.documented_paise

    # A month where nothing at all was credited is a MISSING PAYMENT, not a
    # deduction. A deduction means money was paid but reduced; recording an
    # unpaid month as a deduction would double-count it against the arrear
    # ledger and misdescribe what happened.
    nothing_received = credited_paise <= settings.match_tolerance_paise

    if unexplained_gap <= settings.match_tolerance_paise or not period_due or nothing_received:
        # Nothing unexplained: retire any stale unexplained record.
        for record in records:
            if record.status == DeductionStatus.UNEXPLAINED.value and record.detected_by == "RECONCILIATION_GAP":
                db.delete(record)
        db.flush()
        analysis.unexplained_paise = 0
        analysis.records = [r for r in records if r.status != DeductionStatus.UNEXPLAINED.value
                            or r.detected_by != "RECONCILIATION_GAP"]
        return analysis

    # --- Look for a plausible (but unproven) explanation, and say so ---
    analysis.possible_explanations = _possible_explanations(db, employee, period, unexplained_gap)

    auto = next((r for r in records
                 if r.detected_by == "RECONCILIATION_GAP"
                 and r.status == DeductionStatus.UNEXPLAINED.value), None)
    if auto is None:
        auto = Deduction(
            employee_id=employee.id,
            period_id=period.id,
            category=DeductionCategory.UNKNOWN.value,
            amount_paise=unexplained_gap,
            reason="Amount received is less than the expected amount, with no authorising record found.",
            status=DeductionStatus.UNEXPLAINED.value,
            detected_by="RECONCILIATION_GAP",
            verification_level=VerificationLevel.USER_CLAIM,
            notes="; ".join(analysis.possible_explanations) or None,
        )
        db.add(auto)
        analysis.records.append(auto)
    else:
        auto.amount_paise = unexplained_gap
        auto.notes = "; ".join(analysis.possible_explanations) or None

    analysis.unexplained_paise = unexplained_gap
    db.flush()
    return analysis


def _possible_explanations(db: Session, employee: Employee, period: SalaryPeriod,
                           gap_paise: int) -> list[str]:
    """Candidate reasons for a shortfall, each clearly marked unconfirmed.

    The specification is emphatic (s12): investigate, but do not accuse.
    """
    reasons: list[str] = []

    attendance = (
        db.query(AttendanceRecord)
        .filter(AttendanceRecord.employee_id == employee.id,
                AttendanceRecord.period_id == period.id)
        .first()
    )
    if attendance and attendance.unpaid_leave_days:
        reasons.append(
            f"{attendance.unpaid_leave_days} day(s) of unpaid leave are recorded for this month, "
            "which may account for part of the difference (not confirmed)."
        )
    elif attendance is None:
        reasons.append("No attendance record was uploaded for this month, so an absence-based deduction cannot be ruled out.")

    from ..models import Adjustment

    recoveries = (
        db.query(Adjustment)
        .filter(Adjustment.employee_id == employee.id, Adjustment.kind == "RECOVERY")
        .all()
    )
    if recoveries:
        reasons.append(
            f"{len(recoveries)} recovery adjustment(s) exist for this employee; a recovery "
            "instalment may explain part of the difference (not confirmed)."
        )

    if gap_paise <= 5000:  # up to Rs 50
        reasons.append("The amount is small and could be a bank charge (not confirmed).")

    reasons.append("No authorised deduction record has been found for this amount.")
    return reasons


def mark_documented(db: Session, deduction: Deduction, document_id: int,
                    authority: Optional[str], reason: Optional[str]) -> Deduction:
    """Attach evidence to a deduction, raising its verification level."""
    deduction.evidence_document_id = document_id
    deduction.status = DeductionStatus.DOCUMENTED.value
    deduction.verification_level = VerificationLevel.OFFICIAL_DOCUMENT
    if authority:
        deduction.authority = authority
    if reason:
        deduction.reason = reason
    db.flush()
    return deduction


def unexplained_total(db: Session, employee: Employee) -> int:
    rows = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id,
                Deduction.status == DeductionStatus.UNEXPLAINED.value)
        .all()
    )
    return sum(r.amount_paise for r in rows)
