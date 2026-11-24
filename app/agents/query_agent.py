"""AGENT 11 - Natural language query and reporting (spec s34, s65, s66).

Answers questions about a person's own payment records.

How it stays truthful
---------------------
1. The question is routed to a structured intent.
2. The intent runs a real SQL query against the ledger.
3. The answer is composed from those rows, with the evidence attached.
4. Only THEN, and only if an API key is configured, the LLM may rephrase the
   already-computed answer into friendlier prose. It is given the figures and
   forbidden to alter them; if it fails, the deterministic answer is shown.

The LLM never chooses a number. Every financial answer carries its evidence
(spec s34).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..enums import (
    AllocationStatus,
    ArrearStatus,
    DeductionStatus,
    Direction,
    ReconOutcome,
    SHORTFALL_OUTCOMES,
    VerificationLevel,
)
from ..money import format_money, to_paise
from ..models import (
    Arrear,
    BankTransaction,
    Deduction,
    Employee,
    Entitlement,
    PaymentAllocation,
    PaymentSanction,
    Reconciliation,
    SalaryPeriod,
)
from ..services.textutils import MONTH_LOOKUP, normalize_narration
from . import llm

AGENT_NAME = "query_agent"


@dataclass
class Answer:
    text: str
    intent: str
    evidence: list[dict] = field(default_factory=list)
    used_llm: bool = False
    followups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"text": self.text, "intent": self.intent, "evidence": self.evidence,
                "used_llm": self.used_llm, "followups": self.followups}


SUGGESTED_QUESTIONS_EN = [
    "Why is my salary lower?",
    "Which months are unpaid?",
    "How much money is pending?",
    "Show all deductions.",
    "Show my March salary calculation.",
    "Which transactions are not verified?",
    "Show all arrears.",
    "How much government money was sanctioned?",
    "Explain my salary this year like I'm a normal person.",
]

SUGGESTED_QUESTIONS_HI = [
    "मेरा वेतन कम क्यों है?",
    "कौन से महीने अवैतनिक हैं?",
    "कितनी राशि लंबित है?",
    "सभी कटौती दिखाएँ।",
    "मार्च के वेतन की गणना दिखाएँ।",
    "कौन से लेन-देन प्रमाणित नहीं हैं?",
    "सभी बकाया दिखाएँ।",
    "इस वर्ष का पूरा हिसाब सरल भाषा में समझाएँ।",
]


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------
INTENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("unpaid_months", ("unpaid", "not paid", "missing month", "which months are pending",
                       "अवैतनिक", "नहीं मिला", "बकाया महीने")),
    ("pending_amount", ("how much is pending", "money is pending", "total pending",
                        "how much do they owe", "कितनी राशि लंबित", "कितना बकाया")),
    ("arrears", ("arrear", "बकाया", "एरियर")),
    ("deductions", ("deduction", "deducted", "कटौती", "how much did i lose")),
    ("unverified", ("not verified", "unverified", "verification", "अप्रमाणित", "सत्यापन")),
    ("sanctioned", ("sanction", "government money", "स्वीकृत", "सरकारी राशि")),
    ("calculation", ("calculation", "how was it calculated", "breakdown", "why is my salary",
                     "salary lower", "गणना", "वेतन कम")),
    ("explain_month", ("explain", "why did i receive", "what was", "समझाएँ", "क्यों मिला")),
    ("documents", ("document", "proof", "evidence", "दस्तावेज़", "प्रमाण")),
    ("total_received", ("how much salary did i receive", "total received", "how much did i get",
                        "कुल प्राप्त", "कितना मिला")),
    # Note: normalize_narration strips apostrophes, so "like I'm" arrives as
    # "like i m". Patterns are written against the normalised form.
    ("plain_summary", ("normal person", "plain language", "plain terms", "simple language",
                       "in simple", "overall summary", "explain my salary from",
                       "explain my salary this year", "सरल भाषा", "पूरा हिसाब")),
    ("annual_statement", ("annual statement", "yearly", "year statement", "वार्षिक")),
    ("is_correct", ("is my salary correct", "is my payment correct", "is my june payment correct",
                    "क्या मेरा वेतन सही")),
    ("large_transactions", ("above", "more than", "greater than", "से अधिक")),
    ("low_confidence", ("confidence below", "low confidence", "कम विश्वास")),
]


def detect_intent(question: str) -> str:
    text = normalize_narration(question)
    scores: dict[str, int] = {}
    for intent, phrases in INTENT_PATTERNS:
        for phrase in phrases:
            if phrase in text:
                scores[intent] = scores.get(intent, 0) + len(phrase)
    if not scores:
        return "general"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def _extract_year(question: str, default_year: int) -> int:
    match = re.search(r"\b(20\d{2})\b", question)
    return int(match.group(1)) if match else default_year


def _extract_month(question: str) -> Optional[int]:
    text = normalize_narration(question)
    for word, month in MONTH_LOOKUP.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            return month
    return None


def _extract_amount_paise(question: str) -> Optional[int]:
    match = re.search(r"(?:above|more than|greater than|over)\s*(?:rs\.?|inr|₹)?\s*([\d,]+)",
                      question, re.I)
    if not match:
        return None
    try:
        return to_paise(match.group(1))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def _recons(db: Session, employee: Employee, year: Optional[int] = None) -> list[Reconciliation]:
    query = db.query(Reconciliation).filter(Reconciliation.employee_id == employee.id)
    rows = query.all()
    if year:
        rows = [r for r in rows if r.period and r.period.year == year]
    return sorted(rows, key=lambda r: r.period.sort_key if r.period else 0)


def _plabel(period: Optional[SalaryPeriod], locale: str) -> str:
    """Period name in the reader's language."""
    if period is None:
        return ""
    return period.label_hi if locale == "hi" else period.label


