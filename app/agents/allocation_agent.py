"""Payment allocation, match scoring, duplicate and reversal detection.

Specification coverage: s10, s11, s14, s15, s57, s69, s70, s99.

The two invariants this module exists to protect
------------------------------------------------
1. **A transaction is never counted twice.** The sum of a transaction's
   non-rejected allocations may never exceed its amount. Every write goes
   through :func:`allocate`, which checks the remaining headroom first.
2. **Money is never forced into a month.** What cannot be confidently
   attributed stays unallocated and visible (spec s70), rather than being
   quietly absorbed to make a total look tidy.

Match scores are transparent and itemised (spec s57): every point awarded
carries a reason, and the breakdown is stored on the allocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..enums import (
    AllocationMethod,
    AllocationStatus,
    Direction,
    PaymentClassification,
    TransactionStatus,
)
from ..models import (
    Arrear,
    ArrearSettlement,
    BankTransaction,
    Employee,
    Entitlement,
    PaymentAllocation,
    PaymentSanction,
    SalaryPeriod,
)

AGENT_NAME = "allocation_agent"


class AllocationError(Exception):
    """Raised when an allocation would violate a financial invariant."""


# ---------------------------------------------------------------------------
# Match scoring (spec s57)
# ---------------------------------------------------------------------------
@dataclass
class ScoreLine:
    factor: str
    points: int
    detail: str


@dataclass
class MatchScore:
    lines: list[ScoreLine] = field(default_factory=list)

    @property
    def total(self) -> int:
        return max(0, min(100, sum(line.points for line in self.lines)))

    def add(self, factor: str, points: int, detail: str) -> None:
        self.lines.append(ScoreLine(factor, points, detail))

    def as_json(self) -> list[dict]:
        return [{"factor": l.factor, "points": l.points, "detail": l.detail} for l in self.lines]

    @property
    def explanation(self) -> str:
        parts = [f"{l.detail} (+{l.points})" if l.points >= 0 else f"{l.detail} ({l.points})"
                 for l in self.lines]
        return f"Match score {self.total}/100: " + "; ".join(parts)


#: Published weights. Kept as constants so the UI can show the scoring rules.
POINTS = {
    "utr_match": 40,
    "employee_match": 20,
    "month_match": 15,
    "amount_match": 10,
    "sender_match": 10,
    "historical_pattern": 5,
}


def score_allocation(
    db: Session,
    txn: BankTransaction,
    employee: Employee,
    period: Optional[SalaryPeriod],
    month_probability: float = 0.0,
    month_explicit: bool = False,
) -> MatchScore:
    """Build the transparent match score for putting ``txn`` against ``period``."""
    score = MatchScore()

    # --- UTR / official reference agreement (strongest single signal) ---
    if txn.utr and period:
        sanction_hit = (
            db.query(PaymentSanction)
            .filter(PaymentSanction.employee_id == employee.id,
                    PaymentSanction.period_id == period.id)
            .first()
        )
        matched_utr = False
        if sanction_hit:
            for disb in sanction_hit.disbursements:
                if disb.utr and disb.utr.upper() == txn.utr.upper():
                    matched_utr = True
                    break
        if matched_utr:
            score.add("utr_match", POINTS["utr_match"],
                      f"Transaction reference {txn.utr} matches the official disbursement record")

    # --- Employee identification ---
    if txn.employee_id == employee.id:
        detail = ("Transaction is on this employee's own statement"
                  if not txn.is_manual_entry else "Recorded directly against this employee")
        score.add("employee_match", POINTS["employee_match"], detail)

    # --- Salary month agreement ---
    if period is not None:
        if month_explicit:
            score.add("month_match", POINTS["month_match"],
                      f"The narration names {period.label} explicitly")
        elif month_probability >= 0.6:
            score.add("month_match", int(POINTS["month_match"] * month_probability),
                      f"{period.label} is the most likely salary month "
                      f"({month_probability * 100:.0f}% confidence)")
        elif month_probability > 0:
            score.add("month_match", int(POINTS["month_match"] * month_probability * 0.5),
                      f"{period.label} is a possible salary month "
                      f"({month_probability * 100:.0f}% confidence)")

    # --- Expected amount agreement ---
    if period is not None:
        entitlement = (
            db.query(Entitlement)
            .filter(Entitlement.employee_id == employee.id,
                    Entitlement.period_id == period.id,
                    Entitlement.is_current.is_(True))
            .first()
        )
        if entitlement and entitlement.net_paise > 0:
            diff = abs(entitlement.net_paise - txn.amount_paise)
            if diff <= settings.match_tolerance_paise:
                score.add("amount_match", POINTS["amount_match"],
                          "The credited amount equals the expected amount for this month")
            elif diff <= entitlement.net_paise * 0.1:
                score.add("amount_match", POINTS["amount_match"] // 2,
                          "The credited amount is close to the expected amount for this month")

    # --- Sender agreement ---
    if txn.counterparty:
        previous = (
            db.query(BankTransaction)
            .filter(BankTransaction.employee_id == employee.id,
                    BankTransaction.counterparty == txn.counterparty,
                    BankTransaction.id != txn.id,
                    BankTransaction.direction == Direction.CREDIT.value)
            .count()
        )
        if previous:
            score.add("sender_match", POINTS["sender_match"],
                      f"This sender has paid this employee {previous} time(s) before")

    # --- Historical pattern ---
    if period is not None:
        prior_allocations = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.employee_id == employee.id,
                    PaymentAllocation.status == AllocationStatus.CONFIRMED.value)
            .count()
        )
        if prior_allocations >= 3:
            score.add("historical_pattern", POINTS["historical_pattern"],
                      "Consistent with this employee's established payment history")

    return score


# ---------------------------------------------------------------------------
# Allocation with the double-counting invariant (spec s69)
# ---------------------------------------------------------------------------
def allocated_paise(db: Session, txn: BankTransaction) -> int:
    rows = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.transaction_id == txn.id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    return sum(r.amount_paise for r in rows)


def remaining_paise(db: Session, txn: BankTransaction) -> int:
    """Headroom left on a transaction. Never negative."""
    return max(0, txn.amount_paise - allocated_paise(db, txn))


def allocate(
    db: Session,
    txn: BankTransaction,
    employee: Employee,
    period: Optional[SalaryPeriod],
    amount_paise: int,
    *,
    kind: str = PaymentClassification.SALARY.value,
    method: str = AllocationMethod.AUTO.value,
    status: str = AllocationStatus.SUGGESTED.value,
    score: Optional[MatchScore] = None,
    confidence: float = 0.0,
    arrear_id: Optional[int] = None,
    created_by_id: Optional[int] = None,
    reasoning: Optional[str] = None,
) -> PaymentAllocation:
    """Create one allocation, refusing to break the sum invariant.

    Raises :class:`AllocationError` rather than silently trimming: an
    over-allocation is a bug that must surface, not be papered over.
    """
    if amount_paise <= 0:
        raise AllocationError("An allocation must be for a positive amount.")

    headroom = remaining_paise(db, txn)
    if amount_paise > headroom:
        raise AllocationError(
            f"Cannot allocate {amount_paise / 100:.2f} to this transaction: only "
            f"{headroom / 100:.2f} of the {txn.amount_paise / 100:.2f} credit is still unallocated. "
            "Adjust or remove an existing allocation first."
        )

    allocation = PaymentAllocation(
        transaction_id=txn.id,
        employee_id=employee.id,
        period_id=period.id if period else None,
        arrear_id=arrear_id,
        kind=kind,
        amount_paise=amount_paise,
        method=method,
        status=status,
        match_score=score.total if score else 0,
        match_breakdown_json=score.as_json() if score else [],
        confidence=confidence,
        reasoning=reasoning or (score.explanation if score else None),
        created_by_id=created_by_id,
    )
    if status == AllocationStatus.CONFIRMED.value:
        allocation.confirmed_by_id = created_by_id
        allocation.confirmed_at = utcnow()

    db.add(allocation)
    db.flush()
    refresh_transaction_status(db, txn)
    return allocation


def refresh_transaction_status(db: Session, txn: BankTransaction) -> None:
    """Recompute a transaction's status from its allocations."""
    if txn.is_reversed:
        txn.status = TransactionStatus.REVERSED.value
        return

    total = allocated_paise(db, txn)
    if total == 0:
        txn.status = TransactionStatus.UNALLOCATED.value
    elif total >= txn.amount_paise:
        rows = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.transaction_id == txn.id,
                    PaymentAllocation.status != AllocationStatus.REJECTED.value)
            .all()
        )
        all_confirmed = all(r.status == AllocationStatus.CONFIRMED.value for r in rows)
        txn.status = (TransactionStatus.RECONCILED.value if all_confirmed
                      else TransactionStatus.MATCHED.value)
    else:
        txn.status = TransactionStatus.PARTIALLY_ALLOCATED.value
    db.flush()


