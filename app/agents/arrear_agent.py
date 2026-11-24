"""Arrear ledger and retroactive rule adjustments (spec s18, s19, s56).

An arrear is money owed for an EARLIER period. This module keeps the ledger
the specification asks for -- original entitlement, previously received,
outstanding, settled, remaining -- and links a later payment back to the month
it actually settles.

Retroactive rate changes are handled by creating an *adjustment* plus a new
arrear. Historical entitlement rows are never edited (spec s19).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..enums import (
    AdjustmentKind,
    AllocationStatus,
    ArrearStatus,
    PaymentClassification,
    RuleStatus,
)
from ..models import (
    Adjustment,
    Arrear,
    Employee,
    Entitlement,
    PaymentAllocation,
    SalaryPeriod,
    SalaryRule,
)

AGENT_NAME = "arrear_agent"


@dataclass
class RetroImpact:
    """One period's difference under a retroactive rule change."""

    period: SalaryPeriod
    old_paise: int
    new_paise: int

    @property
    def delta_paise(self) -> int:
        return self.new_paise - self.old_paise


@dataclass
class RetroResult:
    impacts: list[RetroImpact] = field(default_factory=list)
    total_delta_paise: int = 0
    employees_affected: int = 0
    #: True when nothing was written -- simulation mode (spec s80, s81).
    simulated: bool = True


# ---------------------------------------------------------------------------
# Creating and maintaining arrears
# ---------------------------------------------------------------------------
def received_for_period(db: Session, employee: Employee, period: SalaryPeriod) -> int:
    rows = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == period.id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    return sum(r.amount_paise for r in rows)


def sync_for_period(
    db: Session,
    employee: Employee,
    period: SalaryPeriod,
    entitlement: Optional[Entitlement],
    credited_paise: int,
    documented_deduction_paise: int = 0,
) -> Optional[Arrear]:
    """Create, update or close the arrear for one employee-period.

    An authorised deduction is not an arrear: the employee was never owed that
    money in cash. Only the unexplained/unpaid remainder becomes outstanding.
    """
    if entitlement is None:
        return None

    expected_cash = entitlement.net_paise - documented_deduction_paise
    outstanding = expected_cash - credited_paise

    existing = (
        db.query(Arrear)
        .filter(Arrear.employee_id == employee.id, Arrear.origin_period_id == period.id,
                Arrear.reason.in_(["SHORT_CREDIT", "NOT_PAID"]))
        .first()
    )

    if outstanding <= settings.match_tolerance_paise:
        if existing and existing.status != ArrearStatus.SETTLED.value:
            if existing.settled_paise >= existing.outstanding_paise or outstanding <= 0:
                existing.status = ArrearStatus.SETTLED.value
                existing.outstanding_paise = max(0, outstanding)
                db.flush()
        return existing

    reason = "NOT_PAID" if credited_paise == 0 else "SHORT_CREDIT"

    if existing is None:
        existing = Arrear(
            employee_id=employee.id,
            origin_period_id=period.id,
            original_entitlement_paise=entitlement.net_paise,
            previously_received_paise=credited_paise,
            outstanding_paise=outstanding,
            settled_paise=0,
            reason=reason,
            status=ArrearStatus.OPEN.value,
            notes=(
                f"Expected {expected_cash / 100:.2f} in cash for {period.label}; "
                f"{credited_paise / 100:.2f} was credited."
            ),
        )
        db.add(existing)
    else:
        existing.original_entitlement_paise = entitlement.net_paise
        existing.previously_received_paise = credited_paise
        existing.outstanding_paise = outstanding
        existing.reason = reason
        if existing.settled_paise <= 0:
            existing.status = ArrearStatus.OPEN.value
        elif existing.remaining_paise <= 0:
            existing.status = ArrearStatus.SETTLED.value
        else:
            existing.status = ArrearStatus.PARTIALLY_SETTLED.value

    db.flush()
    return existing


def open_arrears(db: Session, employee: Employee) -> list[Arrear]:
    rows = (
        db.query(Arrear)
        .filter(Arrear.employee_id == employee.id,
                Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value]))
        .all()
    )
    return sorted(rows, key=lambda a: a.origin_period.sort_key if a.origin_period else 0)


def total_outstanding(db: Session, employee: Employee) -> int:
    return sum(a.remaining_paise for a in open_arrears(db, employee))


def open_arrears_for_ids(db: Session, employee_ids: list[int]) -> list[Arrear]:
    """Open arrears across a set of employees, for admin roll-ups."""
    if not employee_ids:
        return []
    return (
        db.query(Arrear)
        .filter(Arrear.employee_id.in_(employee_ids),
                Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value]))
        .all()
    )