def _evidence_for_recon(recon: Reconciliation) -> dict:
    return {
        "type": "period",
        "label": recon.period.label if recon.period else "",
        "expected": format_money(recon.entitled_paise),
        "sanctioned": (format_money(recon.sanctioned_paise)
                       if recon.sanctioned_paise is not None else "no official record"),
        "received": format_money(recon.credited_paise),
        "difference": format_money(recon.difference_paise),
        "outcome": recon.outcome,
        "link": f"/periods/{recon.uid}",
    }


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------
def _answer_unpaid(db: Session, employee: Employee, year: int, locale: str) -> Answer:
    rows = [r for r in _recons(db, employee, year)
            if r.outcome in {ReconOutcome.NOT_PAID.value,
                             ReconOutcome.SANCTIONED_NOT_CREDITED.value}]
    if not rows:
        text = ("Every month in your records for this year shows a payment."
                if locale != "hi" else "इस वर्ष के आपके रिकॉर्ड में हर महीने भुगतान दर्ज है।")
        return Answer(text, "unpaid_months")

    total = sum(r.entitled_paise for r in rows)
    names = ", ".join(_plabel(r.period, locale) for r in rows)
    if locale == "hi":
        text = (f"{len(rows)} महीने का भुगतान दर्ज नहीं है: {names}।\n"
                f"इन महीनों की कुल अपेक्षित राशि {format_money(total)} है।")
    else:
        text = (f"{len(rows)} month(s) show no payment: {names}.\n"
                f"The total expected for those months is {format_money(total)}.")
    return Answer(text, "unpaid_months", [_evidence_for_recon(r) for r in rows])


def _answer_pending(db: Session, employee: Employee, locale: str) -> Answer:
    arrears = (
        db.query(Arrear)
        .filter(Arrear.employee_id == employee.id,
                Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value]))
        .all()
    )
    total = sum(a.remaining_paise for a in arrears)
    if total <= 0:
        text = ("Nothing is outstanding in your records."
                if locale != "hi" else "आपके रिकॉर्ड में कोई राशि लंबित नहीं है।")
        return Answer(text, "pending_amount")

    lines = [f"  - {_plabel(a.origin_period, locale)}: {format_money(a.remaining_paise)}"
             for a in sorted(arrears, key=lambda a: a.origin_period.sort_key)]
    if locale == "hi":
        text = f"कुल {format_money(total)} लंबित है:\n" + "\n".join(lines)
    else:
        text = f"{format_money(total)} is outstanding in total:\n" + "\n".join(lines)

    evidence = [{"type": "arrear", "label": a.origin_period.label,
                 "outstanding": format_money(a.remaining_paise), "status": a.status,
                 "link": f"/arrears#{a.uid}"} for a in arrears]
    return Answer(text, "pending_amount", evidence)