def clear_auto_allocations(db: Session, txn: BankTransaction) -> int:
    """Remove machine-made, unconfirmed allocations before recomputing.

    Human decisions (MANUAL method or CONFIRMED status) are never discarded.
    """
    rows = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.transaction_id == txn.id,
                PaymentAllocation.method == AllocationMethod.AUTO.value,
                PaymentAllocation.status == AllocationStatus.SUGGESTED.value)
        .all()
    )
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


def confirm(db: Session, allocation: PaymentAllocation, user_id: Optional[int]) -> PaymentAllocation:
    allocation.status = AllocationStatus.CONFIRMED.value
    allocation.confirmed_by_id = user_id
    allocation.confirmed_at = utcnow()
    db.flush()
    refresh_transaction_status(db, allocation.transaction)
    return allocation


def reject(db: Session, allocation: PaymentAllocation, user_id: Optional[int]) -> PaymentAllocation:
    allocation.status = AllocationStatus.REJECTED.value
    allocation.confirmed_by_id = user_id
    allocation.confirmed_at = utcnow()
    db.flush()
    refresh_transaction_status(db, allocation.transaction)
    return allocation


def split(db: Session, allocation: PaymentAllocation, parts: list[tuple[Optional[int], int]],
          user_id: Optional[int]) -> list[PaymentAllocation]:
    """Split one allocation into several period/amount parts (spec s44).

    The parts must sum exactly to the original amount.
    """
    total = sum(amount for _, amount in parts)
    if total != allocation.amount_paise:
        raise AllocationError(
            f"The parts add up to {total / 100:.2f} but the original allocation is "
            f"{allocation.amount_paise / 100:.2f}. They must match exactly."
        )

    txn = allocation.transaction
    employee = allocation.employee
    created: list[PaymentAllocation] = []

    db.delete(allocation)
    db.flush()

    for period_id, amount in parts:
        period = db.get(SalaryPeriod, period_id) if period_id else None
        created.append(allocate(
            db, txn, employee, period, amount,
            kind=allocation.kind,
            method=AllocationMethod.MANUAL.value,
            status=AllocationStatus.CONFIRMED.value,
            created_by_id=user_id,
            reasoning="Split from a single allocation by a user",
        ))
    return created


