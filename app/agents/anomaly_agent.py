"""AGENT 9 - Anomaly Detection (spec s32, s58, s63).

Detects the patterns the specification lists and scores them LOW / MEDIUM /
HIGH / CRITICAL.

Language rule, enforced throughout this module: an anomaly is *never* called
fraud. The vocabulary is deliberately neutral -- "possible duplicate",
"unexplained difference", "requires verification" -- because this system
identifies discrepancies; it does not adjudicate intent (spec s58).

Anomalies are de-duplicated on ``(code, entity)`` so a repeated scan updates
an existing finding rather than burying the user in copies.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..enums import (
    AllocationStatus,
    AnomalyCode,
    AnomalyStatus,
    DeductionStatus,
    Direction,
    ReconOutcome,
    Severity,
    SHORTFALL_OUTCOMES,
    TransactionStatus,
)
from ..money import format_money
from ..models import (
    Anomaly,
    BankTransaction,
    Deduction,
    Employee,
    Entitlement,
    PaymentAllocation,
    Reconciliation,
    SalaryPeriod,
)

AGENT_NAME = "anomaly_agent"


# ---------------------------------------------------------------------------
# Creation helper
# ---------------------------------------------------------------------------
def raise_anomaly(
    db: Session,
    organization_id: int,
    *,
    code: str,
    severity: str,
    entity_type: str,
    entity_id: int,
    title_en: str,
    title_hi: Optional[str] = None,
    detail_en: Optional[str] = None,
    detail_hi: Optional[str] = None,
    employee_id: Optional[int] = None,
    period_id: Optional[int] = None,
    amount_paise: Optional[int] = None,
    score: float = 0.0,
    evidence: Optional[dict] = None,
) -> Anomaly:
    """Create or refresh an anomaly. Never duplicates an existing open finding."""
    existing = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id,
                Anomaly.code == code,
                Anomaly.entity_type == entity_type,
                Anomaly.entity_id == entity_id)
        .first()
    )

    if existing is not None:
        # A finding a person already dismissed or resolved stays that way
        # unless the underlying numbers changed.
        if existing.status in {AnomalyStatus.RESOLVED.value, AnomalyStatus.DISMISSED.value}:
            if existing.amount_paise == amount_paise:
                return existing
            existing.status = AnomalyStatus.OPEN.value
            existing.resolved_at = None
            existing.resolution_note = (
                (existing.resolution_note or "")
                + " | Reopened because the underlying amount changed."
            ).strip()
        existing.severity = severity
        existing.title_en = title_en
        existing.title_hi = title_hi
        existing.detail_en = detail_en
        existing.detail_hi = detail_hi
        existing.amount_paise = amount_paise
        existing.score = score
        existing.employee_id = employee_id
        existing.period_id = period_id
        existing.evidence_json = evidence or {}
        db.flush()
        return existing

    anomaly = Anomaly(
        organization_id=organization_id,
        employee_id=employee_id,
        period_id=period_id,
        code=code,
        severity=severity,
        score=score,
        entity_type=entity_type,
        entity_id=entity_id,
        title_en=title_en,
        title_hi=title_hi,
        detail_en=detail_en,
        detail_hi=detail_hi,
        amount_paise=amount_paise,
        evidence_json=evidence or {},
        status=AnomalyStatus.OPEN.value,
    )
    db.add(anomaly)
    db.flush()
    return anomaly


def resolve_stale(db: Session, employee: Employee, active_keys: set[tuple[str, str, int]]) -> int:
    """Close findings that no longer reproduce, so the list stays truthful."""
    open_rows = (
        db.query(Anomaly)
        .filter(Anomaly.employee_id == employee.id,
                Anomaly.status == AnomalyStatus.OPEN.value)
        .all()
    )
    closed = 0
    for row in open_rows:
        if (row.code, row.entity_type, row.entity_id) not in active_keys:
            row.status = AnomalyStatus.RESOLVED.value
            row.resolved_at = utcnow()
            row.resolution_note = "No longer detected in the latest check."
            closed += 1
    db.flush()
    return closed


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def scan_employee(db: Session, employee: Employee) -> list[Anomaly]:
    """Run every detector for one employee."""
    found: list[Anomaly] = []
    org_id = employee.organization_id
    tol = settings.match_tolerance_paise

    reconciliations = (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id == employee.id)
        .all()
    )
    by_period = {r.period_id: r for r in reconciliations}
    ordered = sorted(reconciliations, key=lambda r: r.period.sort_key if r.period else 0)

    # --- 1. Unexplained short credit ---
    for recon in ordered:
        if recon.outcome == ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.UNEXPLAINED_SHORT_CREDIT.value,
                severity=_severity_for_amount(recon.difference_paise, recon.entitled_paise),
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"{recon.period.label} payment is short by "
                         f"{format_money(recon.difference_paise)} with no explanation on record",
                title_hi=f"{recon.period.label_hi} का भुगतान "
                         f"{format_money(recon.difference_paise)} कम है, कारण दर्ज नहीं",
                detail_en=recon.explanation_en,
                detail_hi=recon.explanation_hi,
                amount_paise=recon.difference_paise,
            ))

    # --- 2. Missing / unpaid months ---
    for recon in ordered:
        if recon.outcome == ReconOutcome.NOT_PAID.value:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.MISSING_MONTH.value,
                severity=Severity.HIGH.value,
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"No payment recorded for {recon.period.label}",
                title_hi=f"{recon.period.label_hi} का कोई भुगतान दर्ज नहीं",
                detail_en=recon.explanation_en,
                detail_hi=recon.explanation_hi,
                amount_paise=recon.entitled_paise,
            ))
        elif recon.outcome == ReconOutcome.SANCTIONED_NOT_CREDITED.value:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.OVERDUE_PAYMENT.value,
                severity=Severity.CRITICAL.value,
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"{recon.period.label} was sanctioned but has not reached the bank account",
                title_hi=f"{recon.period.label_hi} स्वीकृत हुआ, पर बैंक खाते में नहीं आया",
                detail_en=recon.explanation_en,
                detail_hi=recon.explanation_hi,
                amount_paise=recon.sanctioned_paise,
            ))

    # --- 3. Excess credit ---
    for recon in ordered:
        if recon.outcome == ReconOutcome.EXCESS_CREDIT.value:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.EXCESS_CREDIT.value,
                severity=Severity.MEDIUM.value,
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"{recon.period.label} received "
                         f"{format_money(abs(recon.difference_paise))} more than expected",
                title_hi=f"{recon.period.label_hi} में अपेक्षा से "
                         f"{format_money(abs(recon.difference_paise))} अधिक प्राप्त",
                detail_en=recon.explanation_en,
                detail_hi=recon.explanation_hi,
                amount_paise=abs(recon.difference_paise),
            ))

    # --- 4. Repeated short payments ---
    shortfalls = [r for r in ordered if r.outcome in SHORTFALL_OUTCOMES]
    if len(shortfalls) >= 3:
        recent = shortfalls[-3:]
        if _consecutive(recent):
            total = sum(r.difference_paise for r in recent)
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.REPEATED_SHORT_PAYMENT.value,
                severity=Severity.HIGH.value,
                entity_type="Employee", entity_id=employee.id,
                employee_id=employee.id,
                title_en=f"Payments have been short for {len(recent)} months in a row",
                title_hi=f"लगातार {len(recent)} महीनों से कम भुगतान हो रहा है",
                detail_en=(
                    "Months affected: "
                    + ", ".join(r.period.label for r in recent)
                    + f". Total difference: {total / 100:.2f}. This is a pattern worth "
                      "raising with the concerned office."
                ),
                detail_hi="लगातार कम भुगतान का यह क्रम संबंधित कार्यालय के समक्ष रखा जाना चाहिए।",
                amount_paise=total,
            ))

    # --- 5. Unexplained deductions ---
    unexplained = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id,
                Deduction.status == DeductionStatus.UNEXPLAINED.value)
        .all()
    )
    for deduction in unexplained:
        if deduction.amount_paise <= tol:
            continue
        found.append(raise_anomaly(
            db, org_id,
            code=AnomalyCode.UNEXPLAINED_DEDUCTION.value,
            severity=Severity.MEDIUM.value,
            entity_type="Deduction", entity_id=deduction.id,
            employee_id=employee.id, period_id=deduction.period_id,
            title_en=f"Deduction of {format_money(deduction.amount_paise)} has no supporting record",
            title_hi=f"{format_money(deduction.amount_paise)} की कटौती का कोई प्रमाण नहीं",
            detail_en=(
                (deduction.reason or "")
                + " No authorising document has been linked to this deduction. "
                  "A deduction is not assumed to be legitimate merely because the "
                  "received amount was lower."
            ),
            detail_hi="इस कटौती से कोई अधिकृत दस्तावेज़ नहीं जुड़ा है।",
            amount_paise=deduction.amount_paise,
        ))

    # --- 6. Delayed payments ---
    for recon in ordered:
        if recon.delay_days and recon.delay_days > settings.delay_threshold_days:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.DELAYED_PAYMENT.value,
                severity=Severity.LOW.value if recon.delay_days < 30 else Severity.MEDIUM.value,
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"{recon.period.label} payment arrived {recon.delay_days} days late",
                title_hi=f"{recon.period.label_hi} का भुगतान {recon.delay_days} दिन देर से आया",
                detail_en=(
                    f"Expected on {recon.expected_pay_date.isoformat() if recon.expected_pay_date else 'unknown'}, "
                    f"received on {recon.first_credit_date.isoformat() if recon.first_credit_date else 'unknown'}."
                ),
                detail_hi="भुगतान अपेक्षित तिथि के बाद प्राप्त हुआ।",
                amount_paise=recon.credited_paise,
            ))

    # --- 7. Unallocated credits (spec s70) ---
    unallocated = (
        db.query(BankTransaction)
        .filter(BankTransaction.employee_id == employee.id,
                BankTransaction.direction == Direction.CREDIT.value,
                BankTransaction.is_reversed.is_(False),
                BankTransaction.status.in_([TransactionStatus.UNALLOCATED.value,
                                            TransactionStatus.REVIEW_REQUIRED.value,
                                            TransactionStatus.PARTIALLY_ALLOCATED.value]))
        .all()
    )
    for txn in unallocated:
        remaining = txn.unallocated_paise
        if remaining <= tol:
            continue
        found.append(raise_anomaly(
            db, org_id,
            code=AnomalyCode.UNALLOCATED_CREDIT.value,
            severity=Severity.LOW.value,
            entity_type="BankTransaction", entity_id=txn.id,
            employee_id=employee.id,
            title_en=f"{format_money(remaining)} received on {txn.txn_date.isoformat()} "
                     "is not yet assigned to a month",
            title_hi=f"{txn.txn_date.isoformat()} को प्राप्त {format_money(remaining)} "
                     "अभी किसी माह से नहीं जोड़ी गई",
            detail_en=(
                "This money has deliberately not been forced into a salary month. "
                "Please confirm what it relates to."
            ),
            detail_hi="यह राशि जानबूझकर किसी माह में नहीं डाली गई है। कृपया पुष्टि करें।",
            amount_paise=remaining,
        ))

    # --- 8. Sudden change in the paid amount ---
    credited = [(r.period, r.credited_paise) for r in ordered if r.credited_paise > 0]
    for index in range(1, len(credited)):
        prev_period, prev_amount = credited[index - 1]
        curr_period, curr_amount = credited[index]
        if prev_amount <= 0:
            continue
        change = abs(curr_amount - prev_amount) / prev_amount
        if change >= 0.4:
            recon = by_period.get(curr_period.id)
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.SUDDEN_AMOUNT_CHANGE.value,
                severity=Severity.LOW.value,
                entity_type="Reconciliation",
                entity_id=recon.id if recon else curr_period.id,
                employee_id=employee.id, period_id=curr_period.id,
                title_en=f"Amount received changed sharply between {prev_period.label} "
                         f"and {curr_period.label}",
                title_hi=f"{prev_period.label_hi} और {curr_period.label_hi} के बीच "
                         "प्राप्त राशि में बड़ा बदलाव",
                detail_en=(
                    f"{prev_period.label}: {format_money(prev_amount)}; "
                    f"{curr_period.label}: {format_money(curr_amount)}. "
                    "This may be due to arrears, an incentive or a rate change, "
                    "or it may need checking."
                ),
                detail_hi="यह बकाया, प्रोत्साहन या दर परिवर्तन के कारण हो सकता है।",
                amount_paise=abs(curr_amount - prev_amount),
            ))

    # --- 9. Missing entitlement rule ---
    for recon in ordered:
        if recon.outcome == ReconOutcome.NO_ENTITLEMENT_RULE.value:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.MISSING_ENTITLEMENT_RULE.value,
                severity=Severity.HIGH.value,
                entity_type="Reconciliation", entity_id=recon.id,
                employee_id=employee.id, period_id=recon.period_id,
                title_en=f"No salary rule is configured for {recon.period.label}",
                title_hi=f"{recon.period.label_hi} के लिए कोई वेतन नियम नहीं",
                detail_en=recon.explanation_en,
                detail_hi=recon.explanation_hi,
            ))

    # --- 10. Payments to an unexpected account ---
    known_ids = {a.id for a in employee.bank_accounts}
    if known_ids:
        strays = (
            db.query(BankTransaction)
            .filter(BankTransaction.employee_id == employee.id,
                    BankTransaction.bank_account_id.isnot(None),
                    ~BankTransaction.bank_account_id.in_(known_ids))
            .all()
        )
        for txn in strays:
            found.append(raise_anomaly(
                db, org_id,
                code=AnomalyCode.UNEXPECTED_ACCOUNT.value,
                severity=Severity.MEDIUM.value,
                entity_type="BankTransaction", entity_id=txn.id,
                employee_id=employee.id,
                title_en="Payment recorded against an account not on this employee's file",
                title_hi="इस कर्मचारी की फ़ाइल में दर्ज न किए गए खाते में भुगतान",
                detail_en="Please confirm the bank account details for this employee.",
                detail_hi="कृपया इस कर्मचारी के बैंक खाते की जानकारी की पुष्टि करें।",
                amount_paise=txn.amount_paise,
            ))

    active_keys = {(a.code, a.entity_type, a.entity_id) for a in found}
    resolve_stale(db, employee, active_keys)
    return found


def _consecutive(recons: list[Reconciliation]) -> bool:
    keys = [r.period.sort_key for r in recons if r.period]
    if len(keys) < 2:
        return False
    for a, b in zip(keys, keys[1:]):
        ay, am = divmod(a, 100)
        by, bm = divmod(b, 100)
        if (by * 12 + bm) - (ay * 12 + am) != 1:
            return False
    return True


def _severity_for_amount(difference_paise: int, entitled_paise: int) -> str:
    """Scale severity by how much of the month's pay is missing."""
    if entitled_paise <= 0:
        return Severity.MEDIUM.value
    ratio = abs(difference_paise) / entitled_paise
    if ratio >= 0.5:
        return Severity.CRITICAL.value
    if ratio >= 0.2:
        return Severity.HIGH.value
    if ratio >= 0.05:
        return Severity.MEDIUM.value
    return Severity.LOW.value