def arrear_received_in_period(db: Session, employee: Employee, period: SalaryPeriod) -> int:
    """Arrear money that arrived *during* this period's credits.

    This is money for OTHER months, so reconciliation must not treat it as
    payment of the current month.
    """
    rows = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == period.id,
                PaymentAllocation.kind == PaymentClassification.ARREAR.value,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    return sum(r.amount_paise for r in rows)


# ---------------------------------------------------------------------------
# Retroactive rule change (spec s19)
# ---------------------------------------------------------------------------
def simulate_retroactive_change(
    db: Session,
    new_rule: SalaryRule,
    employees: Optional[list[Employee]] = None,
) -> RetroResult:
    """Compute what a rule would change, WITHOUT writing anything.

    Powers both the rule simulator (s80) and the what-if calculator (s81).
    """
    from . import entitlement_agent

    result = RetroResult(simulated=True)
    if employees is None:
        employees = (
            db.query(Employee)
            .filter(Employee.organization_id == new_rule.organization_id)
            .all()
        )

    periods = (
        db.query(SalaryPeriod)
        .filter(SalaryPeriod.organization_id == new_rule.organization_id)
        .all()
    )
    relevant = sorted(
        (p for p in periods if p.cycle_end >= new_rule.effective_from
         and (new_rule.effective_to is None or p.cycle_start <= new_rule.effective_to)),
        key=lambda p: p.sort_key,
    )

    affected: set[int] = set()
    original_status = new_rule.status
    try:
        # Temporarily treat the rule as active so resolution can see it, then
        # roll the change back. Nothing is committed.
        new_rule.status = RuleStatus.ACTIVE.value
        db.flush()

        for employee in employees:
            for period in relevant:
                current = (
                    db.query(Entitlement)
                    .filter(Entitlement.employee_id == employee.id,
                            Entitlement.period_id == period.id,
                            Entitlement.is_current.is_(True))
                    .first()
                )
                old_paise = current.net_paise if current else 0

                recomputed = entitlement_agent.calculate(db, employee, period)
                if not recomputed.ok:
                    continue
                if recomputed.net_paise == old_paise:
                    continue

                result.impacts.append(RetroImpact(period, old_paise, recomputed.net_paise))
                result.total_delta_paise += recomputed.net_paise - old_paise
                affected.add(employee.id)
    finally:
        new_rule.status = original_status
        db.flush()

    result.employees_affected = len(affected)
    return result


def apply_retroactive_change(
    db: Session,
    employee: Employee,
    period: SalaryPeriod,
    new_entitlement: Entitlement,
    old_entitlement: Optional[Entitlement],
    reason: str,
    created_by_id: Optional[int] = None,
) -> tuple[Adjustment, Optional[Arrear]]:
    """Record a retroactive increase as an adjustment plus an arrear.

    The historical entitlement is preserved: ``new_entitlement`` supersedes it
    via the entitlement chain, and the difference becomes money owed.
    """
    old_paise = old_entitlement.net_paise if old_entitlement else 0
    delta = new_entitlement.net_paise - old_paise

    adjustment = Adjustment(
        employee_id=employee.id,
        period_id=period.id,
        kind=AdjustmentKind.RETROACTIVE_RULE.value,
        old_value_paise=old_paise,
        new_value_paise=new_entitlement.net_paise,
        delta_paise=delta,
        reason=reason,
        old_rule_id=old_entitlement.rule_id if old_entitlement else None,
        new_rule_id=new_entitlement.rule_id,
        created_by_id=created_by_id,
    )
    db.add(adjustment)
    db.flush()

    if delta <= 0:
        return adjustment, None

    already_received = received_for_period(db, employee, period)
    outstanding = new_entitlement.net_paise - already_received
    if outstanding <= 0:
        return adjustment, None

    arrear = (
        db.query(Arrear)
        .filter(Arrear.employee_id == employee.id,
                Arrear.origin_period_id == period.id,
                Arrear.reason == "RETROACTIVE_RULE")
        .first()
    )
    if arrear is None:
        arrear = Arrear(
            employee_id=employee.id,
            origin_period_id=period.id,
            original_entitlement_paise=new_entitlement.net_paise,
            previously_received_paise=already_received,
            outstanding_paise=outstanding,
            reason="RETROACTIVE_RULE",
            status=ArrearStatus.OPEN.value,
            adjustment_id=adjustment.id,
            notes=(
                f"A revised rate was applied to {period.label}. Previously "
                f"{old_paise / 100:.2f}, now {new_entitlement.net_paise / 100:.2f}."
            ),
        )
        db.add(arrear)
    else:
        arrear.original_entitlement_paise = new_entitlement.net_paise
        arrear.previously_received_paise = already_received
        arrear.outstanding_paise = outstanding
        arrear.adjustment_id = adjustment.id

    db.flush()
    return adjustment, arrear