# ---------------------------------------------------------------------------
# Arrear settlement linkage (spec s18)
# ---------------------------------------------------------------------------
def settle_arrears(db: Session, allocation: PaymentAllocation) -> list[ArrearSettlement]:
    """Apply an arrear-kind allocation against the oldest open arrears first."""
    if allocation.kind != PaymentClassification.ARREAR.value:
        return []

    remaining = allocation.amount_paise
    settlements: list[ArrearSettlement] = []

    query = (
        db.query(Arrear)
        .filter(Arrear.employee_id == allocation.employee_id,
                Arrear.status.in_(["OPEN", "PARTIALLY_SETTLED"]))
    )
    if allocation.arrear_id:
        query = query.filter(Arrear.id == allocation.arrear_id)

    arrears = sorted(query.all(), key=lambda a: a.origin_period.sort_key if a.origin_period else 0)

    for arrear in arrears:
        if remaining <= 0:
            break
        due = arrear.remaining_paise
        if due <= 0:
            continue
        applied = min(due, remaining)
        settlement = ArrearSettlement(
            arrear_id=arrear.id,
            allocation_id=allocation.id,
            amount_paise=applied,
            settled_on=allocation.transaction.txn_date if allocation.transaction else None,
            note=f"Settled from transaction {allocation.transaction.uid}" if allocation.transaction else None,
        )
        db.add(settlement)
        arrear.settled_paise += applied
        arrear.status = "SETTLED" if arrear.remaining_paise <= 0 else "PARTIALLY_SETTLED"
        remaining -= applied
        settlements.append(settlement)

    db.flush()
    return settlements


# ---------------------------------------------------------------------------
# Duplicate detection (spec s14) - flag, never delete
# ---------------------------------------------------------------------------
@dataclass
class DuplicateSignal:
    other: BankTransaction
    reasons: list[str]
    strength: float


