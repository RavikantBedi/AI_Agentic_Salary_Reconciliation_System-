"""AGENT 3 - Salary Entitlement calculation (spec s5, s19, s20, s21, s91).

Computes what an employee *should* receive for a period, and -- just as
importantly -- records exactly how that number was reached, component by
component, with the rule and government order behind each line.

Both figures the specification demands are preserved:
    GROSS ENTITLEMENT  and  NET EXPECTED CREDIT.

Recalculation never edits history. A new :class:`Entitlement` row supersedes
the previous one, which stays queryable (spec s19, s56).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy.orm import Session

from ..db import utcnow
from ..enums import ComponentKind, FundingSource, ProrationPolicy, RecordState, VerificationLevel
from ..models import (
    AttendanceRecord,
    Employee,
    Entitlement,
    EntitlementComponent,
    SalaryPeriod,
    SalaryRule,
    SalaryRuleComponent,
)
from ..services.textutils import days_in_month
from . import rule_agent

AGENT_NAME = "entitlement_agent"


@dataclass
class ComputedComponent:
    code: str
    label_en: str
    label_hi: Optional[str]
    kind: str
    funding_source: Optional[str]
    amount_paise: int
    calculation_note: str
    source_reference: Optional[str] = None
    sort_order: int = 0


@dataclass
class EntitlementResult:
    """A computed entitlement, not yet persisted."""

    employee_id: int
    period_id: int
    rule: Optional[SalaryRule]
    gross_paise: int = 0
    deductions_paise: int = 0
    net_paise: int = 0
    central_paise: int = 0
    state_paise: int = 0
    employer_paise: int = 0
    other_paise: int = 0
    proration_factor: float = 1.0
    components: list[ComputedComponent] = field(default_factory=list)
    calculation: dict = field(default_factory=dict)
    basis_note: str = ""
    problem: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.problem is None


# ---------------------------------------------------------------------------
# Service days and proration
# ---------------------------------------------------------------------------
def service_window(employee: Employee, start: date, end: date) -> tuple[Optional[date], Optional[date]]:
    """The portion of ``start..end`` the employee was actually in service.

    Returns ``(None, None)`` when the employee had not joined yet or had
    already left -- there is no entitlement in that case (spec s21).
    """
    win_start, win_end = start, end
    if employee.joining_date and employee.joining_date > win_start:
        win_start = employee.joining_date
    if employee.relieving_date and employee.relieving_date < win_end:
        win_end = employee.relieving_date
    if win_start > win_end:
        return None, None
    return win_start, win_end


def compute_proration(
    rule: SalaryRule,
    period: SalaryPeriod,
    seg_start: date,
    seg_end: date,
    served_start: date,
    served_end: date,
    attendance: Optional[AttendanceRecord],
) -> tuple[float, str]:
    """Return ``(factor, explanation)`` for a partial period.

    The proration policy comes from the RULE, never assumed (spec s20).
    """
    policy = rule.proration_policy or ProrationPolicy.NONE.value
    total_days = (seg_end - seg_start).days + 1
    served_days = (served_end - served_start).days + 1

    if policy == ProrationPolicy.NONE.value:
        return 1.0, "The applicable rule pays the full amount without proration."

    if policy == ProrationPolicy.FULL_MONTH_IF_ANY_SERVICE.value:
        return 1.0, "The applicable rule pays a full month when there is any service in the period."

    if policy == ProrationPolicy.ATTENDANCE_DAYS.value:
        if attendance is None or not attendance.working_days:
            return 1.0, (
                "This rule prorates by attendance, but no attendance record was found "
                "for this period. The full amount was used and this needs confirmation."
            )
        present = attendance.present_days or 0
        paid = present + (attendance.paid_leave_days or 0)
        factor = min(1.0, paid / attendance.working_days) if attendance.working_days else 1.0
        return factor, (
            f"Prorated by attendance: {paid} paid days out of {attendance.working_days} working days."
        )

    # CALENDAR_DAYS
    month_days = days_in_month(period.year, period.month)
    if served_days >= total_days and total_days >= month_days:
        return 1.0, "Full period served."
    factor = served_days / month_days if month_days else 1.0
    return min(1.0, factor), (
        f"Prorated by calendar days: {served_days} day(s) served out of {month_days} in the month."
    )


# ---------------------------------------------------------------------------
# Component evaluation
# ---------------------------------------------------------------------------
def _condition_met(component: SalaryRuleComponent, employee: Employee,
                   attendance: Optional[AttendanceRecord]) -> tuple[bool, str]:
    """Evaluate a component's eligibility condition (no-code builder output)."""
    condition = component.condition_json or {}
    if not condition:
        return True, ""

    min_present = condition.get("min_present_days")
    if min_present is not None:
        if attendance is None or attendance.present_days is None:
            return False, f"Requires at least {min_present} present days, but no attendance record exists."
        if attendance.present_days < int(min_present):
            return False, (
                f"Requires at least {min_present} present days; "
                f"{attendance.present_days} recorded."
            )

    for field_name, expected in condition.get("employee_equals", {}).items():
        actual = getattr(employee, field_name, None)
        if str(actual or "").strip().lower() != str(expected or "").strip().lower():
            return False, f"Requires {field_name} = {expected}."

    return True, ""


