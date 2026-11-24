"""AGENT 12 - Document Request (spec s35).

When evidence is missing, tell the user precisely which document would close
the gap -- and only the smallest useful set. "Do not overwhelm the user" is an
explicit requirement, so each request offers at most three alternatives, any
one of which is sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..enums import DeductionStatus, ReconOutcome, VerificationLevel
from ..models import Deduction, Employee, Reconciliation, SalaryPeriod
from ..money import format_money

AGENT_NAME = "document_request_agent"


@dataclass
class DocumentRequest:
    """One thing the user could upload, and why it helps."""

    reason_en: str
    reason_hi: str
    options_en: list[str] = field(default_factory=list)
    options_hi: list[str] = field(default_factory=list)
    category_hint: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    priority: int = 5   # lower sorts first

    def as_dict(self, locale: str = "en") -> dict:
        hi = locale == "hi"
        return {
            "reason": self.reason_hi if hi else self.reason_en,
            "options": self.options_hi if hi else self.options_en,
            "category_hint": self.category_hint,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "priority": self.priority,
        }


def for_reconciliation(recon: Reconciliation) -> list[DocumentRequest]:
    """What would resolve this period, ranked by usefulness."""
    requests: list[DocumentRequest] = []
    period = recon.period
    amount = format_money(abs(recon.difference_paise))

    if recon.outcome in {ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value,
                         ReconOutcome.SHORT_CREDIT.value,
                         ReconOutcome.SANCTIONED_PARTIALLY_CREDITED.value}:
        requests.append(DocumentRequest(
            reason_en=f"To verify the {amount} difference in {period.label}, upload any ONE of:",
            reason_hi=f"{period.label_hi} में {amount} के अंतर की पुष्टि के लिए, इनमें से कोई एक अपलोड करें:",
            options_en=["Payment order for this month",
                        "Salary slip or salary statement",
                        "Department payment record (PFMS / treasury print)"],
            options_hi=["इस माह का भुगतान आदेश",
                        "वेतन पर्ची या वेतन विवरण",
                        "विभागीय भुगतान रिकॉर्ड (PFMS / कोषागार प्रिंट)"],
            category_hint="PAYMENT_RECORD",
            entity_type="Reconciliation", entity_id=recon.id, priority=1,
        ))

    if recon.outcome == ReconOutcome.SANCTIONED_NOT_CREDITED.value:
        requests.append(DocumentRequest(
            reason_en=(f"{period.label} was sanctioned but no matching credit was found. "
                       "Upload any ONE of:"),
            reason_hi=(f"{period.label_hi} स्वीकृत था, पर कोई जमा नहीं मिली। "
                       "इनमें से कोई एक अपलोड करें:"),
            options_en=["Bank statement covering the expected credit date",
                        "Bank passbook page for this period",
                        "Payment status print from the paying office"],
            options_hi=["अपेक्षित तिथि वाला बैंक स्टेटमेंट",
                        "इस अवधि का पासबुक पृष्ठ",
                        "भुगतान कार्यालय से भुगतान स्थिति प्रिंट"],
            category_hint="BANK_STATEMENT",
            entity_type="Reconciliation", entity_id=recon.id, priority=1,
        ))

    if recon.outcome == ReconOutcome.NOT_PAID.value:
        requests.append(DocumentRequest(
            reason_en=f"No payment is recorded for {period.label}. Upload any ONE of:",
            reason_hi=f"{period.label_hi} का कोई भुगतान दर्ज नहीं है। इनमें से कोई एक अपलोड करें:",
            options_en=["Bank statement for this period",
                        "Bank passbook page",
                        "Sanction or payment order for this month"],
            options_hi=["इस अवधि का बैंक स्टेटमेंट",
                        "पासबुक पृष्ठ",
                        "इस माह का स्वीकृति या भुगतान आदेश"],
            category_hint="BANK_STATEMENT",
            entity_type="Reconciliation", entity_id=recon.id, priority=2,
        ))

    if recon.outcome == ReconOutcome.EXCESS_CREDIT.value:
        requests.append(DocumentRequest(
            reason_en=f"{amount} more than expected was received in {period.label}. Upload any ONE of:",
            reason_hi=f"{period.label_hi} में अपेक्षा से {amount} अधिक प्राप्त हुई। इनमें से कोई एक अपलोड करें:",
            options_en=["Arrear sanction order",
                        "Incentive or bonus sanction",
                        "Salary slip showing the extra amount"],
            options_hi=["बकाया स्वीकृति आदेश",
                        "प्रोत्साहन या बोनस स्वीकृति",
                        "अतिरिक्त राशि दर्शाने वाली वेतन पर्ची"],
            category_hint="SANCTION_ORDER",
            entity_type="Reconciliation", entity_id=recon.id, priority=3,
        ))

    if recon.outcome == ReconOutcome.NO_ENTITLEMENT_RULE.value:
        requests.append(DocumentRequest(
            reason_en="No salary rule is configured for this period. Upload:",
            reason_hi="इस अवधि के लिए कोई वेतन नियम नहीं है। अपलोड करें:",
            options_en=["The government order fixing the honorarium or salary rate"],
            options_hi=["मानदेय या वेतन दर निर्धारित करने वाला सरकारी आदेश"],
            category_hint="GOVERNMENT_ORDER",
            entity_type="Reconciliation", entity_id=recon.id, priority=1,
        ))

    # A period proven only by bank records still lacks official confirmation.
    if (recon.credit_verification_level >= VerificationLevel.BANK_STATEMENT
            and recon.sanction_verification_level < VerificationLevel.OFFICIAL_DOCUMENT
            and recon.outcome == ReconOutcome.FULLY_RECONCILED.value):
        requests.append(DocumentRequest(
            reason_en=(f"{period.label} matches your bank records, but there is no official "
                       "document confirming the payment was sanctioned. To verify it fully, upload:"),
            reason_hi=(f"{period.label_hi} आपके बैंक रिकॉर्ड से मेल खाता है, परंतु भुगतान की "
                       "आधिकारिक स्वीकृति का कोई दस्तावेज़ नहीं है। पूर्ण पुष्टि के लिए अपलोड करें:"),
            options_en=["Sanction order for this month", "Salary slip"],
            options_hi=["इस माह का स्वीकृति आदेश", "वेतन पर्ची"],
            category_hint="SANCTION_ORDER",
            entity_type="Reconciliation", entity_id=recon.id, priority=8,
        ))

    return requests


def for_deduction(deduction: Deduction) -> Optional[DocumentRequest]:
    if deduction.status != DeductionStatus.UNEXPLAINED.value:
        return None
    amount = format_money(deduction.amount_paise)
    period = deduction.period.label if deduction.period else "this period"
    period_hi = deduction.period.label_hi if deduction.period else "इस अवधि"
    return DocumentRequest(
        reason_en=f"A deduction of {amount} in {period} has no supporting record. Upload any ONE of:",
        reason_hi=f"{period_hi} में {amount} की कटौती का कोई प्रमाण नहीं है। इनमें से कोई एक अपलोड करें:",
        options_en=["Salary slip showing the deduction",
                    "Recovery or deduction order",
                    "Attendance record for this month"],
        options_hi=["कटौती दर्शाने वाली वेतन पर्ची",
                    "वसूली या कटौती आदेश",
                    "इस माह का उपस्थिति रिकॉर्ड"],
        category_hint="DEDUCTION_RECORD",
        entity_type="Deduction", entity_id=deduction.id, priority=2,
    )


def for_employee(db: Session, employee: Employee, limit: int = 5) -> list[DocumentRequest]:
    """The most useful documents this employee could supply right now.

    Capped and de-duplicated: a long list of asks is a list nobody acts on.
    """
    requests: list[DocumentRequest] = []

    recons = (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id == employee.id)
        .all()
    )
    for recon in sorted(recons, key=lambda r: -(r.period.sort_key if r.period else 0)):
        requests.extend(for_reconciliation(recon))

    deductions = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id,
                Deduction.status == DeductionStatus.UNEXPLAINED.value)
        .all()
    )
    for deduction in deductions:
        request = for_deduction(deduction)
        if request:
            requests.append(request)

    requests.sort(key=lambda r: r.priority)

    # Collapse repeats of the same ask so the user sees variety, not volume.
    seen: set[str] = set()
    unique: list[DocumentRequest] = []
    for request in requests:
        key = request.category_hint or request.reason_en[:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(request)
        if len(unique) >= limit:
            break
    return unique