def find_possible_duplicates(db: Session, txn: BankTransaction,
                             window_days: int = 7) -> list[DuplicateSignal]:
    """Find credits that may duplicate ``txn``.

    Nothing is ever deleted automatically. A genuine duplicate payment and a
    legitimately repeated amount look identical without human context.
    """
    if txn.direction != Direction.CREDIT.value:
        return []

    lo = txn.txn_date - timedelta(days=window_days)
    hi = txn.txn_date + timedelta(days=window_days)

    others = (
        db.query(BankTransaction)
        .filter(BankTransaction.organization_id == txn.organization_id,
                BankTransaction.id != txn.id,
                BankTransaction.direction == Direction.CREDIT.value,
                BankTransaction.amount_paise == txn.amount_paise,
                BankTransaction.txn_date >= lo,
                BankTransaction.txn_date <= hi)
        .all()
    )

    signals: list[DuplicateSignal] = []
    for other in others:
        reasons, strength = ["Same amount"], 0.3

        if other.employee_id and other.employee_id == txn.employee_id:
            reasons.append("Same employee")
            strength += 0.2
        elif other.employee_id != txn.employee_id:
            # Two different people receiving the same amount is normal.
            continue

        if other.txn_date == txn.txn_date:
            reasons.append("Same date")
            strength += 0.2
        if txn.utr and other.utr and txn.utr.upper() == other.utr.upper():
            reasons.append(f"Identical transaction reference ({txn.utr})")
            strength += 0.45
        if txn.counterparty and other.counterparty == txn.counterparty:
            reasons.append("Same sender")
            strength += 0.1
        if (txn.narration_normalized and other.narration_normalized
                and txn.narration_normalized == other.narration_normalized):
            reasons.append("Identical narration")
            strength += 0.15

        # Same salary month allocated on both.
        txn_periods = {a.period_id for a in txn.allocations if a.period_id}
        other_periods = {a.period_id for a in other.allocations if a.period_id}
        if txn_periods and txn_periods & other_periods:
            reasons.append("Both are recorded against the same salary month")
            strength += 0.2

        if strength >= 0.5:
            signals.append(DuplicateSignal(other, reasons, min(1.0, round(strength, 3))))

    return sorted(signals, key=lambda s: -s.strength)


# ---------------------------------------------------------------------------
# Reversal detection (spec s15)
# ---------------------------------------------------------------------------
def link_reversal(db: Session, txn: BankTransaction, window_days: int = 45) -> Optional[BankTransaction]:
    """Link a debit that reverses an earlier credit.

    On success the original credit is marked reversed, its allocations are
    rejected, and the net effect on the salary period becomes zero.
    """
    if txn.direction != Direction.DEBIT.value:
        return None

    lo = txn.txn_date - timedelta(days=window_days)
    candidates = (
        db.query(BankTransaction)
        .filter(BankTransaction.organization_id == txn.organization_id,
                BankTransaction.direction == Direction.CREDIT.value,
                BankTransaction.amount_paise == txn.amount_paise,
                BankTransaction.txn_date >= lo,
                BankTransaction.txn_date <= txn.txn_date,
                BankTransaction.is_reversed.is_(False))
        .all()
    )
    if txn.employee_id:
        candidates = [c for c in candidates if c.employee_id == txn.employee_id]
    if not candidates:
        return None

    from ..services.textutils import looks_like_reversal

    explicit = looks_like_reversal(txn.description or "")
    same_utr = [c for c in candidates if txn.utr and c.utr and c.utr.upper() == txn.utr.upper()]

    original: Optional[BankTransaction] = None
    if same_utr:
        original = max(same_utr, key=lambda c: c.txn_date)
    elif explicit:
        original = max(candidates, key=lambda c: c.txn_date)
    if original is None:
        return None

    txn.reverses_transaction_id = original.id
    txn.classification = PaymentClassification.REVERSAL.value
    original.is_reversed = True
    original.status = TransactionStatus.REVERSED.value

    for allocation in original.allocations:
        if allocation.status != AllocationStatus.REJECTED.value:
            allocation.status = AllocationStatus.REJECTED.value
            allocation.reasoning = (
                (allocation.reasoning or "")
                + f" | Rejected automatically: the credit was reversed on {txn.txn_date.isoformat()}."
            ).strip()

    db.flush()
    return original
