"""Bilingual interface support: English and Hindi (spec s60).

Hindi strings are written in *simple* Hindi so a non-technical worker can read
them, while widely-understood financial terms are kept where a translation
would be less clear than the familiar word.

Usage in templates:  ``{{ t('dashboard.total_entitled') }}``
"""

from __future__ import annotations

from typing import Any

from .enums import Locale

# ---------------------------------------------------------------------------
# Translation table.  key -> (english, hindi)
# ---------------------------------------------------------------------------
STRINGS: dict[str, tuple[str, str]] = {
    # --- App / navigation (spec s104) ---
    "app.name": ("Salary & Payment Reconciliation", "वेतन एवं भुगतान मिलान"),
    "app.tagline": ("Your payments, checked and explained", "आपके भुगतान, जाँचे और समझाए गए"),
    "nav.dashboard": ("Dashboard", "डैशबोर्ड"),
    "nav.action_center": ("Action Center", "कार्य केंद्र"),
    "nav.my_salary": ("My Salary", "मेरा वेतन"),
    "nav.transactions": ("Transactions", "लेन-देन"),
    "nav.arrears": ("Arrears", "बकाया"),
    "nav.deductions": ("Deductions", "कटौती"),
    "nav.benefits": ("Benefits", "सामग्री लाभ"),
    "nav.verification": ("Verification", "सत्यापन"),
    "nav.documents": ("Documents", "दस्तावेज़"),
    "nav.disputes": ("Disputes", "शिकायत"),
    "nav.reports": ("Reports", "रिपोर्ट"),
    "nav.assistant": ("AI Assistant", "AI सहायक"),
    "nav.settings": ("Settings", "सेटिंग्स"),
    "nav.employees": ("Employees", "कर्मचारी"),
    "nav.rules": ("Salary Rules", "वेतन नियम"),
    "nav.admin": ("Administration", "प्रशासन"),
    "nav.audit": ("Audit Log", "ऑडिट लॉग"),
    "nav.anomalies": ("Anomalies", "असामान्य रिकॉर्ड"),
    "nav.data_quality": ("Data Quality", "डेटा गुणवत्ता"),
    "nav.logout": ("Sign out", "साइन आउट"),
    "nav.import": ("Import", "आयात"),

    # --- Auth ---
    "auth.login": ("Sign in", "साइन इन करें"),
    "auth.email": ("Email", "ईमेल"),
    "auth.password": ("Password", "पासवर्ड"),
    "auth.remember": ("Keep me signed in", "मुझे साइन इन रखें"),
    "auth.invalid": ("Email or password is incorrect.", "ईमेल या पासवर्ड ग़लत है।"),
    "auth.locked": ("Too many attempts. Please try again later.",
                    "बहुत अधिक प्रयास। कृपया बाद में पुनः प्रयास करें।"),
    "auth.welcome": ("Welcome back", "आपका पुनः स्वागत है"),

    # --- Dashboard cards (spec s38) ---
    "dashboard.total_entitled": ("Total Entitled", "कुल देय राशि"),
    "dashboard.total_received": ("Total Received", "कुल प्राप्त राशि"),
    "dashboard.total_pending": ("Total Pending", "कुल लंबित राशि"),
    "dashboard.total_deductions": ("Total Deductions", "कुल कटौती"),
    "dashboard.total_arrears": ("Total Arrears", "कुल बकाया"),
    "dashboard.unverified": ("Unverified", "अप्रमाणित"),
    "dashboard.payment_health": ("Payment Health", "भुगतान स्थिति"),
    "dashboard.timeline": ("Monthly Timeline", "मासिक विवरण"),
    "dashboard.is_salary_correct": ("Is my salary correct?", "क्या मेरा वेतन सही है?"),
    "dashboard.process_month": ("Process this month", "इस महीने की प्रक्रिया चलाएँ"),

    # --- Health states ---
    "health.GOOD": ("Good", "ठीक है"),
    "health.ATTENTION": ("Needs attention", "ध्यान देने योग्य"),
    "health.ACTION_REQUIRED": ("Action required", "कार्रवाई आवश्यक"),
    "health.UNKNOWN": ("Not enough information", "पर्याप्त जानकारी नहीं"),

    # --- Common financial words ---
    "money.expected": ("Expected", "अपेक्षित"),
    "money.sanctioned": ("Officially sanctioned", "आधिकारिक रूप से स्वीकृत"),
    "money.disbursed": ("Disbursed", "जारी किया गया"),
    "money.received": ("Received", "प्राप्त"),
    "money.credited": ("Bank credited", "बैंक में जमा"),
    "money.difference": ("Difference", "अंतर"),
    "money.gross": ("Gross", "सकल"),
    "money.net": ("Net", "शुद्ध"),
    "money.deduction": ("Deduction", "कटौती"),
    "money.arrear": ("Arrear", "बकाया"),
    "money.outstanding": ("Outstanding", "शेष देय"),
    "money.unallocated": ("Unallocated", "अवंटित नहीं"),
    "money.amount": ("Amount", "राशि"),
    "money.balance": ("Balance", "शेष"),

    # --- Table headers / fields ---
    "field.month": ("Salary month", "वेतन माह"),
    "field.date": ("Date", "दिनांक"),
    "field.credit_date": ("Credit date", "जमा दिनांक"),
    "field.value_date": ("Value date", "मूल्य दिनांक"),
    "field.narration": ("Narration", "विवरण"),
    "field.utr": ("UTR / Reference", "UTR / संदर्भ"),
    "field.bank": ("Bank", "बैंक"),
    "field.status": ("Status", "स्थिति"),
    "field.classification": ("Classification", "वर्गीकरण"),
    "field.confidence": ("Confidence", "विश्वास स्तर"),
    "field.evidence": ("Evidence", "प्रमाण"),
    "field.employee": ("Employee", "कर्मचारी"),
    "field.employee_code": ("Employee ID", "कर्मचारी आईडी"),
    "field.designation": ("Designation", "पदनाम"),
    "field.department": ("Department", "विभाग"),
    "field.scheme": ("Scheme", "योजना"),
    "field.location": ("Location", "स्थान"),
    "field.delay": ("Delay", "विलंब"),
    "field.days": ("days", "दिन"),
    "field.reason": ("Reason", "कारण"),
    "field.action": ("Action", "कार्रवाई"),
    "field.quantity": ("Quantity", "मात्रा"),
    "field.verification_level": ("Verification level", "सत्यापन स्तर"),

    # --- Reconciliation outcomes (spec s29) ---
    "outcome.FULLY_RECONCILED": ("Fully reconciled", "पूरी तरह मिलान"),
    "outcome.SHORT_CREDIT": ("Short credit", "कम राशि प्राप्त"),
    "outcome.UNEXPLAINED_SHORT_CREDIT": ("Unexplained short credit", "बिना कारण कम राशि"),
    "outcome.SHORT_SANCTION": ("Short sanction", "कम स्वीकृति"),
    "outcome.SANCTIONED_NOT_CREDITED": ("Sanctioned but not credited", "स्वीकृत, पर जमा नहीं"),
    "outcome.SANCTIONED_PARTIALLY_CREDITED": ("Sanctioned, partly credited", "स्वीकृत, आंशिक जमा"),
    "outcome.EXCESS_CREDIT": ("Extra amount credited", "अतिरिक्त राशि जमा"),
    "outcome.NOT_PAID": ("Not paid", "भुगतान नहीं हुआ"),
    "outcome.PAYMENT_REVERSED": ("Payment reversed", "भुगतान वापस"),
    "outcome.NO_ENTITLEMENT_RULE": ("No salary rule found", "कोई वेतन नियम नहीं मिला"),
    "outcome.INSUFFICIENT_EVIDENCE": ("Insufficient evidence", "पर्याप्त प्रमाण नहीं"),
    "outcome.AWAITING_CONFIRMATION": ("Awaiting your confirmation", "आपकी पुष्टि की प्रतीक्षा"),

    # --- Status badges (spec s61: colour is never the only signal) ---
    "badge.matched": ("MATCHED", "मिलान हुआ"),
    "badge.review": ("REVIEW", "जाँच करें"),
    "badge.unresolved": ("UNRESOLVED", "अनसुलझा"),
    "badge.verified": ("VERIFIED", "प्रमाणित"),
    "badge.unverified": ("UNVERIFIED", "अप्रमाणित"),

    # --- Action center (spec s64) ---
    "action.title": ("What needs my attention?", "किन बातों पर ध्यान देना है?"),
    "action.critical": ("Critical", "अत्यावश्यक"),
    "action.review": ("Review", "जाँच"),
    "action.reconciled": ("Reconciled", "मिलान पूर्ण"),
    "action.none": ("Nothing needs your attention right now.",
                    "अभी किसी बात पर ध्यान देने की आवश्यकता नहीं है।"),

    # --- Evidence / verification wording ---
    "evidence.insufficient": ("Insufficient evidence", "पर्याप्त प्रमाण नहीं"),
    "evidence.upload_prompt": ("Upload a document to verify this",
                               "इसे प्रमाणित करने के लिए दस्तावेज़ अपलोड करें"),
    "evidence.bank_only": (
        "A bank credit proves money arrived. It does not by itself prove the "
        "payment was officially sanctioned.",
        "बैंक में जमा यह सिद्ध करता है कि पैसा आया। यह अपने आप यह सिद्ध नहीं करता "
        "कि भुगतान आधिकारिक रूप से स्वीकृत था।"),

    # --- Buttons ---
    "btn.upload": ("Upload", "अपलोड करें"),
    "btn.save": ("Save", "सहेजें"),
    "btn.cancel": ("Cancel", "रद्द करें"),
    "btn.confirm": ("Confirm", "पुष्टि करें"),
    "btn.approve": ("Approve", "स्वीकृत करें"),
    "btn.reject": ("Reject", "अस्वीकार करें"),
    "btn.correct": ("Correct this", "सुधार करें"),
    "btn.view": ("View", "देखें"),
    "btn.view_evidence": ("View evidence", "प्रमाण देखें"),
    "btn.explain": ("Explain this", "समझाएँ"),
    "btn.raise_dispute": ("Raise a dispute", "शिकायत दर्ज करें"),
    "btn.download": ("Download", "डाउनलोड करें"),
    "btn.ask": ("Ask", "पूछें"),
    "btn.split": ("Split", "विभाजित करें"),
    "btn.assign_month": ("Assign month", "माह निर्धारित करें"),
    "btn.back": ("Back", "वापस"),

    # --- Assistant (spec s105) ---
    "assistant.placeholder": ("Ask about your payments...", "अपने भुगतान के बारे में पूछें..."),
    "assistant.title": ("AI Assistant", "AI सहायक"),
    "assistant.grounded": ("Every answer is based on your records.",
                           "हर उत्तर आपके रिकॉर्ड पर आधारित है।"),
    "assistant.q1": ("Why is my salary lower?", "मेरा वेतन कम क्यों है?"),
    "assistant.q2": ("Show unpaid months.", "अवैतनिक महीने दिखाएँ।"),
    "assistant.q3": ("How much is pending?", "कितनी राशि लंबित है?"),
    "assistant.q4": ("Show my deductions.", "मेरी कटौती दिखाएँ।"),
    "assistant.q5": ("Generate my annual statement.", "मेरा वार्षिक विवरण बनाएँ।"),
    "assistant.q6": ("Which transactions are not verified?", "कौन से लेन-देन प्रमाणित नहीं हैं?"),

    # --- Generic ---
    "common.yes": ("Yes", "हाँ"),
    "common.no": ("No", "नहीं"),
    "common.none": ("None", "कोई नहीं"),
    "common.unknown": ("Not known", "ज्ञात नहीं"),
    "common.loading": ("Loading...", "लोड हो रहा है..."),
    "common.no_records": ("No records found.", "कोई रिकॉर्ड नहीं मिला।"),
    "common.total": ("Total", "कुल"),
    "common.search": ("Search", "खोजें"),
    "common.filter": ("Filter", "छाँटें"),
    "common.all": ("All", "सभी"),
    "common.language": ("भाषा / Language", "भाषा / Language"),
    "common.required": ("Required", "आवश्यक"),
    "common.optional": ("Optional", "वैकल्पिक"),
    "common.saved": ("Saved.", "सहेज लिया गया।"),
    "common.error": ("Something went wrong.", "कुछ गड़बड़ी हुई।"),
}


