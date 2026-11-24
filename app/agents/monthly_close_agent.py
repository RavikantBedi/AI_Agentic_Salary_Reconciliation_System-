"""AGENT 13 - Monthly Close, Yearly Reconciliation and Agentic Monitoring.

Specification coverage: s36 (monthly close), s37 (yearly), s63 (periodic
agentic inspection), s64 (action centre feed).

Closing a month freezes its figures. A closed month can still be corrected --
records must never be immutable to the point of being wrong -- but doing so
REQUIRES a reason and always writes an audit event. Nothing changes silently
(spec s36, s51).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..db import utcnow
from ..enums import (
    AllocationStatus,
    AnomalyStatus,
    ArrearStatus,
    DeductionStatus,
    Direction,
    PeriodStatus,
    ReconOutcome,
    SHORTFALL_OUTCOMES,
    Severity,
    TransactionStatus,
    VerificationLevel,
)
from ..models import (
    Anomaly,
    Arrear,
    BankTransaction,
    BenefitDistribution,
    Deduction,
    Dispute,
    Employee,
    Notification,
    PaymentAllocation,
    Reconciliation,
    SalaryPeriod,
    User,
)
from ..money import format_money

AGENT_NAME = "monthly_close_agent"


# ---------------------------------------------------------------------------
# Monthly close
# ---------------------------------------------------------------------------
@dataclass
class ClosingStatement:
    period_label: str = ""
    employees: int = 0
    expected_paise: int = 0
    sanctioned_paise: int = 0
    received_paise: int = 0
    pending_paise: int = 0
    short_paise: int = 0
    excess_paise: int = 0
    deducted_paise: int = 0
    unexplained_deduction_paise: int = 0
    arrears_outstanding_paise: int = 0
    arrears_paid_paise: int = 0
    benefits_short_items: int = 0
    unverified_transactions: int = 0
    open_disputes: int = 0
    fully_reconciled: int = 0
    needs_review: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def build_statement(db: Session, period: SalaryPeriod) -> ClosingStatement:
    """Compute the closing figures for a period without writing anything."""
    statement = ClosingStatement(period_label=period.label)

    recons = (
        db.query(Reconciliation)
        .filter(Reconciliation.period_id == period.id)
        .all()
    )
    statement.employees = len(recons)

    for recon in recons:
        statement.expected_paise += recon.entitled_paise
        statement.sanctioned_paise += recon.sanctioned_paise or 0
        statement.received_paise += recon.credited_paise
        statement.deducted_paise += recon.deducted_paise
        statement.unexplained_deduction_paise += recon.unexplained_deduction_paise
        statement.arrears_paid_paise += recon.arrear_received_paise

        if recon.difference_paise > 0:
            statement.short_paise += recon.difference_paise
        elif recon.difference_paise < 0:
            statement.excess_paise += abs(recon.difference_paise)

        if recon.outcome == ReconOutcome.FULLY_RECONCILED.value:
            statement.fully_reconciled += 1
        elif recon.outcome != ReconOutcome.AWAITING_CONFIRMATION.value:
            statement.needs_review += 1

    statement.pending_paise = statement.short_paise

    arrears = (
        db.query(Arrear)
        .filter(Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value]))
        .all()
    )
    statement.arrears_outstanding_paise = sum(a.remaining_paise for a in arrears)

    statement.benefits_short_items = (
        db.query(BenefitDistribution)
        .filter(BenefitDistribution.period_id == period.id,
                BenefitDistribution.shortage_quantity > 0)
        .count()
    )

    statement.unverified_transactions = (
        db.query(BankTransaction)
        .join(PaymentAllocation, PaymentAllocation.transaction_id == BankTransaction.id)
        .filter(PaymentAllocation.period_id == period.id,
                BankTransaction.verification_level < VerificationLevel.OFFICIAL_DOCUMENT)
        .distinct()
        .count()
    )

    statement.open_disputes = (
        db.query(Dispute)
        .filter(Dispute.period_id == period.id,
                Dispute.status.notin_(["RESOLVED", "REJECTED", "CLOSED"]))
        .count()
    )
    return statement


def close_period(db: Session, period: SalaryPeriod, user: Optional[User] = None,
                 reason: Optional[str] = None) -> ClosingStatement:
    """Close a month and freeze its closing statement."""
    statement = build_statement(db, period)

    period.status = PeriodStatus.CLOSED.value
    period.closed_at = utcnow()
    period.closed_by_id = user.id if user else None
    period.closing_summary_json = statement.as_dict()
    db.flush()

    audit_record(
        db,
        action="CLOSE_PERIOD",
        entity_type="SalaryPeriod",
        entity_id=period.id,
        organization_id=period.organization_id,
        actor=user,
        new_value=statement.as_dict(),
        reason=reason or f"Monthly close for {period.label}",
        source=AuditSource.HUMAN if user else AuditSource.agent(AGENT_NAME),
    )
    return statement


def reopen_period(db: Session, period: SalaryPeriod, user: User, reason: str) -> None:
    """Reopen a closed month. A reason is mandatory (spec s36)."""
    if not reason or not reason.strip():
        raise ValueError("A reason is required to reopen a closed month.")

    before = {"status": period.status, "closed_at": period.closed_at.isoformat() if period.closed_at else None}
    period.status = PeriodStatus.REOPENED.value
    period.closed_at = None
    db.flush()

    audit_record(
        db,
        action="REOPEN_PERIOD",
        entity_type="SalaryPeriod",
        entity_id=period.id,
        organization_id=period.organization_id,
        actor=user,
        old_value=before,
        new_value={"status": period.status},
        reason=reason,
        source=AuditSource.HUMAN,
    )


# ---------------------------------------------------------------------------
# Yearly reconciliation (spec s37)
# ---------------------------------------------------------------------------
@dataclass
class AnnualSummary:
    year: int = 0
    entitled_paise: int = 0
    sanctioned_paise: int = 0
    received_paise: int = 0
    deductions_paise: int = 0
    unexplained_paise: int = 0
    arrears_received_paise: int = 0
    arrears_outstanding_paise: int = 0
    benefits_received: int = 0
    benefits_short: int = 0
    unexplained_difference_paise: int = 0
    average_delay_days: Optional[float] = None
    verification_percent: float = 0.0
    months_reconciled: int = 0
    months_total: int = 0
    rows: list[Reconciliation] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "rows"}
        return data


def annual_summary(db: Session, organization_id: int, year: int,
                   employee: Optional[Employee] = None) -> AnnualSummary:
    """Yearly totals for one employee, or for the whole organisation."""
    summary = AnnualSummary(year=year)

    query = (
        db.query(Reconciliation)
        .join(SalaryPeriod, SalaryPeriod.id == Reconciliation.period_id)
        .filter(Reconciliation.organization_id == organization_id,
                SalaryPeriod.year == year)
    )
    if employee is not None:
        query = query.filter(Reconciliation.employee_id == employee.id)
    rows = query.all()
    summary.rows = sorted(rows, key=lambda r: r.period.sort_key if r.period else 0)
    summary.months_total = len(rows)

    delays: list[int] = []
    verified = 0
    for recon in rows:
        summary.entitled_paise += recon.entitled_paise
        summary.sanctioned_paise += recon.sanctioned_paise or 0
        summary.received_paise += recon.credited_paise
        summary.deductions_paise += recon.deducted_paise
        summary.unexplained_paise += recon.unexplained_deduction_paise
        summary.arrears_received_paise += recon.arrear_received_paise

        if recon.outcome == ReconOutcome.FULLY_RECONCILED.value:
            summary.months_reconciled += 1
        if recon.outcome in SHORTFALL_OUTCOMES:
            summary.unexplained_difference_paise += max(0, recon.difference_paise)
        if recon.delay_days is not None and recon.delay_days > 0:
            delays.append(recon.delay_days)
        if recon.credit_verification_level >= VerificationLevel.OFFICIAL_DOCUMENT:
            verified += 1

    summary.average_delay_days = round(sum(delays) / len(delays), 1) if delays else None
    summary.verification_percent = round(verified * 100 / len(rows), 1) if rows else 0.0

    arrear_query = db.query(Arrear).filter(
        Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value])
    )
    if employee is not None:
        arrear_query = arrear_query.filter(Arrear.employee_id == employee.id)
    summary.arrears_outstanding_paise = sum(a.remaining_paise for a in arrear_query.all())

    benefit_query = (
        db.query(BenefitDistribution)
        .join(SalaryPeriod, SalaryPeriod.id == BenefitDistribution.period_id)
        .filter(SalaryPeriod.year == year)
    )
    if employee is not None:
        benefit_query = benefit_query.filter(BenefitDistribution.employee_id == employee.id)
    distributions = benefit_query.all()
    summary.benefits_received = len(distributions)
    summary.benefits_short = sum(1 for d in distributions if d.shortage_quantity > 0)

    return summary


# ---------------------------------------------------------------------------
# Agentic monitoring (spec s63, s64)
# ---------------------------------------------------------------------------
@dataclass
class ActionItem:
    severity: str
    title_en: str
    title_hi: str
    detail_en: str = ""
    detail_hi: str = ""
    link: str = ""
    amount_paise: Optional[int] = None
    category: str = "REVIEW"

    def title(self, locale: str) -> str:
        return self.title_hi if locale == "hi" else self.title_en

    def detail(self, locale: str) -> str:
        return self.detail_hi if locale == "hi" else self.detail_en


@dataclass
class MonitorResult:
    critical: list[ActionItem] = field(default_factory=list)
    review: list[ActionItem] = field(default_factory=list)
    reconciled_count: int = 0
    questions_checked: list[str] = field(default_factory=list)

    @property
    def total_open(self) -> int:
        return len(self.critical) + len(self.review)


#: The questions the agent asks on every pass (spec s63).
MONITOR_QUESTIONS = [
    "Are any months missing?",
    "Are expected payments overdue?",
    "Are there unexplained deductions?",
    "Are there unmatched bank credits?",
    "Are there unresolved arrears?",
    "Are any records inconsistent?",
    "Are any documents missing?",
]


def monitor(db: Session, organization_id: int,
            employee: Optional[Employee] = None) -> MonitorResult:
    """Inspect open records and produce the Action Centre feed.

    Reads from the anomaly ledger and the reconciliation table rather than
    re-deriving findings, so what the user sees here is exactly what the
    engines concluded.
    """
    result = MonitorResult(questions_checked=list(MONITOR_QUESTIONS))

    anomaly_query = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id,
                Anomaly.status == AnomalyStatus.OPEN.value)
    )
    if employee is not None:
        anomaly_query = anomaly_query.filter(Anomaly.employee_id == employee.id)

    for anomaly in anomaly_query.all():
        item = ActionItem(
            severity=anomaly.severity,
            title_en=anomaly.title_en,
            title_hi=anomaly.title_hi or anomaly.title_en,
            detail_en=anomaly.detail_en or "",
            detail_hi=anomaly.detail_hi or anomaly.detail_en or "",
            link=_link_for(anomaly),
            amount_paise=anomaly.amount_paise,
            category=anomaly.code,
        )
        if anomaly.severity in {Severity.CRITICAL.value, Severity.HIGH.value}:
            result.critical.append(item)
        else:
            result.review.append(item)

    # Credits sitting unassigned to a month (spec s70).
    txn_query = (
        db.query(BankTransaction)
        .filter(BankTransaction.organization_id == organization_id,
                BankTransaction.direction == Direction.CREDIT.value,
                BankTransaction.is_reversed.is_(False),
                BankTransaction.status == TransactionStatus.REVIEW_REQUIRED.value)
    )
    if employee is not None:
        txn_query = txn_query.filter(BankTransaction.employee_id == employee.id)

    for txn in txn_query.all():
        # Skip if an anomaly already covers this transaction.
        if any(i.category == "UNCERTAIN_MONTH_MAPPING" and str(txn.id) in i.link
               for i in result.review):
            continue
        result.review.append(ActionItem(
            severity=Severity.MEDIUM.value,
            title_en=f"Confirm which month the {format_money(txn.amount_paise)} credit "
                     f"of {txn.txn_date.isoformat()} belongs to",
            title_hi=f"{txn.txn_date.isoformat()} की {format_money(txn.amount_paise)} राशि "
                     "किस माह की है, पुष्टि करें",
            detail_en="This money has not been assigned to a salary month because the "
                      "evidence was not conclusive.",
            detail_hi="प्रमाण निर्णायक न होने के कारण यह राशि किसी वेतन माह से नहीं जोड़ी गई है।",
            link=f"/transactions/{txn.uid}",
            amount_paise=txn.amount_paise,
            category="ASSIGN_MONTH",
        ))

    recon_query = db.query(Reconciliation).filter(
        Reconciliation.organization_id == organization_id,
        Reconciliation.outcome == ReconOutcome.FULLY_RECONCILED.value,
    )
    if employee is not None:
        recon_query = recon_query.filter(Reconciliation.employee_id == employee.id)
    result.reconciled_count = recon_query.count()

    order = {Severity.CRITICAL.value: 0, Severity.HIGH.value: 1,
             Severity.MEDIUM.value: 2, Severity.LOW.value: 3}
    result.critical.sort(key=lambda i: (order.get(i.severity, 9), -(i.amount_paise or 0)))
    result.review.sort(key=lambda i: (order.get(i.severity, 9), -(i.amount_paise or 0)))
    return result


def _link_for(anomaly: Anomaly) -> str:
    mapping = {
        "BankTransaction": "/transactions",
        "Reconciliation": "/periods",
        "Deduction": "/deductions",
        "Employee": "/employees",
    }
    base = mapping.get(anomaly.entity_type, "/action-center")
    return f"{base}#{anomaly.entity_type}-{anomaly.entity_id}"


# ---------------------------------------------------------------------------
# Notifications (spec s62 - notify only when meaningful)
# ---------------------------------------------------------------------------
def notify(db: Session, *, organization_id: int, user_id: Optional[int],
           employee_id: Optional[int], kind: str, severity: str,
           title_en: str, title_hi: str, body_en: str = "", body_hi: str = "",
           link: str = "", dedupe_key: Optional[str] = None) -> Optional[Notification]:
    """Create a notification unless an identical one already exists.

    The dedupe key is what stops a nightly monitor run from re-notifying about
    the same unchanged fact every single day.
    """
    if dedupe_key:
        existing = (
            db.query(Notification)
            .filter(Notification.dedupe_key == dedupe_key)
            .first()
        )
        if existing:
            return None

    notification = Notification(
        organization_id=organization_id,
        user_id=user_id,
        employee_id=employee_id,
        kind=kind,
        severity=severity,
        title_en=title_en,
        title_hi=title_hi,
        body_en=body_en,
        body_hi=body_hi,
        link=link,
        dedupe_key=dedupe_key,
    )
    db.add(notification)
    db.flush()
    return notification


def notify_from_monitor(db: Session, organization_id: int) -> int:
    """Turn critical monitor findings into notifications for the right people."""
    created = 0
    employees = db.query(Employee).filter(Employee.organization_id == organization_id).all()

    for employee in employees:
        result = monitor(db, organization_id, employee)
        if not result.critical:
            continue

        user = (
            db.query(User)
            .filter(User.employee_id == employee.id, User.is_active.is_(True))
            .first()
        )
        top = result.critical[0]
        made = notify(
            db,
            organization_id=organization_id,
            user_id=user.id if user else None,
            employee_id=employee.id,
            kind=top.category,
            severity=top.severity,
            title_en=top.title_en,
            title_hi=top.title_hi,
            body_en=top.detail_en,
            body_hi=top.detail_hi,
            link=top.link,
            dedupe_key=f"{employee.id}:{top.category}:{top.amount_paise}",
        )
        if made:
            created += 1
    return created


def payment_health(db: Session, employee: Employee) -> str:
    """The dashboard traffic light for one employee (spec s38)."""
    from ..enums import PaymentHealth

    recons = db.query(Reconciliation).filter(Reconciliation.employee_id == employee.id).all()
    if not recons:
        return PaymentHealth.UNKNOWN.value

    critical = (
        db.query(Anomaly)
        .filter(Anomaly.employee_id == employee.id,
                Anomaly.status == AnomalyStatus.OPEN.value,
                Anomaly.severity.in_([Severity.CRITICAL.value, Severity.HIGH.value]))
        .count()
    )
    if critical:
        return PaymentHealth.ACTION_REQUIRED.value

    problems = [r for r in recons
                if r.outcome not in {ReconOutcome.FULLY_RECONCILED.value,
                                     ReconOutcome.AWAITING_CONFIRMATION.value}]
    if problems:
        return PaymentHealth.ATTENTION.value

    unexplained = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id,
                Deduction.status == DeductionStatus.UNEXPLAINED.value)
        .count()
    )
    if unexplained:
        return PaymentHealth.ATTENTION.value

    return PaymentHealth.GOOD.value
