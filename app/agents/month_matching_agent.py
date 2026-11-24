"""AGENT 6 - Salary Month Matching (spec s8, s9, s10, s11).

The single most important idea in this module: **a payment date is not the
salary month**. A credit landing on 18 April carrying "MARCH HONORARIUM" is
March's salary, and must be recorded against March.

What this agent does
--------------------
* Reads an explicit month from the narration when one is present.
* Otherwise builds a *probability distribution* over candidate months from the
  organisation's pay cycle, the employee's unpaid periods, historical
  behaviour and learned corrections -- and never collapses that distribution
  into a single answer on its own (spec s9).
* Detects when one credit covers several months and proposes a multi-period
  allocation whose parts sum exactly to the transaction amount (spec s10).

Every candidate carries the reasons behind its probability, so the user always
sees why a month was suggested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..enums import AllocationStatus, PaymentClassification
from ..models import (
    BankTransaction,
    CorrectionPattern,
    Employee,
    Entitlement,
    MonthCandidate,
    PaymentAllocation,
    SalaryPeriod,
)
from ..services.textutils import add_months, extract_month_references, looks_like_arrear

AGENT_NAME = "month_matching_agent"


@dataclass
class Candidate:
    period: SalaryPeriod
    probability: float
    reasons: list[str] = field(default_factory=list)

    def add(self, weight: float, reason: str) -> None:
        self.probability += weight
        self.reasons.append(reason)


@dataclass
class MonthMatchResult:
    candidates: list[Candidate] = field(default_factory=list)
    #: True when the narration named the month outright.
    explicit: bool = False
    #: Proposed split when one credit appears to cover several months.
    multi_period: list[tuple[SalaryPeriod, int, str]] = field(default_factory=list)
    note: str = ""

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def confident(self) -> bool:
        """Confident only when clearly ahead of the runner-up."""
        if not self.candidates:
            return False
        if self.explicit:
            return True
        top = self.candidates[0].probability
        second = self.candidates[1].probability if len(self.candidates) > 1 else 0.0
        return top >= 0.6 and (top - second) >= 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _periods_in_window(db: Session, organization_id: int, centre: date,
                       back: int = 14, forward: int = 2) -> list[SalaryPeriod]:
    """Salary periods plausibly related to a credit on ``centre``."""
    start = add_months(centre, -back)
    end = add_months(centre, forward)
    lo = start.year * 100 + start.month
    hi = end.year * 100 + end.month
    periods = (
        db.query(SalaryPeriod)
        .filter(SalaryPeriod.organization_id == organization_id)
        .all()
    )
    return sorted((p for p in periods if lo <= p.sort_key <= hi), key=lambda p: p.sort_key)


def expected_credit_month(period: SalaryPeriod, lag_months: int) -> tuple[int, int]:
    """The (year, month) in which this period's salary is normally credited."""
    anchor = add_months(date(period.year, period.month, 1), lag_months)
    return anchor.year, anchor.month


def _unpaid_periods(db: Session, employee: Employee,
                    periods: list[SalaryPeriod]) -> dict[int, int]:
    """Outstanding paise per period id (entitled minus already allocated)."""
    outstanding: dict[int, int] = {}
    for period in periods:
        entitlement = (
            db.query(Entitlement)
            .filter(Entitlement.employee_id == employee.id,
                    Entitlement.period_id == period.id,
                    Entitlement.is_current.is_(True))
            .first()
        )
        if entitlement is None or entitlement.net_paise <= 0:
            continue
        allocated = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.employee_id == employee.id,
                    PaymentAllocation.period_id == period.id,
                    PaymentAllocation.status != AllocationStatus.REJECTED.value)
            .all()
        )
        received = sum(a.amount_paise for a in allocated)
        gap = entitlement.net_paise - received
        if gap > 0:
            outstanding[period.id] = gap
    return outstanding


def _learned_lag(db: Session, organization_id: int) -> Optional[int]:
    """A repeatedly-corrected pay lag becomes supporting evidence (spec s76)."""
    pattern = (
        db.query(CorrectionPattern)
        .filter(CorrectionPattern.organization_id == organization_id,
                CorrectionPattern.pattern_key == "salary_month_lag",
                CorrectionPattern.is_active.is_(True))
        .first()
    )
    if pattern and pattern.occurrences >= 3:
        value = (pattern.pattern_value_json or {}).get("lag_months")
        if isinstance(value, int):
            return value
    return None