def _answer_deductions(db: Session, employee: Employee, locale: str) -> Answer:
    rows = db.query(Deduction).filter(Deduction.employee_id == employee.id).all()
    if not rows:
        text = ("No deductions are recorded against your payments."
                if locale != "hi" else "आपके भुगतान में कोई कटौती दर्ज नहीं है।")
        return Answer(text, "deductions")

    documented = [d for d in rows if d.status == DeductionStatus.DOCUMENTED.value]
    expected = [d for d in rows if d.status == DeductionStatus.EXPECTED.value]
    unexplained = [d for d in rows if d.status == DeductionStatus.UNEXPLAINED.value]

    doc_total = sum(d.amount_paise for d in documented)
    exp_total = sum(d.amount_paise for d in expected)
    unexp_total = sum(d.amount_paise for d in unexplained)

    if locale == "hi":
        parts = [f"कुल कटौती: {format_money(doc_total + exp_total + unexp_total)}"]
        if exp_total:
            parts.append(f"नियम के अनुसार अपेक्षित: {format_money(exp_total)}")
        if doc_total:
            parts.append(f"दस्तावेज़ से प्रमाणित: {format_money(doc_total)}")
        if unexp_total:
            parts.append(f"बिना प्रमाण के: {format_money(unexp_total)} — इसका कोई अधिकृत "
                         "रिकॉर्ड नहीं मिला।")
    else:
        parts = [f"Total deductions: {format_money(doc_total + exp_total + unexp_total)}"]
        if exp_total:
            parts.append(f"Authorised by the salary rule: {format_money(exp_total)}")
        if doc_total:
            parts.append(f"Supported by a document: {format_money(doc_total)}")
        if unexp_total:
            parts.append(f"With no supporting record: {format_money(unexp_total)} - "
                         "no authorising document has been found for this amount.")

    for d in sorted(unexplained, key=lambda d: d.period.sort_key if d.period else 0):
        label = _plabel(d.period, locale) or ("अज्ञात अवधि" if locale == "hi" else "unknown period")
        parts.append(f"  - {label}: {format_money(d.amount_paise)} ({d.status})")

    evidence = [{"type": "deduction",
                 "label": d.period.label if d.period else "",
                 "amount": format_money(d.amount_paise),
                 "category": d.category, "status": d.status,
                 "link": "/deductions"} for d in rows]
    return Answer("\n".join(parts), "deductions", evidence)


def _answer_calculation(db: Session, employee: Employee, question: str,
                        year: int, locale: str) -> Answer:
    month = _extract_month(question)
    recons = _recons(db, employee, year)
    if month:
        recons = [r for r in recons if r.period and r.period.month == month]
    if not recons:
        text = ("I could not find that month in your records."
                if locale != "hi" else "वह महीना आपके रिकॉर्ड में नहीं मिला।")
        return Answer(text, "calculation")

    if month:
        recon = recons[-1]
    else:
        # No month named. "Why is my salary lower?" is about a month that
        # actually went wrong, so prefer the most recent one with a
        # discrepancy over the latest month (which is often simply not due).
        problem_months = [
            r for r in recons
            if r.outcome not in {ReconOutcome.FULLY_RECONCILED.value,
                                 ReconOutcome.AWAITING_CONFIRMATION.value}
        ]
        recon = problem_months[-1] if problem_months else recons[-1]
    entitlement = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id,
                Entitlement.period_id == recon.period_id,
                Entitlement.is_current.is_(True))
        .first()
    )
    if entitlement is None:
        return Answer(recon.explanation_hi if locale == "hi" else recon.explanation_en,
                      "calculation", [_evidence_for_recon(recon)])

    lines = [f"{recon.period.label if locale != 'hi' else recon.period.label_hi}:"]
    for comp in entitlement.components:
        label = (comp.label_hi or comp.label_en) if locale == "hi" else comp.label_en
        sign = "-" if comp.kind == "DEDUCTION" else "+"
        lines.append(f"  {sign} {label}: {format_money(comp.amount_paise)}"
                     + (f"  ({comp.calculation_note})" if comp.calculation_note else ""))
    lines.append(f"  = {'सकल' if locale == 'hi' else 'Gross'}: {format_money(entitlement.gross_paise)}")
    if entitlement.deductions_paise:
        lines.append(f"  - {'कटौती' if locale == 'hi' else 'Deductions'}: "
                     f"{format_money(entitlement.deductions_paise)}")
    lines.append(f"  = {'शुद्ध देय' if locale == 'hi' else 'Net expected'}: "
                 f"{format_money(entitlement.net_paise)}")
    lines.append(f"  {'वास्तव में प्राप्त' if locale == 'hi' else 'Actually received'}: "
                 f"{format_money(recon.credited_paise)}")

    rule = entitlement.rule
    if rule:
        source = rule.government_order.order_number if rule.government_order else rule.source_note
        lines.append(f"  {'आधार' if locale == 'hi' else 'Based on'}: {rule.label}"
                     + (f" ({source})" if source else ""))

    evidence = [_evidence_for_recon(recon)]
    if rule and rule.government_order and rule.government_order.document_id:
        evidence.append({"type": "document",
                         "label": rule.government_order.title,
                         "link": f"/documents/{rule.government_order.document_id}"})
    return Answer("\n".join(lines), "calculation", evidence)


