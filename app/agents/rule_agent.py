"""AGENT 2 - Salary Rule Intelligence (spec s4, s20, s54, s55, s79, s80).

Responsibilities
----------------
* Resolve which versioned rule applies to a given employee for a given period.
* Score rule specificity so the most precisely scoped rule wins.
* Detect *ambiguity* (two equally specific rules) and *conflict* (two rules
  disagreeing on a rate) and refuse to choose silently -- a human resolves it.
* Handle a rate that changes mid-month, splitting the month only when the
  rule explicitly permits it.
* Supersede an older rule when a new one takes effect, without ever rewriting
  historical calculations.

Nothing in this module hardcodes a salary amount. Rules are data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..db import utcnow
from ..enums import RuleStatus
from ..models import (
    Employee,
    KnowledgeConflict,
    SalaryPeriod,
    SalaryRule,
)

AGENT_NAME = "rule_agent"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class RuleSegment:
    """A rule applying to a contiguous slice of a period.

    A period normally has exactly one segment. It has two when a rate changes
    mid-month AND the rule allows the month to be split (spec s20).
    """

    rule: SalaryRule
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class RuleResolution:
    """Outcome of resolving rules for one employee-period."""

    segments: list[RuleSegment] = field(default_factory=list)
    #: Rules that scored equally -- the system must not pick one on its own.
    ambiguous_rules: list[SalaryRule] = field(default_factory=list)
    #: Human-readable reasons, surfaced in the explanation tree.
    reasons: list[str] = field(default_factory=list)
    problem: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return bool(self.segments) and not self.ambiguous_rules and self.problem is None

    @property
    def primary_rule(self) -> Optional[SalaryRule]:
        return self.segments[0].rule if self.segments else None

    @property
    def is_split(self) -> bool:
        return len(self.segments) > 1


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------
def _employee_location_chain(db: Session, employee: Employee) -> set[int]:
    """Location ids of the employee's posting and all its ancestors.

    A rule scoped to a district must apply to an employee posted at a centre
    inside that district.
    """
    if not employee.location_id:
        return set()
    from ..models import Location

    ids: set[int] = set()
    node = db.get(Location, employee.location_id)
    guard = 0
    while node is not None and guard < 20:
        ids.add(node.id)
        node = node.parent
        guard += 1
    return ids


def rule_matches(db: Session, rule: SalaryRule, employee: Employee,
                 location_chain: set[int] | None = None) -> tuple[bool, int, list[str]]:
    """Does ``rule`` apply to ``employee``?

    Returns ``(matches, specificity, reasons)``. Specificity counts how many
    scope dimensions the rule pins down; a rule that names the designation AND
    the district is more specific than one that names only the state.

    A ``NULL`` scope column means "any value", which is how a state-wide
    default coexists with a district-specific override.
    """
    reasons: list[str] = []
    specificity = 0

    if rule.scope_state:
        org_state = employee.organization.state if employee.organization else None
        if org_state and rule.scope_state.strip().lower() != org_state.strip().lower():
            return False, 0, [f"State {rule.scope_state} does not match {org_state}"]
        specificity += 1
        reasons.append(f"State = {rule.scope_state}")

    if rule.scope_designation_id:
        if employee.designation_id != rule.scope_designation_id:
            return False, 0, ["Designation does not match"]
        specificity += 2
        reasons.append(f"Designation = {employee.designation.name if employee.designation else ''}")

    if rule.scope_category:
        emp_category = employee.designation.category if employee.designation else None
        if not emp_category or emp_category.strip().lower() != rule.scope_category.strip().lower():
            return False, 0, ["Worker category does not match"]
        specificity += 2
        reasons.append(f"Category = {rule.scope_category}")

    if rule.scope_department_id:
        if employee.department_id != rule.scope_department_id:
            return False, 0, ["Department does not match"]
        specificity += 1
        reasons.append(f"Department = {employee.department.name if employee.department else ''}")

    if rule.scope_scheme_id:
        if employee.scheme_id != rule.scope_scheme_id:
            return False, 0, ["Scheme does not match"]
        specificity += 1
        reasons.append(f"Scheme = {employee.scheme.name if employee.scheme else ''}")

    if rule.scope_location_id:
        chain = location_chain if location_chain is not None else _employee_location_chain(db, employee)
        if rule.scope_location_id not in chain:
            return False, 0, ["Location is outside this rule's area"]
        specificity += 2
        reasons.append("Location is within the rule's area")

    # No-code builder extras: simple equality conditions on employee fields.
    for key, expected in (rule.scope_extra_json or {}).items():
        actual = getattr(employee, key, None)
        if str(actual or "").strip().lower() != str(expected or "").strip().lower():
            return False, 0, [f"Condition {key} = {expected} not met"]
        specificity += 1
        reasons.append(f"{key} = {expected}")

    if not reasons:
        reasons.append("Applies to all employees in this organisation")

    return True, specificity, reasons


def _overlaps(rule: SalaryRule, start: date, end: date) -> bool:
    if rule.effective_from > end:
        return False
    if rule.effective_to and rule.effective_to < start:
        return False
    return True


def candidate_rules(db: Session, employee: Employee, start: date, end: date) -> list[SalaryRule]:
    """Active rules whose validity window overlaps the given date range."""
    rules = (
        db.query(SalaryRule)
        .filter(
            SalaryRule.organization_id == employee.organization_id,
            SalaryRule.status == RuleStatus.ACTIVE.value,
        )
        .all()
    )
    return [r for r in rules if _overlaps(r, start, end)]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve_for_range(db: Session, employee: Employee, start: date, end: date) -> RuleResolution:
    """Resolve the applicable rule(s) across a date range."""
    resolution = RuleResolution()
    chain = _employee_location_chain(db, employee)

    matched: list[tuple[SalaryRule, int, list[str]]] = []
    for rule in candidate_rules(db, employee, start, end):
        ok, specificity, reasons = rule_matches(db, rule, employee, chain)
        if ok:
            matched.append((rule, specificity, reasons))

    if not matched:
        resolution.problem = (
            "No active salary rule was found for this employee and period. "
            "An administrator needs to configure a rule, backed by the relevant "
            "government order, before an expected amount can be calculated."
        )
        return resolution

    # --- Build the timeline of distinct rate windows within the range ---
    boundaries = {start, end}
    for rule, _, _ in matched:
        if start < rule.effective_from <= end:
            boundaries.add(rule.effective_from)
        if rule.effective_to and start <= rule.effective_to < end:
            from datetime import timedelta

            boundaries.add(rule.effective_to + timedelta(days=1))
    ordered = sorted(b for b in boundaries if start <= b <= end)

    from datetime import timedelta

    segments: list[RuleSegment] = []
    for idx, seg_start in enumerate(ordered):
        seg_end = (ordered[idx + 1] - timedelta(days=1)) if idx + 1 < len(ordered) else end
        if seg_start > seg_end:
            continue

        applicable = [
            (rule, spec, reasons)
            for rule, spec, reasons in matched
            if rule.effective_from <= seg_start and (rule.effective_to is None or rule.effective_to >= seg_end)
        ]
        if not applicable:
            continue

        best_spec = max(spec for _, spec, _ in applicable)
        finalists = [item for item in applicable if item[1] == best_spec]

        if len(finalists) > 1:
            # Tie-break on the later effective date: a newer order at the same
            # specificity supersedes an older one.
            latest = max(r.effective_from for r, _, _ in finalists)
            newest = [item for item in finalists if item[0].effective_from == latest]
            if len(newest) > 1:
                # Genuinely ambiguous -- refuse to guess (spec s55).
                resolution.ambiguous_rules = [r for r, _, _ in newest]
                resolution.problem = (
                    "More than one salary rule applies equally to this employee for "
                    f"{seg_start.isoformat()}. An administrator must resolve which rule is correct."
                )
                return resolution
            finalists = newest

        rule, _, reasons = finalists[0]
        segments.append(RuleSegment(rule=rule, start=seg_start, end=seg_end))
        resolution.reasons.extend(
            [f"{seg_start.isoformat()} to {seg_end.isoformat()}: {rule.label} ({'; '.join(reasons)})"]
        )

    if not segments:
        resolution.problem = "No salary rule covers the whole of this period."
        return resolution

    # --- Mid-month change handling (spec s20) ---
    if len(segments) > 1:
        distinct_rules = {s.rule.id for s in segments}
        if len(distinct_rules) == 1:
            merged = RuleSegment(rule=segments[0].rule, start=segments[0].start, end=segments[-1].end)
            segments = [merged]
        elif not all(s.rule.allow_mid_month_split for s in segments):
            # The rate changed mid-period but no rule permits splitting the
            # month. The government order decides, so we apply the rule in
            # force at the START of the period and say so explicitly.
            chosen = segments[0]
            resolution.reasons.append(
                "A rate change occurred inside this period, but the applicable rule does not "
                "permit splitting a month. The rate in force at the start of the period was applied."
            )
            segments = [RuleSegment(rule=chosen.rule, start=segments[0].start, end=segments[-1].end)]
        else:
            resolution.reasons.append(
                "A rate change occurred inside this period and the rule permits a mid-month split, "
                "so the period was calculated in parts."
            )

    resolution.segments = segments
    return resolution


def resolve_for_period(db: Session, employee: Employee, period: SalaryPeriod) -> RuleResolution:
    """Resolve the rule(s) for one salary period."""
    return resolve_for_range(db, employee, period.cycle_start, period.cycle_end)


# ---------------------------------------------------------------------------
# Conflict detection (spec s55)
# ---------------------------------------------------------------------------
def detect_conflicts(db: Session, organization_id: int) -> list[KnowledgeConflict]:
    """Flag active rules that overlap in scope AND time but differ in value.

    The system never silently picks a winner; it records the disagreement for
    an administrator to resolve.
    """
    rules = (
        db.query(SalaryRule)
        .filter(SalaryRule.organization_id == organization_id,
                SalaryRule.status == RuleStatus.ACTIVE.value)
        .all()
    )

    created: list[KnowledgeConflict] = []
    for i, rule_a in enumerate(rules):
        for rule_b in rules[i + 1:]:
            if not _same_scope(rule_a, rule_b):
                continue
            if not _time_overlap(rule_a, rule_b):
                continue
            total_a, total_b = _rule_total_paise(rule_a), _rule_total_paise(rule_b)
            if total_a == total_b:
                continue

            subject = f"Conflicting salary rules: {rule_a.rule_code} vs {rule_b.rule_code}"
            existing = (
                db.query(KnowledgeConflict)
                .filter(KnowledgeConflict.organization_id == organization_id,
                        KnowledgeConflict.subject == subject,
                        KnowledgeConflict.status == "OPEN")
                .first()
            )
            if existing:
                continue

            conflict = KnowledgeConflict(
                organization_id=organization_id,
                subject=subject,
                rule_a_id=rule_a.id,
                rule_b_id=rule_b.id,
                document_a_id=rule_a.government_order.document_id if rule_a.government_order else None,
                document_b_id=rule_b.government_order.document_id if rule_b.government_order else None,
                value_a=f"{total_a / 100:.2f}",
                value_b=f"{total_b / 100:.2f}",
                detail=(
                    f"{rule_a.label} states a gross of {total_a / 100:.2f} while "
                    f"{rule_b.label} states {total_b / 100:.2f} for an overlapping scope and period. "
                    "The system has not chosen between them."
                ),
                status="OPEN",
            )
            db.add(conflict)
            created.append(conflict)

    if created:
        db.flush()
    return created


def _same_scope(a: SalaryRule, b: SalaryRule) -> bool:
    fields = ("scope_state", "scope_location_id", "scope_department_id",
              "scope_scheme_id", "scope_designation_id", "scope_category")
    return all(getattr(a, f) == getattr(b, f) for f in fields)


def _time_overlap(a: SalaryRule, b: SalaryRule) -> bool:
    a_end = a.effective_to or date(2999, 12, 31)
    b_end = b.effective_to or date(2999, 12, 31)
    return a.effective_from <= b_end and b.effective_from <= a_end


def _rule_total_paise(rule: SalaryRule) -> int:
    """Gross of the fixed components -- an indicative comparison value."""
    from ..enums import ComponentKind

    return sum(
        c.amount_paise for c in rule.components
        if c.kind != ComponentKind.DEDUCTION.value and c.calc_method == "FIXED"
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def supersede(db: Session, old_rule: SalaryRule, new_rule: SalaryRule) -> None:
    """Mark ``old_rule`` superseded from the day before ``new_rule`` starts.

    Historical calculations are NOT touched (spec s54). Any resulting
    difference is expressed later as an Adjustment/Arrear, never as an edit.
    """
    from datetime import timedelta

    old_rule.status = RuleStatus.SUPERSEDED.value
    old_rule.superseded_by_id = new_rule.id
    if old_rule.effective_to is None or old_rule.effective_to >= new_rule.effective_from:
        old_rule.effective_to = new_rule.effective_from - timedelta(days=1)
    db.flush()


def activate(db: Session, rule: SalaryRule, approver_id: Optional[int] = None) -> list[SalaryRule]:
    """Activate an approved rule and supersede the rules it replaces.

    Returns the list of rules that were superseded.
    """
    rule.status = RuleStatus.ACTIVE.value
    rule.approved_by_id = approver_id
    rule.approved_at = utcnow()
    db.flush()

    superseded: list[SalaryRule] = []
    others = (
        db.query(SalaryRule)
        .filter(SalaryRule.organization_id == rule.organization_id,
                SalaryRule.status == RuleStatus.ACTIVE.value,
                SalaryRule.id != rule.id)
        .all()
    )
    for other in others:
        if _same_scope(other, rule) and other.effective_from < rule.effective_from:
            supersede(db, other, rule)
            superseded.append(other)
    return superseded