# ---------------------------------------------------------------------------
# Main matcher
# ---------------------------------------------------------------------------
def match(db: Session, txn: BankTransaction, employee: Employee) -> MonthMatchResult:
    """Produce a ranked, explained distribution over candidate salary months."""
    result = MonthMatchResult()
    org = employee.organization
    lag = _learned_lag(db, employee.organization_id)
    lag_note = ""
    if lag is None:
        lag = org.pay_lag_months if org else 1
    else:
        lag_note = " (learned from previous corrections in this organisation)"

    periods = _periods_in_window(db, employee.organization_id, txn.txn_date)
    if not periods:
        result.note = "No salary periods are configured around this transaction date."
        return result

    by_key = {(p.year, p.month): p for p in periods}
    candidates: dict[int, Candidate] = {}

    def candidate_for(period: SalaryPeriod) -> Candidate:
        if period.id not in candidates:
            candidates[period.id] = Candidate(period=period, probability=0.0)
        return candidates[period.id]

    # --- 1. Explicit month reference in the narration (strongest) ---
    references = extract_month_references(txn.description or txn.narration_raw or "")
    explicit_periods: list[SalaryPeriod] = []
    for month, year, matched_text in references:
        if year is None:
            # Infer the year: a salary month named in a narration is almost
            # always recent and in the past relative to the credit.
            for offset in (0, -1, 1):
                guess_year = txn.txn_date.year + offset
                period = by_key.get((guess_year, month))
                if period and period.sort_key <= txn.txn_date.year * 100 + txn.txn_date.month:
                    year = guess_year
                    break
            if year is None:
                continue
        period = by_key.get((year, month))
        if period is None:
            continue
        cand = candidate_for(period)
        cand.add(0.75, f"The narration names this month explicitly ('{matched_text.strip()}')")
        explicit_periods.append(period)

    if explicit_periods:
        result.explicit = True

    # --- 2. Pay-cycle expectation ---
    for period in periods:
        exp_year, exp_month = expected_credit_month(period, lag)
        if (exp_year, exp_month) == (txn.txn_date.year, txn.txn_date.month):
            candidate_for(period).add(
                0.45,
                f"Salary for {period.label} is normally credited in "
                f"{txn.txn_date.strftime('%B %Y')}{lag_note}",
            )
        elif abs((exp_year * 12 + exp_month) - (txn.txn_date.year * 12 + txn.txn_date.month)) == 1:
            candidate_for(period).add(
                0.15, f"{period.label} is one month away from the usual credit month"
            )

    # --- 3. Outstanding amounts ---
    outstanding = _unpaid_periods(db, employee, periods)
    tolerance = settings.match_tolerance_paise
    for period_id, gap in outstanding.items():
        period = next((p for p in periods if p.id == period_id), None)
        if period is None or period.sort_key > txn.txn_date.year * 100 + txn.txn_date.month:
            continue
        cand = candidate_for(period)
        if abs(gap - txn.amount_paise) <= tolerance:
            cand.add(0.4, f"The outstanding amount for {period.label} exactly matches this credit")
        elif gap > 0:
            cand.add(0.1, f"{period.label} still has {gap / 100:.2f} outstanding")

    # --- 4. Entitlement amount agreement ---
    for period in periods:
        entitlement = (
            db.query(Entitlement)
            .filter(Entitlement.employee_id == employee.id,
                    Entitlement.period_id == period.id,
                    Entitlement.is_current.is_(True))
            .first()
        )
        if entitlement and abs(entitlement.net_paise - txn.amount_paise) <= tolerance:
            candidate_for(period).add(
                0.2, f"The credit equals the expected amount for {period.label}"
            )

    # --- 5. Arrear wording pushes the month backwards ---
    if looks_like_arrear(txn.description or ""):
        for period in periods:
            if period.sort_key < txn.txn_date.year * 100 + txn.txn_date.month - 1:
                if period.id in candidates:
                    candidates[period.id].add(0.08, "Narration mentions arrears, which relate to earlier months")

    if not candidates:
        result.note = (
            "Nothing in this transaction indicates which salary month it belongs to."
        )
        return result

    # --- Normalise into probabilities ---
    ranked = sorted(candidates.values(), key=lambda c: -c.probability)
    total = sum(c.probability for c in ranked) or 1.0
    for cand in ranked:
        cand.probability = round(cand.probability / total, 4)
    result.candidates = ranked

    if not result.confident:
        top = ranked[0]
        result.note = (
            f"Most likely {top.period.label} ({top.probability * 100:.0f}%), but the salary "
            "month is not independently confirmed. Please confirm or correct it."
        )

    # --- 6. Multi-month detection (spec s10) ---
    result.multi_period = _detect_multi_period(db, txn, employee, periods, outstanding, explicit_periods)
    return result


