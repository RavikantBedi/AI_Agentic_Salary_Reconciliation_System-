"""AGENT 7 - Three-way and four-way reconciliation (spec s29, s30).

For each employee-period the engine compares:

    ENTITLEMENT  ->  SANCTION  ->  DISBURSEMENT  ->  BANK CREDIT

and produces a single, defensible outcome plus a plain-language explanation
in English and Hindi.

Two distinctions the specification insists on, and which this module keeps
rigorously separate:

* A **bank credit** proves money arrived. It does not prove the payment was
  officially sanctioned.
* A **sanction** proves authorisation. It does not prove the money arrived.

So verification is tracked per dimension, and "no sanction document" is
recorded as *absence of evidence* (``None``), never as a sanction of zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..enums import (
    AllocationStatus,
    Direction,
    PaymentClassification,
    ReconOutcome,
    RecordState,
    VerificationLevel,
)
from ..money import format_money
from ..models import (
    Adjustment,
    BankTransaction,
    Disbursement,
    Employee,
    Entitlement,
    PaymentAllocation,
    PaymentSanction,
    Reconciliation,
    SalaryPeriod,
)
from . import arrear_agent, deduction_agent, entitlement_agent

AGENT_NAME = "reconciliation_agent"


@dataclass
class ReconInputs:
    """Everything gathered before a verdict is reached."""

    entitlement: Optional[Entitlement] = None
    entitled_paise: int = 0
    sanctioned_paise: Optional[int] = None
    disbursed_paise: Optional[int] = None
    credited_paise: int = 0
    arrear_received_paise: int = 0
    documented_deduction_paise: int = 0
    expected_deduction_paise: int = 0
    unexplained_deduction_paise: int = 0
    adjustment_paise: int = 0
    allocations: list[PaymentAllocation] = field(default_factory=list)
    sanctions: list[PaymentSanction] = field(default_factory=list)
    disbursements: list[Disbursement] = field(default_factory=list)
    first_credit_date: Optional[date] = None
    reversed_paise: int = 0
    entitlement_problem: Optional[str] = None


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------
def is_period_due(period: SalaryPeriod, today: Optional[date] = None) -> bool:
    """Has this period's payment date passed yet?

    Nothing can be "missing", "short" or "in arrears" before it is due. Without
    this guard the current month permanently reports itself as unpaid, which
    would bury every real finding under a recurring false alarm.
    """
    today = today or date.today()
    if period.expected_pay_date:
        return period.expected_pay_date <= today
    return period.cycle_end < today


def gather(db: Session, employee: Employee, period: SalaryPeriod) -> ReconInputs:
    """Collect the four legs and the supporting ledgers for one period."""
    inputs = ReconInputs()

    # --- Leg 1: entitlement ---
    entitlement = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id,
                Entitlement.period_id == period.id,
                Entitlement.is_current.is_(True))
        .first()
    )
    if entitlement is None:
        result, entitlement = entitlement_agent.calculate_and_store(db, employee, period)
        if not result.ok:
            inputs.entitlement_problem = result.problem
    inputs.entitlement = entitlement
    inputs.entitled_paise = entitlement.net_paise if entitlement else 0

    # --- Leg 2: official sanction. Absence stays None, never zero. ---
    sanctions = (
        db.query(PaymentSanction)
        .filter(PaymentSanction.employee_id == employee.id,
                PaymentSanction.period_id == period.id)
        .all()
    )
    inputs.sanctions = sanctions
    if sanctions:
        inputs.sanctioned_paise = sum(s.amount_paise for s in sanctions)

    # --- Leg 3: disbursement ---
    disbursements = (
        db.query(Disbursement)
        .filter(Disbursement.employee_id == employee.id,
                Disbursement.period_id == period.id)
        .all()
    )
    inputs.disbursements = disbursements
    successful = [d for d in disbursements if d.lifecycle in {"SUCCESSFUL", "PENDING"}]
    if disbursements:
        inputs.disbursed_paise = sum(d.amount_paise for d in successful)

    # --- Leg 4: actual bank credit ---
    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == period.id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    inputs.allocations = allocations

    # Money from a reversed credit never arrived, so it can never count as
    # received -- whatever the allocation says, and whoever created it.
    #
    # The automatic path rejects such allocations, but a person can confirm one
    # by hand (assigning a month to a credit that was later clawed back). Were
    # that allowed to count, the ledger would report money the employee does
    # not have, which is precisely the error this system exists to catch. The
    # test is applied here, at the point the total is formed, so no caller can
    # bypass it.
    def _arrived(allocation) -> bool:
        txn = allocation.transaction
        return not (txn is not None and txn.is_reversed)

    live = [a for a in allocations if _arrived(a)]

    salary_like = [a for a in live if a.kind != PaymentClassification.ARREAR.value]
    inputs.credited_paise = sum(a.amount_paise for a in salary_like)
    inputs.arrear_received_paise = sum(
        a.amount_paise for a in live if a.kind == PaymentClassification.ARREAR.value
    )

    credit_dates = [
        a.transaction.txn_date for a in salary_like
        if a.transaction and a.transaction.direction == Direction.CREDIT.value
    ]
    inputs.first_credit_date = min(credit_dates) if credit_dates else None

    # Reversed credits that were once allocated to this period.
    reversed_txns = (
        db.query(BankTransaction)
        .join(PaymentAllocation, PaymentAllocation.transaction_id == BankTransaction.id)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == period.id,
                BankTransaction.is_reversed.is_(True))
        .all()
    )
    inputs.reversed_paise = sum(t.amount_paise for t in reversed_txns)

    # --- Supporting ledgers ---
    deduction_agent.sync_rule_deductions(db, employee, period, entitlement)
    analysis = deduction_agent.analyse_gap(
        db, employee, period, entitlement, inputs.credited_paise,
        period_due=is_period_due(period),
    )
    inputs.documented_deduction_paise = analysis.documented_paise
    inputs.expected_deduction_paise = analysis.expected_paise
    inputs.unexplained_deduction_paise = analysis.unexplained_paise

    adjustments = (
        db.query(Adjustment)
        .filter(Adjustment.employee_id == employee.id, Adjustment.period_id == period.id)
        .all()
    )
    inputs.adjustment_paise = sum(a.delta_paise for a in adjustments)

    return inputs


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def decide_outcome(inputs: ReconInputs, period: SalaryPeriod) -> str:
    """Classify the period from the four legs.

    Ordering matters: the most specific, most actionable diagnosis wins.
    """
    tol = settings.match_tolerance_paise

    if inputs.entitlement is None or inputs.entitlement_problem:
        return ReconOutcome.NO_ENTITLEMENT_RULE.value

    entitled = inputs.entitled_paise
    credited = inputs.credited_paise
    sanctioned = inputs.sanctioned_paise

    # Money arrived and was then taken back.
    if inputs.reversed_paise > 0 and credited <= tol:
        return ReconOutcome.PAYMENT_REVERSED.value

    # Expected cash = entitlement minus deductions we can actually justify.
    expected_cash = entitled - inputs.documented_deduction_paise
    difference = expected_cash - credited

    if credited <= tol:
        # Nothing arrived yet. If the pay date has not passed, that is normal.
        if not is_period_due(period):
            return ReconOutcome.AWAITING_CONFIRMATION.value
        if sanctioned is not None and sanctioned > tol:
            return ReconOutcome.SANCTIONED_NOT_CREDITED.value
        return ReconOutcome.NOT_PAID.value

    if difference < -tol:
        return ReconOutcome.EXCESS_CREDIT.value

    if abs(difference) <= tol:
        # Fully paid in cash terms. If a sanction record exists and is short,
        # the sanction itself is still worth flagging.
        if sanctioned is not None and (entitled - sanctioned) > tol:
            return ReconOutcome.SHORT_SANCTION.value
        return ReconOutcome.FULLY_RECONCILED.value

    # Short. Decide WHERE it went short.
    if sanctioned is not None:
        if (entitled - sanctioned) > tol and abs(sanctioned - credited) <= tol:
            # The authority approved less than the entitlement, and paid what
            # it approved -- the problem is upstream of the bank.
            return ReconOutcome.SHORT_SANCTION.value
        if (sanctioned - credited) > tol:
            return ReconOutcome.SANCTIONED_PARTIALLY_CREDITED.value

    if inputs.unexplained_deduction_paise > tol:
        return ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value

    return ReconOutcome.SHORT_CREDIT.value


def verification_levels(db: Session, inputs: ReconInputs) -> tuple[int, int, int]:
    """Per-dimension verification: (credit, sanction, entitlement basis)."""
    credit_level = VerificationLevel.USER_CLAIM
    if inputs.allocations:
        txns = [a.transaction for a in inputs.allocations if a.transaction]
        if txns:
            if all(t.is_manual_entry or t.is_cash for t in txns):
                # A manually recorded cash payment is never as strong as an
                # independently verified bank credit (spec s25).
                credit_level = VerificationLevel.USER_CLAIM
            else:
                credit_level = max(t.verification_level for t in txns)

    sanction_level = VerificationLevel.USER_CLAIM
    if inputs.sanctions:
        sanction_level = max(s.verification_level for s in inputs.sanctions)
    if inputs.disbursements:
        sanction_level = max(sanction_level,
                             max(d.verification_level for d in inputs.disbursements))

    entitlement_level = entitlement_agent.entitlement_verification_level(inputs.entitlement)
    return credit_level, sanction_level, entitlement_level


# ---------------------------------------------------------------------------
# Explanation (spec s33, s60, s92)
# ---------------------------------------------------------------------------
def build_explanation(
    employee: Employee,
    period: SalaryPeriod,
    inputs: ReconInputs,
    outcome: str,
    currency: str = "INR",
) -> tuple[str, str, str, str]:
    """Return ``(explanation_en, explanation_hi, action_en, action_hi)``.

    Never returns a bare "mismatch detected": every explanation states the
    figures, the difference, what is known about the cause, and what to do.
    """
    m = lambda p: format_money(p, currency)  # noqa: E731
    expected_cash = inputs.entitled_paise - inputs.documented_deduction_paise
    difference = expected_cash - inputs.credited_paise

    sanction_en = m(inputs.sanctioned_paise) if inputs.sanctioned_paise is not None else "not on record"
    sanction_hi = m(inputs.sanctioned_paise) if inputs.sanctioned_paise is not None else "रिकॉर्ड में नहीं"

    head_en = (
        f"{period.label} {('honorarium' if 'HONORARIUM' in (employee.designation.name or '').upper() else 'salary') if employee.designation else 'salary'}\n"
        f"Expected: {m(inputs.entitled_paise)}\n"
        f"Officially sanctioned: {sanction_en}\n"
        f"Bank credited: {m(inputs.credited_paise)}"
    )
    head_hi = (
        f"{period.label_hi} का भुगतान\n"
        f"अपेक्षित: {m(inputs.entitled_paise)}\n"
        f"आधिकारिक रूप से स्वीकृत: {sanction_hi}\n"
        f"बैंक में जमा: {m(inputs.credited_paise)}"
    )

    if inputs.documented_deduction_paise:
        head_en += f"\nAuthorised deductions: {m(inputs.documented_deduction_paise)}"
        head_hi += f"\nअधिकृत कटौती: {m(inputs.documented_deduction_paise)}"

    if difference:
        head_en += f"\nDifference: {m(abs(difference))} {'short' if difference > 0 else 'extra'}"
        head_hi += f"\nअंतर: {m(abs(difference))} {'कम' if difference > 0 else 'अधिक'}"

    if outcome == ReconOutcome.FULLY_RECONCILED.value:
        body_en = "This month is fully reconciled. The amount received matches the amount due."
        body_hi = "इस महीने का पूरा मिलान हो गया है। प्राप्त राशि देय राशि के बराबर है।"
        action_en = "No action is needed."
        action_hi = "कोई कार्रवाई आवश्यक नहीं है।"

    elif outcome == ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value:
        body_en = (
            f"Possible explanation: no authorised deduction record was found for "
            f"{m(inputs.unexplained_deduction_paise)}.\n"
            "Current status: UNEXPLAINED SHORT CREDIT."
        )
        body_hi = (
            f"संभावित कारण: {m(inputs.unexplained_deduction_paise)} की कटौती का कोई "
            "अधिकृत प्रमाण उपलब्ध रिकॉर्ड में नहीं मिला।\n"
            "वर्तमान स्थिति: बिना कारण कम भुगतान।"
        )
        action_en = ("Check the payment or sanction record for this month, or contact the "
                     "concerned office. You can also upload a salary slip or payment order to verify.")
        action_hi = ("इस महीने का भुगतान या स्वीकृति रिकॉर्ड जाँचें, या संबंधित कार्यालय से "
                     "संपर्क करें। आप वेतन पर्ची या भुगतान आदेश भी अपलोड कर सकते हैं।")

    elif outcome == ReconOutcome.SHORT_SANCTION.value:
        gap = inputs.entitled_paise - (inputs.sanctioned_paise or 0)
        body_en = (
            f"The amount officially sanctioned ({sanction_en}) is {m(gap)} less than the "
            "entitlement calculated from the applicable rule. The money that was sanctioned "
            "appears to have been paid correctly."
        )
        body_hi = (
            f"आधिकारिक रूप से स्वीकृत राशि ({sanction_hi}) नियम के अनुसार बनने वाली राशि से "
            f"{m(gap)} कम है। जो राशि स्वीकृत हुई, वह सही रूप से भुगतान की गई प्रतीत होती है।"
        )
        action_en = ("The shortfall is at the sanction stage, not the bank. Ask the sanctioning "
                     "office why the full entitlement was not sanctioned.")
        action_hi = ("कमी स्वीकृति के स्तर पर है, बैंक में नहीं। स्वीकृति कार्यालय से पूछें कि "
                     "पूरी राशि क्यों स्वीकृत नहीं हुई।")

    elif outcome == ReconOutcome.SANCTIONED_NOT_CREDITED.value:
        body_en = (
            f"{sanction_en} was officially sanctioned for this month, but no matching credit "
            "has been found in the bank records available to this system."
        )
        body_hi = (
            f"इस महीने के लिए {sanction_hi} स्वीकृत हुई, परंतु उपलब्ध बैंक रिकॉर्ड में इसका "
            "कोई संबंधित भुगतान नहीं मिला।"
        )
        action_en = ("Upload the bank statement covering the expected credit date. If the money "
                     "genuinely did not arrive, raise this with the paying office.")
        action_hi = ("अपेक्षित तिथि वाला बैंक स्टेटमेंट अपलोड करें। यदि राशि वास्तव में नहीं आई, "
                     "तो भुगतान कार्यालय में शिकायत दर्ज करें।")

    elif outcome == ReconOutcome.SANCTIONED_PARTIALLY_CREDITED.value:
        gap = (inputs.sanctioned_paise or 0) - inputs.credited_paise
        body_en = (
            f"{sanction_en} was sanctioned but only {m(inputs.credited_paise)} reached the bank "
            f"account -- a shortfall of {m(gap)} between sanction and credit."
        )
        body_hi = (
            f"{sanction_hi} स्वीकृत हुई, परंतु बैंक खाते में केवल {m(inputs.credited_paise)} "
            f"पहुँची — स्वीकृति और जमा के बीच {m(gap)} का अंतर है।"
        )
        action_en = "Ask the paying office for the payment/disbursement status for this sanction."
        action_hi = "भुगतान कार्यालय से इस स्वीकृति की भुगतान स्थिति की जानकारी लें।"

    elif outcome == ReconOutcome.EXCESS_CREDIT.value:
        extra = abs(difference)
        body_en = (
            f"EXTRA CREDIT DETECTED: {m(extra)} more than expected was received. "
            "Possible reasons include arrears, an incentive, a correction of an earlier "
            "shortfall, a bonus, or a duplicate payment. The reason has not been established."
        )
        body_hi = (
            f"अतिरिक्त राशि प्राप्त: अपेक्षा से {m(extra)} अधिक जमा हुई है। इसका कारण बकाया, "
            "प्रोत्साहन राशि, पहले की कमी का सुधार, बोनस, या दोहरा भुगतान हो सकता है। "
            "कारण अभी स्थापित नहीं हुआ है।"
        )
        action_en = ("Do not treat this as settled income yet. Upload any payment order or "
                     "arrear sanction that explains the extra amount.")
        action_hi = ("इसे अभी अंतिम आय न मानें। अतिरिक्त राशि से संबंधित भुगतान आदेश या बकाया "
                     "स्वीकृति अपलोड करें।")

    elif outcome == ReconOutcome.NOT_PAID.value:
        body_en = ("No payment has been recorded for this month in the records available to "
                   "this system.")
        body_hi = "उपलब्ध रिकॉर्ड में इस महीने का कोई भुगतान दर्ज नहीं है।"
        action_en = ("Upload the bank statement for this period. If no credit exists, this month "
                     "may be genuinely unpaid and should be taken up with the concerned office.")
        action_hi = ("इस अवधि का बैंक स्टेटमेंट अपलोड करें। यदि कोई जमा नहीं है, तो यह महीना "
                     "वास्तव में अवैतनिक हो सकता है और इसे कार्यालय में उठाया जाना चाहिए।")

    elif outcome == ReconOutcome.PAYMENT_REVERSED.value:
        body_en = (f"A credit of {m(inputs.reversed_paise)} was received and later reversed. "
                   "The net amount received for this month is zero.")
        body_hi = (f"{m(inputs.reversed_paise)} की राशि जमा हुई थी, जो बाद में वापस हो गई। "
                   "इस महीने की शुद्ध प्राप्त राशि शून्य है।")
        action_en = "Ask the paying office why the payment was reversed and when it will be reissued."
        action_hi = "भुगतान कार्यालय से पूछें कि भुगतान वापस क्यों हुआ और दोबारा कब होगा।"

    elif outcome == ReconOutcome.NO_ENTITLEMENT_RULE.value:
        problem = inputs.entitlement_problem or "No applicable salary rule was found."
        body_en = f"An expected amount cannot be calculated. {problem}"
        body_hi = ("अपेक्षित राशि की गणना नहीं की जा सकती। इस कर्मचारी और अवधि के लिए कोई "
                   "लागू वेतन नियम नहीं मिला।")
        action_en = ("An administrator needs to configure the salary rule for this employee, "
                     "supported by the relevant government order.")
        action_hi = ("प्रशासक को संबंधित सरकारी आदेश के आधार पर इस कर्मचारी का वेतन नियम "
                     "कॉन्फ़िगर करना होगा।")

    elif outcome == ReconOutcome.AWAITING_CONFIRMATION.value:
        body_en = "This month's payment is not yet due, or has not yet been recorded."
        body_hi = "इस महीने का भुगतान अभी देय नहीं है, या अभी दर्ज नहीं हुआ है।"
        action_en = "No action is needed yet."
        action_hi = "अभी कोई कार्रवाई आवश्यक नहीं है।"

    else:  # SHORT_CREDIT and any residual case
        body_en = (f"{m(abs(difference))} less than expected was received for this month. "
                   "The reason has not been established from the available records.")
        body_hi = (f"इस महीने अपेक्षा से {m(abs(difference))} कम राशि प्राप्त हुई है। "
                   "उपलब्ध रिकॉर्ड से इसका कारण स्थापित नहीं हुआ है।")
        action_en = "Upload a salary slip, payment order or department statement to verify this difference."
        action_hi = "इस अंतर की पुष्टि के लिए वेतन पर्ची, भुगतान आदेश या विभागीय विवरण अपलोड करें।"

    # --- Delay note ---
    if inputs.first_credit_date and period.expected_pay_date:
        delay = (inputs.first_credit_date - period.expected_pay_date).days
        if delay > settings.delay_threshold_days:
            body_en += (f"\n\nThis payment arrived {delay} days after the expected date of "
                        f"{period.expected_pay_date.isoformat()}.")
            body_hi += f"\n\nयह भुगतान अपेक्षित तिथि से {delay} दिन बाद प्राप्त हुआ।"

    # --- Evidence caveat: bank credit alone is not official verification ---
    if inputs.sanctioned_paise is None and inputs.credited_paise > 0:
        body_en += ("\n\nNote: this is based on bank records only. A bank credit shows that money "
                    "arrived; it does not by itself confirm the payment was officially sanctioned.")
        body_hi += ("\n\nध्यान दें: यह केवल बैंक रिकॉर्ड पर आधारित है। बैंक में जमा यह दर्शाता है "
                    "कि पैसा आया, परंतु यह अपने आप यह सिद्ध नहीं करता कि भुगतान आधिकारिक रूप से "
                    "स्वीकृत था।")

    return f"{head_en}\n\n{body_en}", f"{head_hi}\n\n{body_hi}", action_en, action_hi


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def reconcile(db: Session, employee: Employee, period: SalaryPeriod) -> Reconciliation:
    """Reconcile one employee-period and persist the result."""
    inputs = gather(db, employee, period)
    outcome = decide_outcome(inputs, period)
    credit_level, sanction_level, basis_level = verification_levels(db, inputs)

    currency = employee.organization.currency if employee.organization else "INR"
    expl_en, expl_hi, action_en, action_hi = build_explanation(
        employee, period, inputs, outcome, currency
    )

    # Keep the arrear ledger in step with this verdict. A period that is not
    # yet due cannot be in arrears, so it is skipped entirely.
    if is_period_due(period):
        arrear_agent.sync_for_period(
            db, employee, period, inputs.entitlement,
            inputs.credited_paise, inputs.documented_deduction_paise,
        )
    outstanding = arrear_agent.total_outstanding(db, employee)

    delay_days = None
    if inputs.first_credit_date and period.expected_pay_date:
        delay_days = (inputs.first_credit_date - period.expected_pay_date).days

    expected_cash = inputs.entitled_paise - inputs.documented_deduction_paise
    difference = expected_cash - inputs.credited_paise

    record = (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id == employee.id,
                Reconciliation.period_id == period.id)
        .first()
    )
    if record is None:
        record = Reconciliation(
            organization_id=employee.organization_id,
            employee_id=employee.id,
            period_id=period.id,
        )
        db.add(record)

    record.entitlement_id = inputs.entitlement.id if inputs.entitlement else None
    record.entitled_paise = inputs.entitled_paise
    record.sanctioned_paise = inputs.sanctioned_paise
    record.disbursed_paise = inputs.disbursed_paise
    record.credited_paise = inputs.credited_paise
    record.deducted_paise = inputs.documented_deduction_paise + inputs.expected_deduction_paise
    record.unexplained_deduction_paise = inputs.unexplained_deduction_paise
    record.arrear_received_paise = inputs.arrear_received_paise
    record.arrear_outstanding_paise = outstanding
    record.adjustment_paise = inputs.adjustment_paise
    record.difference_paise = difference
    record.outcome = outcome
    record.credit_verification_level = credit_level
    record.sanction_verification_level = sanction_level
    record.entitlement_verification_level = basis_level
    record.expected_pay_date = period.expected_pay_date
    record.first_credit_date = inputs.first_credit_date
    record.delay_days = delay_days
    record.explanation_en = expl_en
    record.explanation_hi = expl_hi
    record.recommended_action_en = action_en
    record.recommended_action_hi = action_hi
    record.computed_at = utcnow()
    record.state = _record_state(outcome, credit_level, sanction_level)
    record.evidence_json = {
        "allocations": [
            {
                "uid": a.uid,
                "amount_paise": a.amount_paise,
                "kind": a.kind,
                "match_score": a.match_score,
                "status": a.status,
                "transaction_uid": a.transaction.uid if a.transaction else None,
                "credit_date": a.transaction.txn_date.isoformat() if a.transaction else None,
                "utr": a.transaction.utr if a.transaction else None,
            }
            for a in inputs.allocations
        ],
        "sanctions": [
            {"uid": s.uid, "number": s.sanction_number, "amount_paise": s.amount_paise,
             "date": s.sanction_date.isoformat() if s.sanction_date else None,
             "verification_level": s.verification_level}
            for s in inputs.sanctions
        ],
        "disbursements": [
            {"uid": d.uid, "amount_paise": d.amount_paise, "channel": d.channel,
             "lifecycle": d.lifecycle, "utr": d.utr}
            for d in inputs.disbursements
        ],
        "entitlement": {
            "uid": inputs.entitlement.uid if inputs.entitlement else None,
            "rule": inputs.entitlement.rule.label if inputs.entitlement and inputs.entitlement.rule else None,
            "gross_paise": inputs.entitlement.gross_paise if inputs.entitlement else 0,
            "net_paise": inputs.entitlement.net_paise if inputs.entitlement else 0,
        },
        "deductions": {
            "documented_paise": inputs.documented_deduction_paise,
            "expected_paise": inputs.expected_deduction_paise,
            "unexplained_paise": inputs.unexplained_deduction_paise,
        },
        "reversed_paise": inputs.reversed_paise,
        "problem": inputs.entitlement_problem,
    }

    db.flush()
    return record


def _record_state(outcome: str, credit_level: int, sanction_level: int) -> str:
    """Map an outcome plus evidence strength onto the universal state (s83)."""
    if outcome == ReconOutcome.NO_ENTITLEMENT_RULE.value:
        return RecordState.REVIEW_REQUIRED.value
    if outcome == ReconOutcome.FULLY_RECONCILED.value:
        if credit_level >= VerificationLevel.BANK_STATEMENT and sanction_level >= VerificationLevel.OFFICIAL_DOCUMENT:
            return RecordState.VERIFIED.value
        if credit_level >= VerificationLevel.BANK_STATEMENT:
            return RecordState.PARTIALLY_VERIFIED.value
        return RecordState.RECONCILED.value
    if outcome == ReconOutcome.AWAITING_CONFIRMATION.value:
        return RecordState.IMPORTED.value
    return RecordState.REVIEW_REQUIRED.value


def reconcile_employee(db: Session, employee: Employee,
                       periods: Optional[list[SalaryPeriod]] = None) -> list[Reconciliation]:
    """Reconcile every period for one employee, oldest first.

    Order matters: an arrear created in an early month must exist before a
    later month's payment can settle it.
    """
    if periods is None:
        periods = (
            db.query(SalaryPeriod)
            .filter(SalaryPeriod.organization_id == employee.organization_id)
            .all()
        )
    ordered = sorted(periods, key=lambda p: p.sort_key)
    return [reconcile(db, employee, period) for period in ordered]
