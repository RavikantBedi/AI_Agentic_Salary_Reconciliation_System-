"""Controlled vocabularies used across the platform.

These are stored as plain strings in the database (portable, greppable, and
stable across migrations) but defined here so that engines and templates agree
on spelling. Every status the specification names has a home in this file.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum that renders as its value in templates and JSON."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def has(cls, value: str) -> bool:
        return value in cls.values()


# --------------------------------------------------------------------------
# Identity & access (spec s49)
# --------------------------------------------------------------------------
class Role(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    DEPARTMENT_ADMIN = "DEPARTMENT_ADMIN"
    DISTRICT_ADMIN = "DISTRICT_ADMIN"
    BLOCK_ADMIN = "BLOCK_ADMIN"
    EMPLOYEE = "EMPLOYEE"
    AUDITOR = "AUDITOR"
    READ_ONLY = "READ_ONLY"


ADMIN_ROLES = {
    Role.SUPER_ADMIN.value,
    Role.DEPARTMENT_ADMIN.value,
    Role.DISTRICT_ADMIN.value,
    Role.BLOCK_ADMIN.value,
}

# Roles that may never mutate financial state.
READ_ONLY_ROLES = {Role.AUDITOR.value, Role.READ_ONLY.value}


class Locale(StrEnum):
    EN = "en"
    HI = "hi"


# --------------------------------------------------------------------------
# Organisation hierarchy (spec s89)
# --------------------------------------------------------------------------
class LocationLevel(StrEnum):
    STATE = "STATE"
    DISTRICT = "DISTRICT"
    BLOCK = "BLOCK"
    PROJECT = "PROJECT"
    CENTRE = "CENTRE"


class EmployeeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ON_LEAVE = "ON_LEAVE"
    TRANSFERRED = "TRANSFERRED"
    RETIRED = "RETIRED"
    TERMINATED = "TERMINATED"
    RESIGNED = "RESIGNED"
    INACTIVE = "INACTIVE"


# --------------------------------------------------------------------------
# Rules & knowledge base (spec s4, s54)
# --------------------------------------------------------------------------
class RuleStatus(StrEnum):
    DRAFT = "DRAFT"
    SIMULATED = "SIMULATED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class ComponentKind(StrEnum):
    BASE = "BASE"
    ALLOWANCE = "ALLOWANCE"
    INCENTIVE = "INCENTIVE"
    REIMBURSEMENT = "REIMBURSEMENT"
    DEDUCTION = "DEDUCTION"


class FundingSource(StrEnum):
    CENTRAL = "CENTRAL"
    STATE = "STATE"
    EMPLOYER = "EMPLOYER"
    OTHER = "OTHER"


class CalcMethod(StrEnum):
    FIXED = "FIXED"                  # flat amount per month
    PERCENT_OF = "PERCENT_OF"        # percentage of another component / gross
    PER_DAY = "PER_DAY"              # rate x attended days
    PER_UNIT = "PER_UNIT"            # rate x arbitrary unit count


class ProrationPolicy(StrEnum):
    """How a rule behaves for a partial month.

    The specification is explicit (s20): do NOT assume every rule prorates.
    The government order decides, so this is a per-rule setting.
    """

    NONE = "NONE"                    # full amount regardless of partial period
    CALENDAR_DAYS = "CALENDAR_DAYS"  # amount x served days / days in month
    ATTENDANCE_DAYS = "ATTENDANCE_DAYS"
    FULL_MONTH_IF_ANY_SERVICE = "FULL_MONTH_IF_ANY_SERVICE"


class GovernmentOrderStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


# --------------------------------------------------------------------------
# Transactions & payments (spec s7, s14-s16, s71)
# --------------------------------------------------------------------------
class Direction(StrEnum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class PaymentClassification(StrEnum):
    SALARY = "SALARY"
    HONORARIUM = "HONORARIUM"
    ARREAR = "ARREAR"
    INCENTIVE = "INCENTIVE"
    ALLOWANCE = "ALLOWANCE"
    REIMBURSEMENT = "REIMBURSEMENT"
    BONUS = "BONUS"
    ADVANCE = "ADVANCE"
    REFUND = "REFUND"
    GOVERNMENT_BENEFIT = "GOVERNMENT_BENEFIT"
    SCHOLARSHIP = "SCHOLARSHIP"
    PENSION = "PENSION"
    REVERSAL = "REVERSAL"
    BANK_CHARGE = "BANK_CHARGE"
    UNKNOWN = "UNKNOWN"
    OTHER = "OTHER"


# Classifications that represent employment income and therefore participate
# in salary-period reconciliation.
INCOME_CLASSIFICATIONS = {
    PaymentClassification.SALARY.value,
    PaymentClassification.HONORARIUM.value,
    PaymentClassification.ARREAR.value,
    PaymentClassification.INCENTIVE.value,
    PaymentClassification.ALLOWANCE.value,
}


class TransactionStatus(StrEnum):
    IMPORTED = "IMPORTED"
    MATCHED = "MATCHED"
    RECONCILED = "RECONCILED"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    UNALLOCATED = "UNALLOCATED"
    REVERSED = "REVERSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IGNORED = "IGNORED"


class PaymentLifecycle(StrEnum):
    """Lifecycle of an outbound/official payment (spec s16)."""

    PENDING = "PENDING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    RETURNED = "RETURNED"
    REJECTED = "REJECTED"
    REVERSED = "REVERSED"


class AllocationStatus(StrEnum):
    SUGGESTED = "SUGGESTED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class AllocationMethod(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    LEARNED = "LEARNED"


class DisbursementChannel(StrEnum):
    PFMS = "PFMS"
    TREASURY = "TREASURY"
    BANK_TRANSFER = "BANK_TRANSFER"
    CASH = "CASH"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


# --------------------------------------------------------------------------
# Reconciliation outcomes (spec s29, s30)
# --------------------------------------------------------------------------
class ReconOutcome(StrEnum):
    FULLY_RECONCILED = "FULLY_RECONCILED"
    SHORT_CREDIT = "SHORT_CREDIT"
    UNEXPLAINED_SHORT_CREDIT = "UNEXPLAINED_SHORT_CREDIT"
    SHORT_SANCTION = "SHORT_SANCTION"
    SANCTIONED_NOT_CREDITED = "SANCTIONED_NOT_CREDITED"
    SANCTIONED_PARTIALLY_CREDITED = "SANCTIONED_PARTIALLY_CREDITED"
    EXCESS_CREDIT = "EXCESS_CREDIT"
    NOT_PAID = "NOT_PAID"
    PAYMENT_REVERSED = "PAYMENT_REVERSED"
    NO_ENTITLEMENT_RULE = "NO_ENTITLEMENT_RULE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


# Outcomes that mean "the employee is owed money".
SHORTFALL_OUTCOMES = {
    ReconOutcome.SHORT_CREDIT.value,
    ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value,
    ReconOutcome.SHORT_SANCTION.value,
    ReconOutcome.SANCTIONED_NOT_CREDITED.value,
    ReconOutcome.SANCTIONED_PARTIALLY_CREDITED.value,
    ReconOutcome.NOT_PAID.value,
}


# --------------------------------------------------------------------------
# Verification hierarchy (spec s28) - the heart of the anti-hallucination model
# --------------------------------------------------------------------------
class VerificationLevel:
    """Strict evidence hierarchy. Higher is stronger.

    Deliberately a plain class of ints rather than an Enum so that levels can
    be compared arithmetically (level >= VerificationLevel.OFFICIAL_DOCUMENT).
    """

    USER_CLAIM = 0             # someone typed it in
    BANK_STATEMENT = 1         # an uploaded bank statement shows the credit
    OFFICIAL_DOCUMENT = 2      # official sanction / payment order document
    GOVERNMENT_PORTAL = 3      # data read from an official portal record
    AUTHORISED_API = 4         # authorised government API / integration
    CROSS_CONFIRMED = 5        # two independent official sources agree

    LABELS = {
        0: "Level 0 - User entered (unverified)",
        1: "Level 1 - Bank statement evidence",
        2: "Level 2 - Official payment/sanction document",
        3: "Level 3 - Official government portal record",
        4: "Level 4 - Authorised API / integration",
        5: "Level 5 - Cross-confirmed independent official sources",
    }

    LABELS_HI = {
        0: "स्तर 0 - उपयोगकर्ता द्वारा दर्ज (अप्रमाणित)",
        1: "स्तर 1 - बैंक स्टेटमेंट प्रमाण",
        2: "स्तर 2 - आधिकारिक भुगतान/स्वीकृति दस्तावेज़",
        3: "स्तर 3 - सरकारी पोर्टल रिकॉर्ड",
        4: "स्तर 4 - अधिकृत API / एकीकरण",
        5: "स्तर 5 - दो स्वतंत्र आधिकारिक स्रोतों से पुष्ट",
    }

    @classmethod
    def label(cls, level: int, locale: str = "en") -> str:
        table = cls.LABELS_HI if locale == "hi" else cls.LABELS
        return table.get(int(level or 0), table[0])


class VerificationDimension(StrEnum):
    """A bank credit and a government sanction prove DIFFERENT things (s28).

    We therefore verify along separate axes and never let one imply the other.
    """

    CREDIT_RECEIVED = "CREDIT_RECEIVED"                # money reached the account
    OFFICIALLY_SANCTIONED = "OFFICIALLY_SANCTIONED"    # authority approved it
    DISBURSED = "DISBURSED"                            # treasury/PFMS released it
    ENTITLEMENT_BASIS = "ENTITLEMENT_BASIS"            # the rule behind the amount


# --------------------------------------------------------------------------
# Deductions (spec s31)
# --------------------------------------------------------------------------
class DeductionCategory(StrEnum):
    PF = "PF"
    ESI = "ESI"
    TDS = "TDS"
    PROFESSIONAL_TAX = "PROFESSIONAL_TAX"
    ADVANCE_RECOVERY = "ADVANCE_RECOVERY"
    OVERPAYMENT_RECOVERY = "OVERPAYMENT_RECOVERY"
    ABSENCE_DEDUCTION = "ABSENCE_DEDUCTION"
    LOAN_RECOVERY = "LOAN_RECOVERY"
    INSURANCE = "INSURANCE"
    BANK_CHARGE = "BANK_CHARGE"
    OTHER_AUTHORISED = "OTHER_AUTHORISED"
    UNKNOWN = "UNKNOWN"


class DeductionStatus(StrEnum):
    EXPECTED = "EXPECTED"          # predicted by the salary rule
    DOCUMENTED = "DOCUMENTED"      # evidenced by an uploaded record
    UNEXPLAINED = "UNEXPLAINED"    # amount is missing with no supporting proof
    DISPUTED = "DISPUTED"
    RESOLVED = "RESOLVED"


# --------------------------------------------------------------------------
# Arrears (spec s18)
# --------------------------------------------------------------------------
class ArrearStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    WRITTEN_OFF = "WRITTEN_OFF"


class AdjustmentKind(StrEnum):
    RETROACTIVE_RULE = "RETROACTIVE_RULE"
    OFFICIAL_CORRECTION = "OFFICIAL_CORRECTION"
    RECOVERY = "RECOVERY"
    MANUAL_CORRECTION = "MANUAL_CORRECTION"


# --------------------------------------------------------------------------
# Benefits ledger (spec s26, s27)
# --------------------------------------------------------------------------
class BenefitCategory(StrEnum):
    FOOD_GRAIN = "FOOD_GRAIN"
    NUTRITION = "NUTRITION"
    UNIFORM = "UNIFORM"
    EQUIPMENT = "EQUIPMENT"
    DEVICE = "DEVICE"
    EDUCATIONAL_MATERIAL = "EDUCATIONAL_MATERIAL"
    MEDICAL_SUPPLY = "MEDICAL_SUPPLY"
    FUEL = "FUEL"
    OTHER = "OTHER"


class BenefitStatus(StrEnum):
    EXPECTED = "EXPECTED"
    RECEIVED_FULL = "RECEIVED_FULL"
    SHORT_DISTRIBUTION = "SHORT_DISTRIBUTION"
    NOT_RECEIVED = "NOT_RECEIVED"
    EXCESS_RECEIVED = "EXCESS_RECEIVED"
    UNVERIFIED = "UNVERIFIED"


# --------------------------------------------------------------------------
# Documents & evidence (spec s41-s43)
# --------------------------------------------------------------------------
class DocumentCategory(StrEnum):
    SALARY_ORDER = "SALARY_ORDER"
    GOVERNMENT_ORDER = "GOVERNMENT_ORDER"
    BANK_STATEMENT = "BANK_STATEMENT"
    PASSBOOK = "PASSBOOK"
    PAYMENT_RECORD = "PAYMENT_RECORD"
    SANCTION_ORDER = "SANCTION_ORDER"
    SALARY_SLIP = "SALARY_SLIP"
    ATTENDANCE = "ATTENDANCE"
    DEDUCTION_RECORD = "DEDUCTION_RECORD"
    BENEFIT_RECORD = "BENEFIT_RECORD"
    RECEIPT = "RECEIPT"
    VOUCHER = "VOUCHER"
    IDENTITY = "IDENTITY"
    CIRCULAR = "CIRCULAR"
    PHOTO_EVIDENCE = "PHOTO_EVIDENCE"
    OTHER = "OTHER"


class OcrStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"   # no OCR engine installed on this host


class EvidenceRole(StrEnum):
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"


# --------------------------------------------------------------------------
# Confidence bands (spec s43)
# --------------------------------------------------------------------------
class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    GOOD = "GOOD"
    REVIEW = "REVIEW"
    MANUAL_VERIFICATION_REQUIRED = "MANUAL_VERIFICATION_REQUIRED"


def confidence_band(confidence: float | None) -> str:
    """Map a 0-1 confidence score onto the specification's bands.

    NOTE: confidence expresses extraction/matching certainty only. It is never
    proof of a financial fact (spec s43).
    """
    if confidence is None:
        return ConfidenceBand.MANUAL_VERIFICATION_REQUIRED.value
    pct = float(confidence) * 100 if confidence <= 1 else float(confidence)
    if pct >= 95:
        return ConfidenceBand.HIGH.value
    if pct >= 80:
        return ConfidenceBand.GOOD.value
    if pct >= 60:
        return ConfidenceBand.REVIEW.value
    return ConfidenceBand.MANUAL_VERIFICATION_REQUIRED.value


# --------------------------------------------------------------------------
# Anomalies (spec s32, s58)
# --------------------------------------------------------------------------
class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AnomalyStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class AnomalyCode(StrEnum):
    UNEXPLAINED_SHORT_CREDIT = "UNEXPLAINED_SHORT_CREDIT"
    UNEXPLAINED_DEDUCTION = "UNEXPLAINED_DEDUCTION"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    MISSING_MONTH = "MISSING_MONTH"
    SUDDEN_AMOUNT_CHANGE = "SUDDEN_AMOUNT_CHANGE"
    REPEATED_SHORT_PAYMENT = "REPEATED_SHORT_PAYMENT"
    UNUSUAL_SENDER = "UNUSUAL_SENDER"
    OUT_OF_CYCLE_PAYMENT = "OUT_OF_CYCLE_PAYMENT"
    SUSPICIOUS_REVERSAL = "SUSPICIOUS_REVERSAL"
    UNEXPECTED_ACCOUNT = "UNEXPECTED_ACCOUNT"
    UNCERTAIN_MONTH_MAPPING = "UNCERTAIN_MONTH_MAPPING"
    POSSIBLE_DUPLICATE_EMPLOYEE = "POSSIBLE_DUPLICATE_EMPLOYEE"
    CONFLICTING_GOVERNMENT_ORDER = "CONFLICTING_GOVERNMENT_ORDER"
    INCORRECT_RATE_APPLIED = "INCORRECT_RATE_APPLIED"
    ARREAR_MISMATCH = "ARREAR_MISMATCH"
    BENEFIT_SHORTAGE = "BENEFIT_SHORTAGE"
    DELAYED_PAYMENT = "DELAYED_PAYMENT"
    UNALLOCATED_CREDIT = "UNALLOCATED_CREDIT"
    EXCESS_CREDIT = "EXCESS_CREDIT"
    OVERDUE_PAYMENT = "OVERDUE_PAYMENT"
    MISSING_ENTITLEMENT_RULE = "MISSING_ENTITLEMENT_RULE"
    LOW_CONFIDENCE_EXTRACTION = "LOW_CONFIDENCE_EXTRACTION"


# --------------------------------------------------------------------------
# Disputes (spec s45)
# --------------------------------------------------------------------------
class DisputeStatus(StrEnum):
    OPEN = "OPEN"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    ADDITIONAL_INFORMATION_REQUIRED = "ADDITIONAL_INFORMATION_REQUIRED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


# --------------------------------------------------------------------------
# Jobs & imports (spec s74, s86)
# --------------------------------------------------------------------------
class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
    STATEMENT_IMPORT = "STATEMENT_IMPORT"
    RECONCILE_PERIOD = "RECONCILE_PERIOD"
    RECONCILE_ORG = "RECONCILE_ORG"
    ANOMALY_SCAN = "ANOMALY_SCAN"
    MONTHLY_CLOSE = "MONTHLY_CLOSE"
    RULE_SIMULATION = "RULE_SIMULATION"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    AGENTIC_MONITOR = "AGENTIC_MONITOR"


class RecommendationStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# --------------------------------------------------------------------------
# Universal record state (spec s83)
# --------------------------------------------------------------------------
class RecordState(StrEnum):
    DRAFT = "DRAFT"
    IMPORTED = "IMPORTED"
    MATCHED = "MATCHED"
    RECONCILED = "RECONCILED"
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DISPUTED = "DISPUTED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    PROCESSING = "PROCESSING"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"


# --------------------------------------------------------------------------
# Evidence provenance labelling for reports (spec s92)
# --------------------------------------------------------------------------
class Provenance(StrEnum):
    FACT = "FACT"                # directly evidenced
    OFFICIAL = "OFFICIAL"        # from an official record
    INFERENCE = "INFERENCE"      # derived by an engine/AI
    USER_INPUT = "USER_INPUT"    # asserted by a person
    UNVERIFIED = "UNVERIFIED"    # no supporting evidence yet


PROVENANCE_LABELS = {
    "FACT": ("FACT", "तथ्य"),
    "OFFICIAL": ("OFFICIAL RECORD", "आधिकारिक रिकॉर्ड"),
    "INFERENCE": ("AI INFERENCE", "AI अनुमान"),
    "USER_INPUT": ("USER INPUT", "उपयोगकर्ता इनपुट"),
    "UNVERIFIED": ("UNVERIFIED", "अप्रमाणित"),
}


# --------------------------------------------------------------------------
# Payment health (spec s38)
# --------------------------------------------------------------------------
class PaymentHealth(StrEnum):
    GOOD = "GOOD"
    ATTENTION = "ATTENTION"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    UNKNOWN = "UNKNOWN"