def _answer_unverified(db: Session, employee: Employee, locale: str) -> Answer:
    txns = (
        db.query(BankTransaction)
        .filter(BankTransaction.employee_id == employee.id,
                BankTransaction.direction == Direction.CREDIT.value)
        .all()
    )
    weak = [t for t in txns if t.verification_level < VerificationLevel.OFFICIAL_DOCUMENT]
    if not weak:
        text = ("Every payment in your records is supported by an official document."
                if locale != "hi" else "आपके सभी भुगतान आधिकारिक दस्तावेज़ से प्रमाणित हैं।")
        return Answer(text, "unverified")

    if locale == "hi":
        head = (f"{len(weak)} भुगतान अभी आधिकारिक दस्तावेज़ से प्रमाणित नहीं हैं। "
                "बैंक में जमा यह दर्शाता है कि पैसा आया, परंतु यह सिद्ध नहीं करता कि "
                "भुगतान आधिकारिक रूप से स्वीकृत था।")
    else:
        head = (f"{len(weak)} payment(s) are not yet confirmed by an official document. "
                "A bank credit shows the money arrived; it does not prove the payment "
                "was officially sanctioned.")

    lines = [head]
    for t in sorted(weak, key=lambda t: t.txn_date)[:12]:
        lines.append(f"  - {t.txn_date.isoformat()}: {format_money(t.amount_paise)} "
                     f"({VerificationLevel.label(t.verification_level, locale)})")

    evidence = [{"type": "transaction", "label": t.txn_date.isoformat(),
                 "amount": format_money(t.amount_paise),
                 "verification": VerificationLevel.label(t.verification_level, locale),
                 "link": f"/transactions/{t.uid}"} for t in weak]
    return Answer("\n".join(lines), "unverified", evidence)


def _answer_sanctioned(db: Session, employee: Employee, year: int, locale: str) -> Answer:
    sanctions = (
        db.query(PaymentSanction)
        .filter(PaymentSanction.employee_id == employee.id)
        .all()
    )
    relevant = [s for s in sanctions if s.period and s.period.year == year]
    if not relevant:
        text = (f"No official sanction records for {year} exist in the system yet. "
                "Upload sanction or payment orders to record them."
                if locale != "hi" else
                f"{year} के लिए कोई आधिकारिक स्वीकृति रिकॉर्ड दर्ज नहीं है। "
                "स्वीकृति या भुगतान आदेश अपलोड करें।")
        return Answer(text, "sanctioned")

    total = sum(s.amount_paise for s in relevant)
    lines = [(f"{year} में कुल {format_money(total)} आधिकारिक रूप से स्वीकृत है "
              f"({len(relevant)} रिकॉर्ड):" if locale == "hi" else
              f"{format_money(total)} was officially sanctioned in {year} "
              f"across {len(relevant)} record(s):")]
    for s in sorted(relevant, key=lambda s: s.period.sort_key):
        lines.append(f"  - {_plabel(s.period, locale)}: {format_money(s.amount_paise)}"
                     + (f" ({s.sanction_number})" if s.sanction_number else ""))

    evidence = [{"type": "sanction", "label": s.period.label,
                 "amount": format_money(s.amount_paise),
                 "number": s.sanction_number,
                 "verification": VerificationLevel.label(s.verification_level, locale)}
                for s in relevant]
    return Answer("\n".join(lines), "sanctioned", evidence)