def _apply_factor(amount_paise: int, factor: float) -> int:
    """Apply a proration factor with half-up rounding to whole paise."""
    if factor >= 1.0:
        return int(amount_paise)
    return int(
        (Decimal(int(amount_paise)) * Decimal(str(factor))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _evaluate_segment(
    segment: rule_agent.RuleSegment,
    employee: Employee,
    period: SalaryPeriod,
    attendance: Optional[AttendanceRecord],
    weight: float,
) -> tuple[list[ComputedComponent], float, list[str]]:
    """Evaluate every component of one rule segment.

    ``weight`` scales the segment's contribution when a month was split
    between two rules.
    """
    rule = segment.rule
    served_start, served_end = service_window(employee, segment.start, segment.end)
    notes: list[str] = []

    if served_start is None:
        notes.append(
            f"{rule.label}: the employee was not in service between "
            f"{segment.start.isoformat()} and {segment.end.isoformat()}."
        )
        return [], 0.0, notes

    factor, proration_note = compute_proration(
        rule, period, segment.start, segment.end, served_start, served_end, attendance
    )
    notes.append(proration_note)
    effective = factor * weight

    source_ref = None
    if rule.government_order:
        go = rule.government_order
        source_ref = f"{go.order_number or go.title}"
        if go.order_date:
            source_ref += f" dated {go.order_date.isoformat()}"
    elif rule.source_note:
        source_ref = rule.source_note[:200]

    computed: list[ComputedComponent] = []
    running_gross = 0

    # --- Pass 1: fixed / per-day / per-unit components ---
    for comp in sorted(rule.components, key=lambda c: (c.sort_order, c.id)):
        if comp.calc_method == "PERCENT_OF":
            continue

        eligible, why_not = _condition_met(comp, employee, attendance)
        if not eligible:
            notes.append(f"{comp.label_en} was not applied: {why_not}")
            continue

        base_amount = int(comp.amount_paise or 0)
        note_parts: list[str] = []

        if comp.calc_method == "PER_DAY":
            days = None
            if attendance and attendance.present_days is not None:
                days = attendance.present_days
            else:
                days = (served_end - served_start).days + 1
                note_parts.append("no attendance record; used days in service")
            base_amount = base_amount * int(days)
            note_parts.append(f"{comp.amount_paise / 100:.2f} x {days} day(s)")
        elif comp.calc_method == "PER_UNIT":
            units = int((comp.condition_json or {}).get("units", 1))
            base_amount = base_amount * units
            note_parts.append(f"{comp.amount_paise / 100:.2f} x {units} unit(s)")

        amount = _apply_factor(base_amount, effective if comp.is_prorated else weight)
        if comp.is_prorated and factor < 1.0:
            note_parts.append(f"prorated x{factor:.4f}")
        if weight < 1.0:
            note_parts.append(f"segment share x{weight:.4f}")

        if amount == 0 and base_amount == 0:
            continue

        computed.append(ComputedComponent(
            code=comp.code,
            label_en=comp.label_en,
            label_hi=comp.label_hi,
            kind=comp.kind,
            funding_source=comp.funding_source,
            amount_paise=amount,
            calculation_note="; ".join(note_parts) or f"Fixed amount from {rule.label}",
            source_reference=comp.source_note or source_ref,
            sort_order=comp.sort_order,
        ))
        if comp.kind != ComponentKind.DEDUCTION.value:
            running_gross += amount

    # --- Pass 2: percentage components, which need the gross ---
    by_code = {c.code: c.amount_paise for c in computed}
    for comp in sorted(rule.components, key=lambda c: (c.sort_order, c.id)):
        if comp.calc_method != "PERCENT_OF":
            continue
        eligible, why_not = _condition_met(comp, employee, attendance)
        if not eligible:
            notes.append(f"{comp.label_en} was not applied: {why_not}")
            continue

        basis_code = (comp.percent_of_code or "GROSS").upper()
        basis_amount = running_gross if basis_code == "GROSS" else int(by_code.get(comp.percent_of_code, 0))
        percent = Decimal(str(comp.percent_value or 0))
        amount = int((Decimal(basis_amount) * percent / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        if amount == 0:
            continue

        computed.append(ComputedComponent(
            code=comp.code,
            label_en=comp.label_en,
            label_hi=comp.label_hi,
            kind=comp.kind,
            funding_source=comp.funding_source,
            amount_paise=amount,
            calculation_note=f"{percent}% of {basis_code.lower()} ({basis_amount / 100:.2f})",
            source_reference=comp.source_note or source_ref,
            sort_order=comp.sort_order + 100,
        ))

    return computed, factor, notes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def calculate(db: Session, employee: Employee, period: SalaryPeriod) -> EntitlementResult:
    """Compute the entitlement for one employee-period.

    Never raises for business reasons: an unresolvable rule produces a result
    with ``problem`` set, which the reconciliation engine renders as
    "No salary rule found" rather than inventing a number (spec s52).
    """
    result = EntitlementResult(employee_id=employee.id, period_id=period.id, rule=None)

    resolution = rule_agent.resolve_for_period(db, employee, period)
    if not resolution.resolved:
        result.problem = resolution.problem or "The applicable salary rule could not be determined."
        result.calculation = {
            "status": "UNRESOLVED",
            "reasons": resolution.reasons,
            "ambiguous_rules": [r.label for r in resolution.ambiguous_rules],
        }
        return result

    served_start, served_end = service_window(employee, period.cycle_start, period.cycle_end)
    if served_start is None:
        result.problem = (
            "The employee was not in service during this period, so no amount is due."
        )
        result.calculation = {"status": "NOT_IN_SERVICE"}
        result.rule = resolution.primary_rule
        return result

    attendance = (
        db.query(AttendanceRecord)
        .filter(AttendanceRecord.employee_id == employee.id,
                AttendanceRecord.period_id == period.id)
        .first()
    )

    total_days = (period.cycle_end - period.cycle_start).days + 1
    all_components: list[ComputedComponent] = []
    all_notes: list[str] = list(resolution.reasons)
    factors: list[float] = []

    for segment in resolution.segments:
        weight = (segment.days / total_days) if resolution.is_split else 1.0
        comps, factor, notes = _evaluate_segment(segment, employee, period, attendance, weight)
        all_components.extend(comps)
        all_notes.extend(notes)
        factors.append(factor)

    if not all_components:
        result.problem = (
            "The applicable rule produced no payable component for this period. "
            "Please check the rule configuration."
        )
        result.rule = resolution.primary_rule
        result.calculation = {"status": "NO_COMPONENTS", "notes": all_notes}
        return result

    # --- Merge components sharing a code across split segments ---
    merged: dict[str, ComputedComponent] = {}
    for comp in all_components:
        if comp.code in merged:
            existing = merged[comp.code]
            existing.amount_paise += comp.amount_paise
            existing.calculation_note += f" + {comp.calculation_note}"
        else:
            merged[comp.code] = ComputedComponent(**comp.__dict__)
    components = sorted(merged.values(), key=lambda c: (c.sort_order, c.code))

    gross = sum(c.amount_paise for c in components if c.kind != ComponentKind.DEDUCTION.value)
    deductions = sum(c.amount_paise for c in components if c.kind == ComponentKind.DEDUCTION.value)

    funding = {FundingSource.CENTRAL.value: 0, FundingSource.STATE.value: 0,
               FundingSource.EMPLOYER.value: 0, FundingSource.OTHER.value: 0}
    for comp in components:
        if comp.kind == ComponentKind.DEDUCTION.value:
            continue
        key = comp.funding_source if comp.funding_source in funding else FundingSource.OTHER.value
        funding[key] += comp.amount_paise

    result.rule = resolution.primary_rule
    result.components = components
    result.gross_paise = gross
    result.deductions_paise = deductions
    result.net_paise = gross - deductions
    result.central_paise = funding[FundingSource.CENTRAL.value]
    result.state_paise = funding[FundingSource.STATE.value]
    result.employer_paise = funding[FundingSource.EMPLOYER.value]
    result.other_paise = funding[FundingSource.OTHER.value]
    result.proration_factor = round(sum(factors) / len(factors), 6) if factors else 1.0

    rule_labels = " + ".join(sorted({s.rule.label for s in resolution.segments}))
    result.basis_note = f"Calculated using {rule_labels}."
    result.calculation = {
        "status": "OK",
        "rules": [
            {
                "rule": s.rule.label,
                "rule_uid": s.rule.uid,
                "from": s.start.isoformat(),
                "to": s.end.isoformat(),
                "days": s.days,
                "government_order": (
                    s.rule.government_order.order_number if s.rule.government_order else None
                ),
            }
            for s in resolution.segments
        ],
        "split_month": resolution.is_split,
        "service_from": served_start.isoformat(),
        "service_to": served_end.isoformat(),
        "proration_factor": result.proration_factor,
        "attendance_present_days": attendance.present_days if attendance else None,
        "notes": all_notes,
        "gross_paise": gross,
        "deductions_paise": deductions,
        "net_paise": result.net_paise,
    }
    return result


def persist(db: Session, result: EntitlementResult) -> Optional[Entitlement]:
    """Store a computed entitlement, superseding any previous current row.

    History is preserved: the old row is retained with ``is_current = False``
    so an auditor can always see what was calculated before (spec s56).
    """
    if not result.ok:
        return None

    previous = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == result.employee_id,
                Entitlement.period_id == result.period_id,
                Entitlement.is_current.is_(True))
        .first()
    )

    # No change: keep the existing row so timestamps stay meaningful.
    if previous and _unchanged(previous, result):
        return previous

    entitlement = Entitlement(
        employee_id=result.employee_id,
        period_id=result.period_id,
        rule_id=result.rule.id if result.rule else None,
        gross_paise=result.gross_paise,
        deductions_paise=result.deductions_paise,
        net_paise=result.net_paise,
        central_paise=result.central_paise,
        state_paise=result.state_paise,
        employer_paise=result.employer_paise,
        other_paise=result.other_paise,
        proration_factor=result.proration_factor,
        calculation_json=result.calculation,
        basis_note=result.basis_note,
        is_current=True,
        supersedes_id=previous.id if previous else None,
        state=RecordState.RECONCILED.value,
        computed_at=utcnow(),
    )
    db.add(entitlement)
    db.flush()

    for order, comp in enumerate(result.components):
        db.add(EntitlementComponent(
            entitlement_id=entitlement.id,
            code=comp.code,
            label_en=comp.label_en,
            label_hi=comp.label_hi,
            kind=comp.kind,
            funding_source=comp.funding_source,
            amount_paise=comp.amount_paise,
            calculation_note=comp.calculation_note,
            source_reference=comp.source_reference,
            sort_order=order,
        ))

    if previous:
        previous.is_current = False
    db.flush()
    return entitlement


def _unchanged(previous: Entitlement, result: EntitlementResult) -> bool:
    return (
        previous.gross_paise == result.gross_paise
        and previous.deductions_paise == result.deductions_paise
        and previous.net_paise == result.net_paise
        and previous.rule_id == (result.rule.id if result.rule else None)
        and abs(previous.proration_factor - result.proration_factor) < 1e-9
    )


def calculate_and_store(db: Session, employee: Employee, period: SalaryPeriod) -> tuple[EntitlementResult, Optional[Entitlement]]:
    result = calculate(db, employee, period)
    return result, persist(db, result)


def entitlement_verification_level(entitlement: Optional[Entitlement]) -> int:
    """How well-evidenced the *basis* of an entitlement is (spec s28).

    A rule backed by an uploaded government order is stronger evidence than
    one an administrator typed in from memory.
    """
    if entitlement is None or entitlement.rule is None:
        return VerificationLevel.USER_CLAIM
    rule = entitlement.rule
    if rule.government_order and rule.government_order.document_id:
        return VerificationLevel.OFFICIAL_DOCUMENT
    if rule.government_order:
        return VerificationLevel.USER_CLAIM + 1
    return VerificationLevel.USER_CLAIM
