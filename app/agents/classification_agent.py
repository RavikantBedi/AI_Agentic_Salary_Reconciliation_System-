"""AGENT 5 - Payment Classification (spec s7).

Classifies each transaction using MULTIPLE independent signals and records
every signal it used, with its weight, so a person can see exactly why a
payment was labelled the way it was.

The specification is explicit: never classify from the word "GOVT" alone.
A government marker here contributes a small weight and can never, by itself,
push a transaction past the confidence threshold.

Output is a *suggestion* with a confidence. Low-confidence results stay
``UNKNOWN`` rather than being forced into a category (spec s52, s99).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..enums import Direction, PaymentClassification
from ..models import BankTransaction, Employee, Entitlement
from ..services.textutils import (
    contains_any,
    looks_government,
    looks_like_arrear,
    looks_like_reversal,
    normalize_narration,
)

AGENT_NAME = "classification_agent"


@dataclass
class Signal:
    name: str
    detail: str
    weight: float
    suggests: str


@dataclass
class ClassificationResult:
    classification: str
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    def as_evidence(self) -> dict:
        return {
            "signals": [
                {"name": s.name, "detail": s.detail, "weight": round(s.weight, 3), "suggests": s.suggests}
                for s in self.signals
            ],
            "alternatives": [{"classification": c, "score": round(v, 3)} for c, v in self.alternatives],
        }


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------
KEYWORDS: dict[str, tuple[str, ...]] = {
    PaymentClassification.SALARY.value: ("salary", "sal cr", "sal/", "wages", "pay for", "वेतन"),
    PaymentClassification.HONORARIUM.value: ("honorarium", "honararium", "honararium", "hon.", "मानदेय"),
    PaymentClassification.ARREAR.value: ("arrear", "arrears", "arrer", "बकाया", "एरियर"),
    PaymentClassification.INCENTIVE.value: ("incentive", "protsahan", "प्रोत्साहन", "performance pay"),
    PaymentClassification.ALLOWANCE.value: ("allowance", "bhatta", "भत्ता", "ta/da", "conveyance"),
    PaymentClassification.REIMBURSEMENT.value: ("reimburse", "reimbursement", "claim settle", "प्रतिपूर्ति"),
    PaymentClassification.BONUS.value: ("bonus", "ex gratia", "exgratia", "बोनस"),
    PaymentClassification.ADVANCE.value: ("advance", "adv.", "अग्रिम"),
    PaymentClassification.REFUND.value: ("refund", "refnd", "वापसी"),
    PaymentClassification.PENSION.value: ("pension", "पेंशन"),
    PaymentClassification.SCHOLARSHIP.value: ("scholarship", "chhatravritti", "छात्रवृत्ति"),
    PaymentClassification.GOVERNMENT_BENEFIT.value: ("dbt", "subsidy", "yojana", "योजना", "benefit transfer"),
    PaymentClassification.BANK_CHARGE.value: ("charge", "chrg", "fee", "gst on", "sms alert", "amc"),
}

#: Weight given to a narration keyword hit. High, because an explicit word in
#: the bank's own narration is strong evidence.
KEYWORD_WEIGHT = 0.55
#: Deliberately small: a government marker alone must never decide (spec s7).
GOVERNMENT_MARKER_WEIGHT = 0.12


def _keyword_signals(narration: str) -> list[Signal]:
    signals: list[Signal] = []
    for classification, words in KEYWORDS.items():
        hit = next((w for w in words if w in narration), None)
        if hit:
            signals.append(Signal(
                name="narration_keyword",
                detail=f"Narration contains '{hit}'",
                weight=KEYWORD_WEIGHT,
                suggests=classification,
            ))
    return signals


def _amount_signals(db: Session, txn: BankTransaction, employee: Optional[Employee]) -> list[Signal]:
    """Compare the credit against the employee's known expected amounts."""
    if employee is None:
        return []

    entitlements = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id, Entitlement.is_current.is_(True))
        .all()
    )
    if not entitlements:
        return []

    nets = [e.net_paise for e in entitlements if e.net_paise > 0]
    if not nets:
        return []

    typical = sorted(nets)[len(nets) // 2]
    amount = txn.amount_paise
    signals: list[Signal] = []

    if typical and abs(amount - typical) <= max(100, int(typical * 0.02)):
        signals.append(Signal(
            name="amount_matches_expected",
            detail=f"Amount matches the usual monthly entitlement ({typical / 100:.2f})",
            weight=0.35,
            suggests=PaymentClassification.SALARY.value,
        ))
    elif typical and amount > typical * 1.6:
        # A credit well above one month's pay often bundles months or arrears.
        signals.append(Signal(
            name="amount_exceeds_one_month",
            detail=(
                f"Amount ({amount / 100:.2f}) is well above one month's entitlement "
                f"({typical / 100:.2f}); it may combine months or include arrears"
            ),
            weight=0.18,
            suggests=PaymentClassification.ARREAR.value,
        ))
    return signals


def _recurrence_signals(db: Session, txn: BankTransaction, employee: Optional[Employee]) -> list[Signal]:
    """A credit that repeats monthly at a similar amount behaves like salary."""
    if employee is None:
        return []

    window_start = txn.txn_date - timedelta(days=200)
    history = (
        db.query(BankTransaction)
        .filter(
            BankTransaction.employee_id == employee.id,
            BankTransaction.direction == Direction.CREDIT.value,
            BankTransaction.txn_date >= window_start,
            BankTransaction.txn_date < txn.txn_date,
            BankTransaction.id != txn.id,
        )
        .all()
    )
    if len(history) < 2:
        return []

    tolerance = max(100, int(txn.amount_paise * 0.05))
    similar = [h for h in history if abs(h.amount_paise - txn.amount_paise) <= tolerance]
    if len(similar) >= 2:
        months = {(h.txn_date.year, h.txn_date.month) for h in similar}
        if len(months) >= 2:
            return [Signal(
                name="recurring_pattern",
                detail=f"A similar amount was credited in {len(months)} earlier months",
                weight=0.3,
                suggests=PaymentClassification.SALARY.value,
            )]
    return []


def _sender_signals(txn: BankTransaction, employee: Optional[Employee]) -> list[Signal]:
    signals: list[Signal] = []
    narration = txn.narration_normalized or normalize_narration(txn.description)

    if looks_government(narration) or looks_government(txn.counterparty or ""):
        signals.append(Signal(
            name="government_marker",
            detail=(
                "Narration carries a government/treasury marker. This is a weak "
                "signal on its own and does not prove an official payment."
            ),
            weight=GOVERNMENT_MARKER_WEIGHT,
            suggests=PaymentClassification.SALARY.value,
        ))

    if txn.utr:
        signals.append(Signal(
            name="has_reference",
            detail=f"Carries a transaction reference ({txn.utr})",
            weight=0.05,
            suggests=PaymentClassification.SALARY.value,
        ))
    return signals


def _cycle_signals(txn: BankTransaction, employee: Optional[Employee]) -> list[Signal]:
    """Credits landing near the organisation's usual pay day look like salary."""
    if employee is None or employee.organization is None:
        return []
    pay_day = employee.organization.pay_day_of_month or 7
    distance = abs(txn.txn_date.day - pay_day)
    if distance <= 5:
        return [Signal(
            name="within_pay_cycle",
            detail=f"Credited on day {txn.txn_date.day}, close to the usual pay day ({pay_day})",
            weight=0.15,
            suggests=PaymentClassification.SALARY.value,
        )]
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def classify(db: Session, txn: BankTransaction,
             employee: Optional[Employee] = None) -> ClassificationResult:
    """Classify one transaction from all available evidence."""
    narration = txn.narration_normalized or normalize_narration(txn.description or "")
    employee = employee or txn.employee

    # --- Debits are not salary. Handle them on their own terms (spec s71). ---
    if txn.direction == Direction.DEBIT.value:
        if looks_like_reversal(narration):
            return ClassificationResult(
                PaymentClassification.REVERSAL.value, 0.85,
                [Signal("reversal_wording", "Narration indicates a reversal or return", 0.85,
                        PaymentClassification.REVERSAL.value)],
            )
        if contains_any(narration, KEYWORDS[PaymentClassification.BANK_CHARGE.value]):
            return ClassificationResult(
                PaymentClassification.BANK_CHARGE.value, 0.7,
                [Signal("charge_wording", "Narration indicates a bank charge", 0.7,
                        PaymentClassification.BANK_CHARGE.value)],
            )
        return ClassificationResult(
            PaymentClassification.OTHER.value, 0.3,
            [Signal("debit", "This is a debit, so it is not an incoming payment", 0.3,
                    PaymentClassification.OTHER.value)],
        )

    # --- A credit that reverses an earlier debit ---
    if looks_like_reversal(narration):
        return ClassificationResult(
            PaymentClassification.REVERSAL.value, 0.8,
            [Signal("reversal_wording", "Narration indicates a reversal or returned payment", 0.8,
                    PaymentClassification.REVERSAL.value)],
        )

    signals: list[Signal] = []
    signals.extend(_keyword_signals(narration))
    signals.extend(_sender_signals(txn, employee))
    signals.extend(_amount_signals(db, txn, employee))
    signals.extend(_recurrence_signals(db, txn, employee))
    signals.extend(_cycle_signals(txn, employee))

    # An explicit arrear word outranks a generic salary word: "MARCH SALARY
    # ARREAR" is an arrear payment.
    if looks_like_arrear(narration):
        signals.append(Signal(
            name="arrear_wording",
            detail="Narration explicitly mentions arrears",
            weight=0.3,
            suggests=PaymentClassification.ARREAR.value,
        ))

    if not signals:
        return ClassificationResult(
            PaymentClassification.UNKNOWN.value, 0.0,
            [Signal("no_signals", "Nothing in this transaction indicates what the payment is for",
                    0.0, PaymentClassification.UNKNOWN.value)],
        )

    scores: dict[str, float] = {}
    for signal in signals:
        scores[signal.suggests] = scores.get(signal.suggests, 0.0) + signal.weight

    # Salary and honorarium are the same economic event under different names;
    # let them reinforce rather than compete.
    sal = PaymentClassification.SALARY.value
    hon = PaymentClassification.HONORARIUM.value
    if sal in scores and hon in scores:
        combined = scores[sal] + scores[hon]
        winner = hon if scores[hon] >= scores[sal] else sal
        loser = sal if winner == hon else hon
        scores[winner] = combined
        scores.pop(loser)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    confidence = min(0.97, round(best_score, 4))

    # A government marker on its own is never enough (spec s7).
    only_marker = all(s.name in {"government_marker", "has_reference"} for s in signals)
    if only_marker:
        return ClassificationResult(
            PaymentClassification.UNKNOWN.value,
            min(0.35, confidence),
            signals,
            [(c, v) for c, v in ranked],
        )

    if confidence < 0.30:
        return ClassificationResult(
            PaymentClassification.UNKNOWN.value, confidence, signals, [(c, v) for c, v in ranked]
        )

    return ClassificationResult(best, confidence, signals, [(c, v) for c, v in ranked[1:4]])


def apply(db: Session, txn: BankTransaction, employee: Optional[Employee] = None) -> ClassificationResult:
    """Classify and write the result onto the transaction.

    A transaction whose classification a human has locked is never overwritten
    (spec s44: AI suggestions are editable, and corrections stick).
    """
    result = classify(db, txn, employee)
    if txn.classification_locked:
        return result
    txn.classification = result.classification
    txn.classification_confidence = result.confidence
    txn.classification_evidence_json = result.as_evidence()
    return result