def _answer_total_received(db: Session, employee: Employee, year: int, locale: str) -> Answer:
    rows = _recons(db, employee, year)
    received = sum(r.credited_paise for r in rows)
    entitled = sum(r.entitled_paise for r in rows)
    arrears = sum(r.arrear_received_paise for r in rows)

    if locale == "hi":
        text = (f"{year} में आपको कुल {format_money(received)} प्राप्त हुआ।\n"
                f"नियम के अनुसार अपेक्षित राशि {format_money(entitled)} थी।")
        if arrears:
            text += f"\nइसमें {format_money(arrears)} बकाया भुगतान शामिल है।"
        if entitled > received:
            text += f"\nअंतर: {format_money(entitled - received)} कम।"
    else:
        text = (f"You received {format_money(received)} in total during {year}.\n"
                f"The expected amount under the applicable rule was {format_money(entitled)}.")
        if arrears:
            text += f"\nThis includes {format_money(arrears)} of arrear payments."
        if entitled > received:
            text += f"\nDifference: {format_money(entitled - received)} short."
    return Answer(text, "total_received", [_evidence_for_recon(r) for r in rows])


def _answer_plain_summary(db: Session, employee: Employee, year: int, locale: str) -> Answer:
    """Spec s66: explain the year in plain language."""
    rows = _recons(db, employee, year)
    entitled = sum(r.entitled_paise for r in rows)
    received = sum(r.credited_paise for r in rows)
    arrears_received = sum(r.arrear_received_paise for r in rows)

    unexplained = sum(
        d.amount_paise for d in db.query(Deduction).filter(
            Deduction.employee_id == employee.id,
            Deduction.status == DeductionStatus.UNEXPLAINED.value).all()
    )
    documented = sum(
        d.amount_paise for d in db.query(Deduction).filter(
            Deduction.employee_id == employee.id,
            Deduction.status.in_([DeductionStatus.DOCUMENTED.value,
                                  DeductionStatus.EXPECTED.value])).all()
    )
    gap = entitled - received

    if locale == "hi":
        parts = [f"{year} का सरल हिसाब:",
                 f"आपको कुल {format_money(entitled)} मिलना चाहिए था।",
                 f"{format_money(received)} आपके खाते में जमा हुआ।"]
        if gap > 0:
            parts.append(f"{format_money(gap)} अभी बाकी या अस्पष्ट है।")
        elif gap < 0:
            parts.append(f"{format_money(abs(gap))} अपेक्षा से अधिक मिला है।")
        if arrears_received:
            parts.append(f"{format_money(arrears_received)} बकाया भुगतान के रूप में मिला।")
        if documented:
            parts.append(f"{format_money(documented)} की कटौती दस्तावेज़ों से समझाई गई है।")
        if unexplained:
            parts.append(f"{format_money(unexplained)} के अंतर की अभी पुष्टि होनी बाकी है।")
    else:
        parts = [f"Here is your {year} in plain terms:",
                 f"You were expected to receive {format_money(entitled)}.",
                 f"{format_money(received)} was credited to your account."]
        if gap > 0:
            parts.append(f"{format_money(gap)} remains unexplained or pending.")
        elif gap < 0:
            parts.append(f"{format_money(abs(gap))} more than expected was received.")
        if arrears_received:
            parts.append(f"You received {format_money(arrears_received)} in arrears.")
        if documented:
            parts.append(f"There were {format_money(documented)} of documented deductions.")
        if unexplained:
            parts.append(f"A {format_money(unexplained)} difference still needs verification.")

    return Answer("\n".join(parts), "plain_summary", [_evidence_for_recon(r) for r in rows])


def _answer_is_correct(db: Session, employee: Employee, question: str,
                       year: int, locale: str) -> Answer:
    """Spec s103: the one-click 'is my salary correct?' answer."""
    month = _extract_month(question)
    rows = _recons(db, employee, year)
    if month:
        rows = [r for r in rows if r.period and r.period.month == month]
    if not rows:
        return Answer("I could not find that period in your records." if locale != "hi"
                      else "वह अवधि आपके रिकॉर्ड में नहीं मिली।", "is_correct")

    problems = [r for r in rows if r.outcome not in {ReconOutcome.FULLY_RECONCILED.value,
                                                     ReconOutcome.AWAITING_CONFIRMATION.value}]
    if not problems:
        text = ("YES - every month checked is fully reconciled. The amount you received "
                "matches the amount due." if locale != "hi" else
                "हाँ — जाँचे गए सभी महीनों का पूरा मिलान है। आपको देय राशि के बराबर ही मिला है।")
        return Answer(text, "is_correct", [_evidence_for_recon(r) for r in rows])

    if locale == "hi":
        lines = [f"जाँच आवश्यक — {len(problems)} महीने में अंतर मिला:"]
    else:
        lines = [f"REVIEW REQUIRED - {len(problems)} month(s) show a difference:"]
    for r in problems:
        label = r.period.label if locale != "hi" else r.period.label_hi
        lines.append(f"  - {label}: {format_money(abs(r.difference_paise))} "
                     f"({r.outcome.replace('_', ' ').lower()})")
    return Answer("\n".join(lines), "is_correct", [_evidence_for_recon(r) for r in problems])