def scan_organization(db: Session, organization_id: int) -> list[Anomaly]:
    employees = db.query(Employee).filter(Employee.organization_id == organization_id).all()
    out: list[Anomaly] = []
    for employee in employees:
        out.extend(scan_employee(db, employee))
    out.extend(_scan_duplicate_employees(db, organization_id))
    return out


def _scan_duplicate_employees(db: Session, organization_id: int) -> list[Anomaly]:
    """Flag employees who may be the same person entered twice (spec s32).

    Never merges anything: identity is a human decision (spec s23).
    """
    from ..services.textutils import name_similarity

    employees = db.query(Employee).filter(Employee.organization_id == organization_id).all()
    found: list[Anomaly] = []

    for i, a in enumerate(employees):
        for b in employees[i + 1:]:
            score = name_similarity(a.full_name, b.full_name)
            if score < 0.9:
                continue
            # Strong corroboration required before even suggesting a duplicate.
            corroboration = 0
            if a.date_of_birth and a.date_of_birth == b.date_of_birth:
                corroboration += 1
            if a.designation_id and a.designation_id == b.designation_id:
                corroboration += 1
            if a.location_id and a.location_id == b.location_id:
                corroboration += 1
            if corroboration < 2:
                continue

            found.append(raise_anomaly(
                db, organization_id,
                code=AnomalyCode.POSSIBLE_DUPLICATE_EMPLOYEE.value,
                severity=Severity.MEDIUM.value,
                entity_type="Employee", entity_id=min(a.id, b.id),
                employee_id=a.id,
                title_en=f"'{a.full_name}' and '{b.full_name}' may be the same person",
                title_hi=f"'{a.full_name}' और '{b.full_name}' एक ही व्यक्ति हो सकते हैं",
                detail_en=(
                    f"Employee codes {a.employee_code} and {b.employee_code} have very similar "
                    "names and share other details. These records have NOT been merged -- "
                    "please confirm whether they are the same person."
                ),
                detail_hi="ये रिकॉर्ड जोड़े नहीं गए हैं — कृपया पुष्टि करें।",
                score=score,
            ))
    return found