def t(key: str, locale: str = Locale.EN.value, **kwargs: Any) -> str:
    """Translate ``key`` into ``locale``.

    Unknown keys return the key itself rather than raising -- a missing
    translation must never take a financial page down.
    """
    pair = STRINGS.get(key)
    if pair is None:
        return key
    text = pair[1] if locale == Locale.HI.value else pair[0]
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def outcome_label(outcome: str, locale: str = Locale.EN.value) -> str:
    return t(f"outcome.{outcome}", locale)


def health_label(health: str, locale: str = Locale.EN.value) -> str:
    return t(f"health.{health}", locale)


def pick(en: str | None, hi: str | None, locale: str) -> str:
    """Choose between a stored English/Hindi pair, falling back sensibly."""
    if locale == Locale.HI.value:
        return hi or en or ""
    return en or hi or ""


#: Month names for narration parsing and display.
MONTH_NAMES_EN = ["", "January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
MONTH_NAMES_HI = ["", "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
                  "जुलाई", "अगस्त", "सितम्बर", "अक्टूबर", "नवम्बर", "दिसम्बर"]


def month_name(month: int, locale: str = Locale.EN.value) -> str:
    if not 1 <= int(month) <= 12:
        return ""
    return MONTH_NAMES_HI[int(month)] if locale == Locale.HI.value else MONTH_NAMES_EN[int(month)]