def _detect_multi_period(
    db: Session,
    txn: BankTransaction,
    employee: Employee,
    periods: list[SalaryPeriod],
    outstanding: dict[int, int],
    explicit_periods: list[SalaryPeriod],
) -> list[tuple[SalaryPeriod, int, str]]:
    """Propose a split when one credit plausibly covers several months.

    A split is only proposed when the parts sum EXACTLY to the transaction
    amount (or leave a clearly-labelled remainder), never as a rounding fudge.
    """
    amount = txn.amount_paise
    tolerance = settings.match_tolerance_paise

    # Case A: the narration named several months outright.
    if len(explicit_periods) > 1:
        unique = list(dict.fromkeys(explicit_periods))
        entitlements = []
        for period in unique:
            ent = (
                db.query(Entitlement)
                .filter(Entitlement.employee_id == employee.id,
                        Entitlement.period_id == period.id,
                        Entitlement.is_current.is_(True))
                .first()
            )
            entitlements.append((period, ent.net_paise if ent else 0))

        known_total = sum(v for _, v in entitlements)
        if known_total and abs(known_total - amount) <= tolerance:
            return [(p, v, f"Narration names {p.label}; expected {v / 100:.2f}")
                    for p, v in entitlements]
        if known_total and known_total < amount:
            remainder = amount - known_total
            out = [(p, v, f"Narration names {p.label}; expected {v / 100:.2f}")
                   for p, v in entitlements]
            out.append((unique[-1], remainder,
                        f"Remaining {remainder / 100:.2f} is unexplained and needs confirmation"))
            return out
        if not known_total:
            from ..money import split_evenly

            shares = split_evenly(amount, len(unique))
            return [(p, share, f"Narration names {p.label}; amount split equally pending confirmation")
                    for p, share in zip(unique, shares)]

    # Case B: the amount is a clean multiple of the usual entitlement.
    unpaid = sorted(
        ((pid, gap) for pid, gap in outstanding.items()),
        key=lambda kv: next((p.sort_key for p in periods if p.id == kv[0]), 0),
    )
    if len(unpaid) >= 2:
        running, chosen = 0, []
        for period_id, gap in unpaid:
            if running + gap > amount + tolerance:
                break
            period = next((p for p in periods if p.id == period_id), None)
            if period is None:
                continue
            chosen.append((period, gap))
            running += gap
        if len(chosen) >= 2 and abs(running - amount) <= tolerance:
            return [(p, v, f"Clears the outstanding {v / 100:.2f} for {p.label}") for p, v in chosen]
        if len(chosen) >= 2 and running < amount:
            remainder = amount - running
            out = [(p, v, f"Clears the outstanding {v / 100:.2f} for {p.label}") for p, v in chosen]
            out.append((chosen[-1][0], remainder,
                        f"Extra {remainder / 100:.2f} beyond the outstanding amounts; reason not established"))
            return out

    return []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def store_candidates(db: Session, txn: BankTransaction, result: MonthMatchResult,
                     keep: int = 5) -> list[MonthCandidate]:
    """Persist the ranked distribution so the UI can show the alternatives.

    Candidates a human has already confirmed are preserved untouched.
    """
    confirmed = (
        db.query(MonthCandidate)
        .filter(MonthCandidate.transaction_id == txn.id,
                MonthCandidate.confirmed_by_human.is_(True))
        .all()
    )
    if confirmed:
        return confirmed

    db.query(MonthCandidate).filter(MonthCandidate.transaction_id == txn.id).delete()

    stored: list[MonthCandidate] = []
    for index, cand in enumerate(result.candidates[:keep]):
        row = MonthCandidate(
            transaction_id=txn.id,
            period_id=cand.period.id,
            probability=cand.probability,
            reasons_json=cand.reasons,
            is_selected=(index == 0 and result.confident),
        )
        db.add(row)
        stored.append(row)
    db.flush()
    return stored


def confirm_month(db: Session, txn: BankTransaction, period: SalaryPeriod) -> MonthCandidate:
    """Record a human's decision about which month a credit belongs to."""
    db.query(MonthCandidate).filter(MonthCandidate.transaction_id == txn.id).update(
        {MonthCandidate.is_selected: False}
    )
    row = (
        db.query(MonthCandidate)
        .filter(MonthCandidate.transaction_id == txn.id, MonthCandidate.period_id == period.id)
        .first()
    )
    if row is None:
        row = MonthCandidate(transaction_id=txn.id, period_id=period.id, probability=1.0,
                             reasons_json=["Confirmed by a user"])
        db.add(row)
    row.is_selected = True
    row.confirmed_by_human = True
    db.flush()
    return row


def learn_from_correction(db: Session, txn: BankTransaction, period: SalaryPeriod) -> None:
    """Record the organisation's habitual pay lag from a human correction.

    Learned patterns are supporting evidence only; they never override an
    explicit month in a narration or an official document (spec s76, s77).
    """
    credit_index = txn.txn_date.year * 12 + txn.txn_date.month
    period_index = period.year * 12 + period.month
    lag = credit_index - period_index
    if not 0 <= lag <= 6:
        return

    pattern = (
        db.query(CorrectionPattern)
        .filter(CorrectionPattern.organization_id == txn.organization_id,
                CorrectionPattern.pattern_key == "salary_month_lag")
        .first()
    )
    if pattern is None:
        pattern = CorrectionPattern(
            organization_id=txn.organization_id,
            pattern_key="salary_month_lag",
            pattern_value_json={"lag_months": lag, "samples": [lag]},
            occurrences=1,
        )
        db.add(pattern)
    else:
        samples = list((pattern.pattern_value_json or {}).get("samples", []))[-19:]
        samples.append(lag)
        # The mode of recent corrections, not the latest one, so a single
        # unusual correction cannot swing the system.
        mode = max(set(samples), key=samples.count)
        pattern.pattern_value_json = {"lag_months": mode, "samples": samples}
        pattern.occurrences = (pattern.occurrences or 0) + 1
        from ..db import utcnow

        pattern.last_seen_at = utcnow()
    db.flush()