def _answer_arrears(db: Session, employee: Employee, locale: str) -> Answer:
    arrears = db.query(Arrear).filter(Arrear.employee_id == employee.id).all()
    if not arrears:
        return Answer("No arrears are recorded for you." if locale != "hi"
                      else "आपके लिए कोई बकाया दर्ज नहीं है।", "arrears")

    lines = ["बकाया विवरण:" if locale == "hi" else "Arrear ledger:"]
    for a in sorted(arrears, key=lambda a: a.origin_period.sort_key):
        lines.append(
            f"  - {_plabel(a.origin_period, locale)}: entitled {format_money(a.original_entitlement_paise)}, "
            f"received {format_money(a.previously_received_paise)}, "
            f"outstanding {format_money(a.remaining_paise)} [{a.status}]"
        )
    evidence = [{"type": "arrear", "label": a.origin_period.label,
                 "outstanding": format_money(a.remaining_paise), "status": a.status}
                for a in arrears]
    return Answer("\n".join(lines), "arrears", evidence)


def _answer_large_transactions(db: Session, employee: Employee, question: str,
                               locale: str) -> Answer:
    threshold = _extract_amount_paise(question)
    if threshold is None:
        return Answer("Please say an amount, for example 'show all transactions above 15000'."
                      if locale != "hi" else
                      "कृपया राशि बताएँ, जैसे '15000 से अधिक के सभी लेन-देन दिखाएँ'।",
                      "large_transactions")

    txns = (
        db.query(BankTransaction)
        .filter(BankTransaction.employee_id == employee.id,
                BankTransaction.amount_paise > threshold)
        .order_by(BankTransaction.txn_date)
        .all()
    )
    if not txns:
        return Answer(f"No transactions above {format_money(threshold)} were found."
                      if locale != "hi" else
                      f"{format_money(threshold)} से अधिक का कोई लेन-देन नहीं मिला।",
                      "large_transactions")

    lines = [f"{len(txns)} transaction(s) above {format_money(threshold)}:"
             if locale != "hi" else
             f"{format_money(threshold)} से अधिक के {len(txns)} लेन-देन:"]
    for t in txns:
        lines.append(f"  - {t.txn_date.isoformat()}  {format_money(t.amount_paise)}  "
                     f"{t.direction}  {(t.description or '')[:60]}")
    evidence = [{"type": "transaction", "label": t.txn_date.isoformat(),
                 "amount": format_money(t.amount_paise), "link": f"/transactions/{t.uid}"}
                for t in txns]
    return Answer("\n".join(lines), "large_transactions", evidence)


def _answer_documents(db: Session, employee: Employee, question: str,
                      year: int, locale: str) -> Answer:
    from . import document_agent

    month = _extract_month(question)
    rows = _recons(db, employee, year)
    if month:
        rows = [r for r in rows if r.period and r.period.month == month]
    if not rows:
        return Answer("I could not find that month." if locale != "hi" else "वह महीना नहीं मिला।",
                      "documents")

    recon = rows[-1]
    docs = document_agent.evidence_for(db, "Reconciliation", recon.id)
    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == recon.period_id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )

    lines = [f"Evidence for {recon.period.label}:" if locale != "hi"
             else f"{recon.period.label_hi} के प्रमाण:"]
    for a in allocations:
        t = a.transaction
        if t:
            lines.append(f"  - Bank credit {t.txn_date.isoformat()} {format_money(a.amount_paise)}"
                         + (f", reference {t.utr}" if t.utr else "")
                         + (f", from statement '{t.import_job_id and 'uploaded statement' or 'manual entry'}'"))
    for d in docs:
        lines.append(f"  - Document: {d.title} ({d.category})")
    if len(lines) == 1:
        lines.append("  No documents are linked to this month yet."
                     if locale != "hi" else "  इस माह से कोई दस्तावेज़ नहीं जुड़ा है।")

    evidence = [{"type": "document", "label": d.title, "link": f"/documents/{d.uid}"} for d in docs]
    return Answer("\n".join(lines), "documents", evidence)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def ask(db: Session, employee: Employee, question: str, locale: str = "en",
        use_llm: bool = True) -> Answer:
    """Answer a question about this employee's records."""
    if not question or not question.strip():
        return Answer("Please type a question." if locale != "hi" else "कृपया प्रश्न लिखें।",
                      "empty")

    intent = detect_intent(question)
    today = date.today()
    year = _extract_year(question, today.year)

    handlers = {
        "unpaid_months": lambda: _answer_unpaid(db, employee, year, locale),
        "pending_amount": lambda: _answer_pending(db, employee, locale),
        "arrears": lambda: _answer_arrears(db, employee, locale),
        "deductions": lambda: _answer_deductions(db, employee, locale),
        "unverified": lambda: _answer_unverified(db, employee, locale),
        "sanctioned": lambda: _answer_sanctioned(db, employee, year, locale),
        "calculation": lambda: _answer_calculation(db, employee, question, year, locale),
        "explain_month": lambda: _answer_calculation(db, employee, question, year, locale),
        "documents": lambda: _answer_documents(db, employee, question, year, locale),
        "total_received": lambda: _answer_total_received(db, employee, year, locale),
        "plain_summary": lambda: _answer_plain_summary(db, employee, year, locale),
        "annual_statement": lambda: _answer_plain_summary(db, employee, year, locale),
        "is_correct": lambda: _answer_is_correct(db, employee, question, year, locale),
        "large_transactions": lambda: _answer_large_transactions(db, employee, question, locale),
        "low_confidence": lambda: _answer_unverified(db, employee, locale),
    }

    answer = handlers.get(intent, lambda: _answer_general(db, employee, question, year, locale))()
    answer.followups = (SUGGESTED_QUESTIONS_HI if locale == "hi" else SUGGESTED_QUESTIONS_EN)[:4]

    # Optional narrative polish, strictly over the already-computed figures.
    if use_llm and llm.available() and answer.intent != "empty":
        polished = _polish(question, answer, locale)
        if polished:
            answer.text = polished
            answer.used_llm = True
    return answer


def _answer_general(db: Session, employee: Employee, question: str,
                    year: int, locale: str) -> Answer:
    """Fallback: give an overview and say plainly what was not understood."""
    summary = _answer_plain_summary(db, employee, year, locale)
    if locale == "hi":
        head = ("मैं आपका प्रश्न पूरी तरह नहीं समझ सका, इसलिए आपके रिकॉर्ड का सारांश दे रहा हूँ। "
                "आप नीचे दिए सुझावों में से कोई प्रश्न पूछ सकते हैं।\n\n")
    else:
        head = ("I could not fully understand that question, so here is a summary of your "
                "records. You can try one of the suggested questions below.\n\n")
    return Answer(head + summary.text, "general", summary.evidence)


def _polish(question: str, answer: Answer, locale: str) -> Optional[str]:
    """Ask the LLM to rephrase a computed answer without changing any figure."""
    language = "Hindi (simple, everyday Hindi)" if locale == "hi" else "English (plain, simple)"
    # Only the computed answer is sent. The evidence rows are deliberately
    # withheld: they contain figures that are correct but absent from the
    # answer, and a model that borrows one would trip the number-guard below
    # and have its whole response discarded. Omitting them also keeps the
    # request small, which matters on a free tier with a per-minute token cap.
    prompt = f"""A worker asked: "{question}"

The system has already computed the correct answer from their records:

--- COMPUTED ANSWER (every figure here is correct and must be preserved exactly) ---
{answer.text}
--- END ---

Rewrite the computed answer in {language} so it is easy for someone with limited
financial literacy to understand. Keep every amount, month name and status exactly
as given. Do not add any number that is not above. Do not add reassurance or blame.
Keep it under 140 words. Reply with the rewritten answer only."""

    result = llm.complete(prompt)
    if not result.used_llm or not result.text:
        return None

    # Guard: if the model introduced a figure that is not in our answer, discard
    # its output entirely rather than risk showing an invented number.
    original_numbers = set(re.findall(r"[\d,]+\.\d{2}", answer.text))
    produced_numbers = set(re.findall(r"[\d,]+\.\d{2}", result.text))
    if produced_numbers - original_numbers:
        return None
    return result.text
