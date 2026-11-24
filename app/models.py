"""The complete relational model for the reconciliation platform.

Design rules encoded here (from the specification):

* s68  -- a period is not "a salary number". Each period carries the whole
         chain: ENTITLEMENT -> SANCTION -> DISBURSEMENT -> CREDIT, plus
         DEDUCTION / ARREAR / ADJUSTMENT / BENEFIT ledgers.
* s69  -- a bank transaction is never counted twice: allocations reference the
         transaction and their sum is constrained to the transaction amount.
* s73  -- dates are never collapsed. Transaction date, value date, salary
         period, effective date, sanction date, document date and credit date
         are separate columns.
* s97  -- immutable public ids; employee NAME is never an identifier.
* s42  -- documents are versioned, never overwritten.
* s51  -- every meaningful change is written to AuditLog.

Money columns are integer paise and always end in ``_paise`` (see app.money).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, new_uid, utcnow
from .enums import (
    AllocationMethod,
    AllocationStatus,
    AnomalyStatus,
    ArrearStatus,
    BenefitStatus,
    DeductionStatus,
    DisputeStatus,
    Direction,
    DocumentCategory,
    EmployeeStatus,
    EvidenceRole,
    GovernmentOrderStatus,
    JobStatus,
    Locale,
    OcrStatus,
    PaymentClassification,
    PaymentLifecycle,
    PeriodStatus,
    ProrationPolicy,
    RecommendationStatus,
    RecordState,
    ReconOutcome,
    Role,
    RuleStatus,
    Severity,
    TransactionStatus,
    VerificationLevel,
)


# ===========================================================================
# Mixins
# ===========================================================================
class TimestampMixin:
    """Every financial record carries creation and modification times (s97)."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UidMixin:
    """Immutable public identifier used in URLs, reports and disputes."""

    uid: Mapped[str] = mapped_column(String(32), default=new_uid, unique=True, index=True, nullable=False)


# ===========================================================================
# SECTION 1 - Organisation, hierarchy and identity
# ===========================================================================
class Organization(Base, UidMixin, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # GOVERNMENT_SCHEME | GOVERNMENT_DEPARTMENT | COMPANY | NGO | OTHER
    org_type: Mapped[str] = mapped_column(String(40), default="GOVERNMENT_SCHEME", nullable=False)
    country: Mapped[str] = mapped_column(String(60), default="India", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    state: Mapped[Optional[str]] = mapped_column(String(80))
    default_locale: Mapped[str] = mapped_column(String(5), default=Locale.EN.value, nullable=False)

    # Salary cycle defaults; overridable per period.
    pay_day_of_month: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    #: Salary for month M is normally credited in month M+``pay_lag_months``.
    pay_lag_months: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    onboarding_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)

    locations: Mapped[list["Location"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    departments: Mapped[list["Department"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    schemes: Mapped[list["Scheme"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    employees: Mapped[list["Employee"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class Location(Base, TimestampMixin):
    """Configurable hierarchy: State > District > Block > Project > Centre."""

    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("organization_id", "level", "name", "parent_id", name="uq_location_scope"),
        Index("ix_location_org_level", "organization_id", "level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(40))

    organization: Mapped["Organization"] = relationship(back_populates="locations")
    parent: Mapped[Optional["Location"]] = relationship(remote_side="Location.id", back_populates="children")
    children: Mapped[list["Location"]] = relationship(back_populates="parent", cascade="all, delete-orphan")

    def ancestor_ids(self) -> list[int]:
        """Ids from this node up to the root, used for scope checks."""
        ids, node = [], self
        seen = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            ids.append(node.id)
            node = node.parent
        return ids

    @property
    def path(self) -> str:
        parts, node, seen = [], self, set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))


class Department(Base, TimestampMixin):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_department_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(40))
    description: Mapped[Optional[str]] = mapped_column(Text)

    organization: Mapped["Organization"] = relationship(back_populates="departments")


class Scheme(Base, TimestampMixin):
    __tablename__ = "schemes"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_scheme_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(40))
    #: e.g. "60:40 Centre:State" -- documentation only, never used for maths.
    funding_pattern: Mapped[Optional[str]] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(Text)

    organization: Mapped["Organization"] = relationship(back_populates="schemes")


class Designation(Base, TimestampMixin):
    __tablename__ = "designations"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_designation_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(40))
    #: Worker category used by salary rules (e.g. AWW, AWH, ASHA).
    category: Mapped[Optional[str]] = mapped_column(String(60))


class User(Base, UidMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_user_email"),
        Index("ix_user_org_role", "organization_id", "role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default=Role.EMPLOYEE.value, nullable=False)
    locale: Mapped[str] = mapped_column(String(5), default=Locale.EN.value, nullable=False)

    #: For EMPLOYEE role users, the employee record they may access.
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    #: For district/block admins, the subtree they are limited to (s49).
    scope_location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    scope_department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    employee: Mapped[Optional["Employee"]] = relationship(foreign_keys=[employee_id])
    scope_location: Mapped[Optional["Location"]] = relationship(foreign_keys=[scope_location_id])


class Employee(Base, UidMixin, TimestampMixin):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("organization_id", "employee_code", name="uq_employee_code"),
        Index("ix_employee_org_status", "organization_id", "status"),
        Index("ix_employee_name_norm", "organization_id", "name_normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    #: Stable human-facing code. NEVER the name (spec s97).
    employee_code: Mapped[str] = mapped_column(String(60), nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Upper-cased, punctuation-stripped, space-collapsed form used for
    #: *candidate* matching only -- never for automatic identity merging (s23).
    name_normalized: Mapped[str] = mapped_column(String(160), default="", nullable=False, index=True)
    father_or_husband_name: Mapped[Optional[str]] = mapped_column(String(160))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    gender: Mapped[Optional[str]] = mapped_column(String(20))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    email: Mapped[Optional[str]] = mapped_column(String(200))

    designation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("designations.id", ondelete="SET NULL"))
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    scheme_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schemes.id", ondelete="SET NULL"))
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))

    joining_date: Mapped[Optional[date]] = mapped_column(Date)
    relieving_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default=EmployeeStatus.ACTIVE.value, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    organization: Mapped["Organization"] = relationship(back_populates="employees")
    designation: Mapped[Optional["Designation"]] = relationship()
    department: Mapped[Optional["Department"]] = relationship()
    scheme: Mapped[Optional["Scheme"]] = relationship()
    location: Mapped[Optional["Location"]] = relationship()
    bank_accounts: Mapped[list["EmployeeBankAccount"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", order_by="EmployeeBankAccount.effective_from"
    )
    assignments: Mapped[list["EmployeeAssignment"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", order_by="EmployeeAssignment.effective_from"
    )

    @property
    def primary_account(self) -> Optional["EmployeeBankAccount"]:
        for acct in self.bank_accounts:
            if acct.is_primary and acct.effective_to is None:
                return acct
        return self.bank_accounts[-1] if self.bank_accounts else None


class EmployeeBankAccount(Base, TimestampMixin):
    """Bank accounts are time-bounded so a change of account does not look
    like duplicate payments (spec s24)."""

    __tablename__ = "employee_bank_accounts"
    __table_args__ = (Index("ix_bank_account_last4", "account_last4"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    bank_name: Mapped[Optional[str]] = mapped_column(String(120))
    branch: Mapped[Optional[str]] = mapped_column(String(120))
    #: Encrypted at rest; the plaintext number is never stored (spec s50, s78).
    account_number_encrypted: Mapped[Optional[str]] = mapped_column(Text)
    #: Last four digits, kept in clear for masked display and matching.
    account_last4: Mapped[Optional[str]] = mapped_column(String(4))
    ifsc: Mapped[Optional[str]] = mapped_column(String(20))
    account_holder_name: Mapped[Optional[str]] = mapped_column(String(160))

    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    employee: Mapped["Employee"] = relationship(back_populates="bank_accounts")

    @property
    def masked(self) -> str:
        return f"XXXXXX{self.account_last4}" if self.account_last4 else "XXXXXXXXXX"


class EmployeeAssignment(Base, TimestampMixin):
    """Posting history. Drives transfer handling (spec s22)."""

    __tablename__ = "employee_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    designation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("designations.id", ondelete="SET NULL"))
    scheme_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schemes.id", ondelete="SET NULL"))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    reason: Mapped[Optional[str]] = mapped_column(String(200))

    employee: Mapped["Employee"] = relationship(back_populates="assignments")
    location: Mapped[Optional["Location"]] = relationship()


class AttendanceRecord(Base, TimestampMixin):
    """Attendance feeds proration and absence-deduction checks (s21, s31)."""

    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("employee_id", "period_id", name="uq_attendance_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    working_days: Mapped[Optional[int]] = mapped_column(Integer)
    present_days: Mapped[Optional[int]] = mapped_column(Integer)
    paid_leave_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unpaid_leave_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    notes: Mapped[Optional[str]] = mapped_column(Text)


# ===========================================================================
# SECTION 2 - Government orders, salary rules and knowledge base
# ===========================================================================
class GovernmentOrder(Base, UidMixin, TimestampMixin):
    """The authority behind a rate (spec s54)."""

    __tablename__ = "government_orders"
    __table_args__ = (Index("ix_go_org_effective", "organization_id", "effective_from"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    order_number: Mapped[Optional[str]] = mapped_column(String(120))
    issuing_authority: Mapped[Optional[str]] = mapped_column(String(200))
    order_date: Mapped[Optional[date]] = mapped_column(Date)
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)

    state: Mapped[Optional[str]] = mapped_column(String(80))
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    scheme_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schemes.id", ondelete="SET NULL"))
    category: Mapped[Optional[str]] = mapped_column(String(80))

    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default=GovernmentOrderStatus.ACTIVE.value, nullable=False)
    superseded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("government_orders.id", ondelete="SET NULL"))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    document: Mapped[Optional["Document"]] = relationship(foreign_keys=[document_id])
    superseded_by: Mapped[Optional["GovernmentOrder"]] = relationship(remote_side="GovernmentOrder.id")
    rules: Mapped[list["SalaryRule"]] = relationship(back_populates="government_order")


class SalaryRule(Base, UidMixin, TimestampMixin):
    """A versioned, scoped, source-backed entitlement rule (spec s4, s79).

    Salary values are NEVER hardcoded in application logic. A rule is data:
    a scope (who it applies to), a validity window, and a component list.
    """

    __tablename__ = "salary_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "rule_code", "version", name="uq_rule_code_version"),
        Index("ix_rule_status_effective", "organization_id", "status", "effective_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default=RuleStatus.DRAFT.value, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)

    # --- Scope. NULL means "applies to any value of this dimension". ---
    scope_state: Mapped[Optional[str]] = mapped_column(String(80))
    scope_location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    scope_department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    scope_scheme_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schemes.id", ondelete="SET NULL"))
    scope_designation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("designations.id", ondelete="SET NULL"))
    scope_category: Mapped[Optional[str]] = mapped_column(String(80))
    #: Additional no-code conditions from the rule builder (spec s79).
    scope_extra_json: Mapped[dict] = mapped_column(JSON, default=dict)

    proration_policy: Mapped[str] = mapped_column(String(40), default=ProrationPolicy.NONE.value, nullable=False)
    #: When a rate changes mid-month, may the month be split? (spec s20)
    allow_mid_month_split: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    government_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("government_orders.id", ondelete="SET NULL"))
    source_note: Mapped[Optional[str]] = mapped_column(Text)

    superseded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    government_order: Mapped[Optional["GovernmentOrder"]] = relationship(back_populates="rules")
    components: Mapped[list["SalaryRuleComponent"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", order_by="SalaryRuleComponent.sort_order"
    )
    superseded_by: Mapped[Optional["SalaryRule"]] = relationship(remote_side="SalaryRule.id")

    @property
    def label(self) -> str:
        return f"{self.rule_code} v{self.version}"

    @property
    def has_source(self) -> bool:
        """A rule with no government order / source note is unverified."""
        return bool(self.government_order_id or self.source_note)


class SalaryRuleComponent(Base, TimestampMixin):
    """One line of a salary rule (base, allowance, incentive, deduction)."""

    __tablename__ = "salary_rule_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("salary_rules.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    label_en: Mapped[str] = mapped_column(String(160), nullable=False)
    label_hi: Mapped[Optional[str]] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    funding_source: Mapped[Optional[str]] = mapped_column(String(20))
    calc_method: Mapped[str] = mapped_column(String(20), default="FIXED", nullable=False)

    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: For PERCENT_OF: percentage value (e.g. 12.0) and the component code or
    #: the token GROSS that it applies to.
    percent_value: Mapped[Optional[float]] = mapped_column(Float)
    percent_of_code: Mapped[Optional[str]] = mapped_column(String(60))
    #: Conditions evaluated by the rule engine (no-code builder output).
    condition_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_prorated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_note: Mapped[Optional[str]] = mapped_column(Text)

    rule: Mapped["SalaryRule"] = relationship(back_populates="components")


class KnowledgeConflict(Base, UidMixin, TimestampMixin):
    """Two documents disagree; a human must resolve it (spec s55)."""

    __tablename__ = "knowledge_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    document_a_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    document_b_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    rule_a_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))
    rule_b_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))
    value_a: Mapped[Optional[str]] = mapped_column(String(200))
    value_b: Mapped[Optional[str]] = mapped_column(String(200))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    resolved_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ===========================================================================
# SECTION 3 - Periods, entitlements, sanctions, disbursements
# ===========================================================================
class SalaryPeriod(Base, TimestampMixin):
    """A salary month. Distinct from the month money arrives (spec s8)."""

    __tablename__ = "salary_periods"
    __table_args__ = (
        UniqueConstraint("organization_id", "year", "month", name="uq_period"),
        Index("ix_period_org_order", "organization_id", "year", "month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: When the credit is normally expected -- basis for delay statistics (s17).
    expected_pay_date: Mapped[Optional[date]] = mapped_column(Date)

    status: Mapped[str] = mapped_column(String(20), default=PeriodStatus.OPEN.value, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    closing_summary_json: Mapped[dict] = mapped_column(JSON, default=dict)

    MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
    MONTH_NAMES_HI = ["", "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
                      "जुलाई", "अगस्त", "सितम्बर", "अक्टूबर", "नवम्बर", "दिसम्बर"]

    @property
    def label(self) -> str:
        return f"{self.MONTH_NAMES[self.month]} {self.year}"

    @property
    def label_hi(self) -> str:
        return f"{self.MONTH_NAMES_HI[self.month]} {self.year}"

    @property
    def short_label(self) -> str:
        return f"{self.MONTH_NAMES[self.month][:3].upper()} {self.year}"

    @property
    def sort_key(self) -> int:
        return self.year * 100 + self.month

    @property
    def is_closed(self) -> bool:
        return self.status == PeriodStatus.CLOSED.value


class Entitlement(Base, UidMixin, TimestampMixin):
    """What the employee should receive for a period.

    Historical entitlements are never edited in place: a recalculation creates
    a new row and marks the old one ``is_current = False`` (spec s19, s56).
    """

    __tablename__ = "entitlements"
    __table_args__ = (
        Index("ix_entitlement_emp_period", "employee_id", "period_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))

    gross_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    deductions_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    net_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Funding split (spec s5)
    central_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    state_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    employer_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    other_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    proration_factor: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    #: Full audit of how the number was reached (spec s91, s93).
    calculation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    basis_note: Mapped[Optional[str]] = mapped_column(Text)

    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supersedes_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entitlements.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(30), default=RecordState.RECONCILED.value, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    employee: Mapped["Employee"] = relationship()
    period: Mapped["SalaryPeriod"] = relationship()
    rule: Mapped[Optional["SalaryRule"]] = relationship()
    components: Mapped[list["EntitlementComponent"]] = relationship(
        back_populates="entitlement", cascade="all, delete-orphan", order_by="EntitlementComponent.sort_order"
    )


class EntitlementComponent(Base):
    """One line of a computed entitlement -- powers the explanation tree (s91)."""

    __tablename__ = "entitlement_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entitlement_id: Mapped[int] = mapped_column(ForeignKey("entitlements.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    label_en: Mapped[str] = mapped_column(String(160), nullable=False)
    label_hi: Mapped[Optional[str]] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    funding_source: Mapped[Optional[str]] = mapped_column(String(20))
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    calculation_note: Mapped[Optional[str]] = mapped_column(Text)
    source_reference: Mapped[Optional[str]] = mapped_column(String(240))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    entitlement: Mapped["Entitlement"] = relationship(back_populates="components")


class PaymentSanction(Base, UidMixin, TimestampMixin):
    """What the authority officially approved (spec s29, leg 2)."""

    __tablename__ = "payment_sanctions"
    __table_args__ = (Index("ix_sanction_emp_period", "employee_id", "period_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))

    sanction_number: Mapped[Optional[str]] = mapped_column(String(120))
    sanction_date: Mapped[Optional[date]] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    authority: Mapped[Optional[str]] = mapped_column(String(200))
    #: SALARY | ARREAR | INCENTIVE ...
    purpose: Mapped[str] = mapped_column(String(40), default=PaymentClassification.SALARY.value, nullable=False)

    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    verification_level: Mapped[int] = mapped_column(Integer, default=VerificationLevel.USER_CLAIM, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(20), default=PaymentLifecycle.PENDING.value, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default=RecordState.IMPORTED.value, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    employee: Mapped["Employee"] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()
    document: Mapped[Optional["Document"]] = relationship()
    disbursements: Mapped[list["Disbursement"]] = relationship(back_populates="sanction", cascade="all, delete-orphan")


class Disbursement(Base, UidMixin, TimestampMixin):
    """Treasury/PFMS release -- the third leg of four-way reconciliation (s30)."""

    __tablename__ = "disbursements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    sanction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payment_sanctions.id", ondelete="SET NULL"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))

    disbursed_on: Mapped[Optional[date]] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), default="BANK_TRANSFER", nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(String(120))
    utr: Mapped[Optional[str]] = mapped_column(String(60), index=True)
    lifecycle: Mapped[str] = mapped_column(String(20), default=PaymentLifecycle.PENDING.value, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(240))

    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    verification_level: Mapped[int] = mapped_column(Integer, default=VerificationLevel.USER_CLAIM, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    sanction: Mapped[Optional["PaymentSanction"]] = relationship(back_populates="disbursements")
    employee: Mapped["Employee"] = relationship()


# ===========================================================================
# SECTION 4 - Bank transactions and allocations
# ===========================================================================
class BankTransaction(Base, UidMixin, TimestampMixin):
    """A normalised bank line. The only proof that money actually arrived."""

    __tablename__ = "bank_transactions"
    __table_args__ = (
        # Duplicate *rows* from re-importing the same statement are rejected by
        # this hash. Genuine duplicate *payments* are a different thing and are
        # flagged as anomalies, never auto-deleted (spec s14).
        UniqueConstraint("organization_id", "row_hash", name="uq_txn_row_hash"),
        Index("ix_txn_emp_date", "employee_id", "txn_date"),
        Index("ix_txn_org_date", "organization_id", "txn_date"),
        Index("ix_txn_utr", "utr"),
        Index("ix_txn_status", "organization_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    bank_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee_bank_accounts.id", ondelete="SET NULL"))
    import_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("import_jobs.id", ondelete="SET NULL"))
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))

    # --- Dates are kept distinct (spec s73) ---
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    value_date: Mapped[Optional[date]] = mapped_column(Date)

    description: Mapped[Optional[str]] = mapped_column(Text)
    narration_raw: Mapped[Optional[str]] = mapped_column(Text)
    narration_normalized: Mapped[Optional[str]] = mapped_column(Text)

    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_paise: Mapped[Optional[int]] = mapped_column(BigInteger)

    utr: Mapped[Optional[str]] = mapped_column(String(60))
    reference_number: Mapped[Optional[str]] = mapped_column(String(120))
    counterparty: Mapped[Optional[str]] = mapped_column(String(200))
    bank_name: Mapped[Optional[str]] = mapped_column(String(120))
    branch: Mapped[Optional[str]] = mapped_column(String(120))
    #: Stable hash of the source row; used only for import de-duplication.
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row_number: Mapped[Optional[int]] = mapped_column(Integer)

    classification: Mapped[str] = mapped_column(String(30), default=PaymentClassification.UNKNOWN.value, nullable=False)
    classification_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Signals behind the classification -- shown to the user, never hidden.
    classification_evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(30), default=TransactionStatus.IMPORTED.value, nullable=False)
    verification_level: Mapped[int] = mapped_column(Integer, default=VerificationLevel.BANK_STATEMENT, nullable=False)

    # --- Reversal linkage (spec s15) ---
    reverses_transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_transactions.id", ondelete="SET NULL"))
    is_reversed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: True for manually recorded cash payments (spec s25).
    is_manual_entry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_cash: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    notes: Mapped[Optional[str]] = mapped_column(Text)

    employee: Mapped[Optional["Employee"]] = relationship()
    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )
    reverses: Mapped[Optional["BankTransaction"]] = relationship(remote_side="BankTransaction.id")

    @property
    def signed_amount_paise(self) -> int:
        return self.amount_paise if self.direction == Direction.CREDIT.value else -self.amount_paise

    @property
    def allocated_paise(self) -> int:
        return sum(a.amount_paise for a in self.allocations if a.status != AllocationStatus.REJECTED.value)

    @property
    def unallocated_paise(self) -> int:
        """Money we have deliberately NOT forced into a month (spec s70)."""
        return self.amount_paise - self.allocated_paise


class PaymentAllocation(Base, UidMixin, TimestampMixin):
    """Links a slice of a bank credit to a salary period.

    The sum of a transaction's non-rejected allocations may never exceed the
    transaction amount -- enforced in the allocation service and asserted by
    the financial integrity tests (spec s69).
    """

    __tablename__ = "payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_allocation_positive"),
        Index("ix_alloc_emp_period", "employee_id", "period_id", "status"),
        Index("ix_alloc_txn", "transaction_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))
    arrear_id: Mapped[Optional[int]] = mapped_column(ForeignKey("arrears.id", ondelete="SET NULL"))

    kind: Mapped[str] = mapped_column(String(30), default=PaymentClassification.SALARY.value, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)

    method: Mapped[str] = mapped_column(String(20), default=AllocationMethod.AUTO.value, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=AllocationStatus.SUGGESTED.value, nullable=False)
    #: Transparent 0-100 score (spec s57).
    match_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Itemised reasons behind the score -- no black-box finance (spec s93).
    match_breakdown_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)

    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    transaction: Mapped["BankTransaction"] = relationship(back_populates="allocations")
    employee: Mapped["Employee"] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()


class MonthCandidate(Base):
    """Probability distribution over candidate salary months (spec s9).

    When narration carries no month we do NOT pick one silently; we store the
    ranked alternatives and surface them for confirmation.
    """

    __tablename__ = "month_candidates"
    __table_args__ = (Index("ix_month_candidate_txn", "transaction_id", "probability"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    probability: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reasons_json: Mapped[list] = mapped_column(JSON, default=list)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_by_human: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    period: Mapped["SalaryPeriod"] = relationship()
    transaction: Mapped["BankTransaction"] = relationship()


# ===========================================================================
# SECTION 5 - Deductions, arrears, adjustments
# ===========================================================================
class Deduction(Base, UidMixin, TimestampMixin):
    __tablename__ = "deductions"
    __table_args__ = (Index("ix_deduction_emp_period", "employee_id", "period_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))
    transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_transactions.id", ondelete="SET NULL"))

    category: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    rule_reference: Mapped[Optional[str]] = mapped_column(String(200))
    authority: Mapped[Optional[str]] = mapped_column(String(200))
    deduction_date: Mapped[Optional[date]] = mapped_column(Date)

    evidence_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    #: EXPECTED (predicted by rule) vs DOCUMENTED vs UNEXPLAINED.
    status: Mapped[str] = mapped_column(String(20), default=DeductionStatus.UNEXPLAINED.value, nullable=False)
    #: RULE | DOCUMENT | RECONCILIATION_GAP | MANUAL
    detected_by: Mapped[str] = mapped_column(String(30), default="RECONCILIATION_GAP", nullable=False)
    verification_level: Mapped[int] = mapped_column(Integer, default=VerificationLevel.USER_CLAIM, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    employee: Mapped["Employee"] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()


class Arrear(Base, UidMixin, TimestampMixin):
    """Outstanding money owed for an earlier period (spec s18)."""

    __tablename__ = "arrears"
    __table_args__ = (Index("ix_arrear_emp_status", "employee_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    origin_period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)

    original_entitlement_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    previously_received_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    outstanding_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    settled_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    status: Mapped[str] = mapped_column(String(30), default=ArrearStatus.OPEN.value, nullable=False)
    #: SHORT_CREDIT | NOT_PAID | RETROACTIVE_RULE | MANUAL
    reason: Mapped[str] = mapped_column(String(40), default="SHORT_CREDIT", nullable=False)
    adjustment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("adjustments.id", ondelete="SET NULL"))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    employee: Mapped["Employee"] = relationship()
    origin_period: Mapped["SalaryPeriod"] = relationship()
    settlements: Mapped[list["ArrearSettlement"]] = relationship(
        back_populates="arrear", cascade="all, delete-orphan"
    )

    @property
    def remaining_paise(self) -> int:
        return max(0, self.outstanding_paise - self.settled_paise)


class ArrearSettlement(Base, TimestampMixin):
    __tablename__ = "arrear_settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    arrear_id: Mapped[int] = mapped_column(ForeignKey("arrears.id", ondelete="CASCADE"), nullable=False)
    allocation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payment_allocations.id", ondelete="SET NULL"))
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    settled_on: Mapped[Optional[date]] = mapped_column(Date)
    note: Mapped[Optional[str]] = mapped_column(Text)

    arrear: Mapped["Arrear"] = relationship(back_populates="settlements")


class Adjustment(Base, UidMixin, TimestampMixin):
    """A correction that preserves history rather than rewriting it (s56)."""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)

    old_value_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    new_value_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    delta_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    reason: Mapped[Optional[str]] = mapped_column(Text)
    old_rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))
    new_rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_rules.id", ondelete="SET NULL"))
    source_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    employee: Mapped["Employee"] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()


# ===========================================================================
# SECTION 6 - Reconciliation results
# ===========================================================================
class Reconciliation(Base, UidMixin, TimestampMixin):
    """The three/four-way comparison for one employee-period (s29, s30)."""

    __tablename__ = "reconciliations"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_id", name="uq_reconciliation_emp_period"),
        Index("ix_recon_outcome", "period_id", "outcome"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    entitlement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("entitlements.id", ondelete="SET NULL"))

    # --- The four legs ---
    entitled_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sanctioned_paise: Mapped[Optional[int]] = mapped_column(BigInteger)     # NULL = no evidence
    disbursed_paise: Mapped[Optional[int]] = mapped_column(BigInteger)      # NULL = no evidence
    credited_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # --- Supporting ledgers ---
    deducted_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    unexplained_deduction_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    arrear_received_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    arrear_outstanding_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    adjustment_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    #: entitled - credited (positive = short, negative = excess)
    difference_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), default=ReconOutcome.INSUFFICIENT_EVIDENCE.value, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default=RecordState.RECONCILED.value, nullable=False)

    # --- Verification, kept per dimension (spec s28) ---
    credit_verification_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sanction_verification_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entitlement_verification_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Delay statistics (spec s17) ---
    expected_pay_date: Mapped[Optional[date]] = mapped_column(Date)
    first_credit_date: Mapped[Optional[date]] = mapped_column(Date)
    delay_days: Mapped[Optional[int]] = mapped_column(Integer)

    explanation_en: Mapped[Optional[str]] = mapped_column(Text)
    explanation_hi: Mapped[Optional[str]] = mapped_column(Text)
    recommended_action_en: Mapped[Optional[str]] = mapped_column(Text)
    recommended_action_hi: Mapped[Optional[str]] = mapped_column(Text)
    #: Structured evidence + possible explanations (spec s33).
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    employee: Mapped["Employee"] = relationship()
    period: Mapped["SalaryPeriod"] = relationship()
    entitlement: Mapped[Optional["Entitlement"]] = relationship()

    @property
    def is_short(self) -> bool:
        return self.difference_paise > 0

    @property
    def is_excess(self) -> bool:
        return self.difference_paise < 0


# ===========================================================================
# SECTION 7 - Benefits ledger (in-kind, never mixed with cash)
# ===========================================================================
class BenefitItem(Base, TimestampMixin):
    __tablename__ = "benefit_items"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_benefit_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_hi: Mapped[Optional[str]] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), default="unit", nullable=False)
    scheme_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schemes.id", ondelete="SET NULL"))
    default_expected_quantity: Mapped[Optional[float]] = mapped_column(Float)
    #: Indicative only. Benefits are NEVER auto-converted into salary (s26).
    estimated_unit_value_paise: Mapped[Optional[int]] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BenefitEntitlement(Base, TimestampMixin):
    __tablename__ = "benefit_entitlements"
    __table_args__ = (UniqueConstraint("employee_id", "period_id", "item_id", name="uq_benefit_entitlement"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("benefit_items.id", ondelete="CASCADE"), nullable=False)
    expected_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_reference: Mapped[Optional[str]] = mapped_column(String(240))

    item: Mapped["BenefitItem"] = relationship()
    period: Mapped["SalaryPeriod"] = relationship()


class BenefitDistribution(Base, UidMixin, TimestampMixin):
    __tablename__ = "benefit_distributions"
    __table_args__ = (Index("ix_benefit_dist_emp_period", "employee_id", "period_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("salary_periods.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("benefit_items.id", ondelete="CASCADE"), nullable=False)

    expected_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    received_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    shortage_quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_value_paise: Mapped[Optional[int]] = mapped_column(BigInteger)

    distribution_date: Mapped[Optional[date]] = mapped_column(Date)
    distribution_location: Mapped[Optional[str]] = mapped_column(String(200))
    supplier: Mapped[Optional[str]] = mapped_column(String(200))
    register_reference: Mapped[Optional[str]] = mapped_column(String(160))

    receipt_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    photo_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    verification_level: Mapped[int] = mapped_column(Integer, default=VerificationLevel.USER_CLAIM, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=BenefitStatus.UNVERIFIED.value, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    item: Mapped["BenefitItem"] = relationship()
    period: Mapped["SalaryPeriod"] = relationship()
    employee: Mapped["Employee"] = relationship()


# ===========================================================================
# SECTION 8 - Documents, evidence, verification
# ===========================================================================
class Document(Base, UidMixin, TimestampMixin):
    """Immutable evidence. Replacing a document creates a NEW version (s42)."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_document_org_category", "organization_id", "category"),
        Index("ix_document_sha", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))

    category: Mapped[str] = mapped_column(String(40), default=DocumentCategory.OTHER.value, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Checksum for tamper evidence and duplicate detection (spec s42).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)

    document_date: Mapped[Optional[date]] = mapped_column(Date)
    issuing_authority: Mapped[Optional[str]] = mapped_column(String(200))
    reference_number: Mapped[Optional[str]] = mapped_column(String(120))

    # --- Versioning: originals are never destroyed ---
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    replacement_reason: Mapped[Optional[str]] = mapped_column(Text)

    # --- Extraction ---
    ocr_status: Mapped[str] = mapped_column(String(20), default=OcrStatus.PENDING.value, nullable=False)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extraction_json: Mapped[dict] = mapped_column(JSON, default=dict)
    extraction_error: Mapped[Optional[str]] = mapped_column(Text)

    uploaded_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    employee: Mapped[Optional["Employee"]] = relationship(foreign_keys=[employee_id])
    supersedes: Mapped[Optional["Document"]] = relationship(remote_side="Document.id")
    evidence_links: Mapped[list["EvidenceLink"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class EvidenceLink(Base, TimestampMixin):
    """Connects a document to any financial record (spec s41)."""

    __tablename__ = "evidence_links"
    __table_args__ = (
        UniqueConstraint("document_id", "entity_type", "entity_id", name="uq_evidence_link"),
        Index("ix_evidence_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=EvidenceRole.SUPPORTING.value, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    linked_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    document: Mapped["Document"] = relationship(back_populates="evidence_links")


class VerificationRecord(Base, UidMixin, TimestampMixin):
    """An assertion that a specific dimension of a record was verified (s28)."""

    __tablename__ = "verification_records"
    __table_args__ = (Index("ix_verification_entity", "entity_type", "entity_id", "dimension"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dimension: Mapped[str] = mapped_column(String(40), nullable=False)

    level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    verified_on: Mapped[Optional[date]] = mapped_column(Date)
    verifier_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[Optional[str]] = mapped_column(Text)

    evidence_document: Mapped[Optional["Document"]] = relationship()


# ===========================================================================
# SECTION 9 - Anomalies, disputes, notifications, audit
# ===========================================================================
class Anomaly(Base, UidMixin, TimestampMixin):
    """Neutral language only. Never asserts fraud (spec s58)."""

    __tablename__ = "anomalies"
    __table_args__ = (
        Index("ix_anomaly_org_status", "organization_id", "status", "severity"),
        UniqueConstraint("organization_id", "code", "entity_type", "entity_id", name="uq_anomaly_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default=Severity.MEDIUM.value, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    entity_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    title_en: Mapped[str] = mapped_column(String(300), nullable=False)
    title_hi: Mapped[Optional[str]] = mapped_column(String(300))
    detail_en: Mapped[Optional[str]] = mapped_column(Text)
    detail_hi: Mapped[Optional[str]] = mapped_column(Text)
    amount_paise: Mapped[Optional[int]] = mapped_column(BigInteger)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default=AnomalyStatus.OPEN.value, nullable=False)
    resolved_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    employee: Mapped[Optional["Employee"]] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()


class Dispute(Base, UidMixin, TimestampMixin):
    __tablename__ = "disputes"
    __table_args__ = (Index("ix_dispute_emp_status", "employee_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salary_periods.id", ondelete="SET NULL"))
    transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bank_transactions.id", ondelete="SET NULL"))
    reconciliation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reconciliations.id", ondelete="SET NULL"))

    issue_type: Mapped[str] = mapped_column(String(60), default="SHORT_PAYMENT", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    expected_paise: Mapped[Optional[int]] = mapped_column(BigInteger)
    received_paise: Mapped[Optional[int]] = mapped_column(BigInteger)
    difference_paise: Mapped[Optional[int]] = mapped_column(BigInteger)

    status: Mapped[str] = mapped_column(String(40), default=DisputeStatus.OPEN.value, nullable=False)
    authority: Mapped[Optional[str]] = mapped_column(String(200))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    response: Mapped[Optional[str]] = mapped_column(Text)
    resolution: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: The generated factual complaint text (spec s46).
    generated_report: Mapped[Optional[str]] = mapped_column(Text)

    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    employee: Mapped["Employee"] = relationship()
    period: Mapped[Optional["SalaryPeriod"]] = relationship()
    events: Mapped[list["DisputeEvent"]] = relationship(
        back_populates="dispute", cascade="all, delete-orphan", order_by="DisputeEvent.created_at"
    )


class DisputeEvent(Base, TimestampMixin):
    __tablename__ = "dispute_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dispute_id: Mapped[int] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    from_status: Mapped[Optional[str]] = mapped_column(String(40))
    to_status: Mapped[Optional[str]] = mapped_column(String(40))

    dispute: Mapped["Dispute"] = relationship(back_populates="events")


class AuditLog(Base):
    """Append-only record of every meaningful change (spec s51).

    There is deliberately no update or delete path for this table anywhere in
    the application.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_org_time", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    #: Kept as text so the log survives user deletion.
    actor_label: Mapped[str] = mapped_column(String(200), default="system", nullable=False)

    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    entity_uid: Mapped[Optional[str]] = mapped_column(String(32))

    old_value_json: Mapped[Optional[dict]] = mapped_column(JSON)
    new_value_json: Mapped[Optional[dict]] = mapped_column(JSON)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    #: HUMAN | AGENT:<name> | SYSTEM | IMPORT
    source: Mapped[str] = mapped_column(String(60), default="SYSTEM", nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(60))
    user_agent: Mapped[Optional[str]] = mapped_column(String(300))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_user_read", "user_id", "is_read"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default=Severity.LOW.value, nullable=False)
    title_en: Mapped[str] = mapped_column(String(300), nullable=False)
    title_hi: Mapped[Optional[str]] = mapped_column(String(300))
    body_en: Mapped[Optional[str]] = mapped_column(Text)
    body_hi: Mapped[Optional[str]] = mapped_column(Text)
    link: Mapped[Optional[str]] = mapped_column(String(300))

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    #: Prevents re-notifying about the same fact (spec s62 - do not spam).
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(200), index=True)


# ===========================================================================
# SECTION 10 - AI jobs, recommendations, memory, imports
# ===========================================================================
class AIJob(Base, UidMixin, TimestampMixin):
    __tablename__ = "ai_jobs"
    __table_args__ = (Index("ix_job_org_status", "organization_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED.value, nullable=False)

    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[Optional[str]] = mapped_column(String(300))
    error: Mapped[Optional[str]] = mapped_column(Text)

    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    @property
    def percent(self) -> int:
        if not self.total:
            return 100 if self.status == JobStatus.COMPLETED.value else 0
        return min(100, int(self.progress * 100 / self.total))


class AIRecommendation(Base, UidMixin, TimestampMixin):
    """A suggestion awaiting human approval (spec s44, s84)."""

    __tablename__ = "ai_recommendations"
    __table_args__ = (Index("ix_recommendation_status", "organization_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    title_en: Mapped[str] = mapped_column(String(300), nullable=False)
    title_hi: Mapped[Optional[str]] = mapped_column(String(300))
    detail: Mapped[Optional[str]] = mapped_column(Text)
    proposed_json: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    agent: Mapped[str] = mapped_column(String(60), default="orchestrator", nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=RecommendationStatus.PENDING.value, nullable=False)
    acted_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    acted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    action_note: Mapped[Optional[str]] = mapped_column(Text)


class CorrectionPattern(Base, TimestampMixin):
    """Learned organisational habits (spec s76).

    Treated as *supporting evidence only*. A learned pattern can never
    override an official document.
    """

    __tablename__ = "correction_patterns"
    __table_args__ = (UniqueConstraint("organization_id", "pattern_key", name="uq_correction_pattern"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    pattern_key: Mapped[str] = mapped_column(String(200), nullable=False)
    pattern_value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AIMemory(Base, TimestampMixin):
    """Explicitly approved organisation facts the agents may rely on (s77)."""

    __tablename__ = "ai_memories"
    __table_args__ = (UniqueConstraint("organization_id", "key", name="uq_ai_memory_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    approved_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ImportJob(Base, UidMixin, TimestampMixin):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))

    #: BANK_STATEMENT_PDF | BANK_STATEMENT_CSV | BANK_STATEMENT_XLSX | EMPLOYEE_LIST | MANUAL
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(300))
    detected_format: Mapped[Optional[str]] = mapped_column(String(120))

    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED.value, nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    errors: Mapped[list["ImportError"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ImportError(Base):
    """Rows are never silently discarded (spec s75)."""

    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False)
    row_number: Mapped[Optional[int]] = mapped_column(Integer)
    page_number: Mapped[Optional[int]] = mapped_column(Integer)
    raw_row: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default=Severity.MEDIUM.value, nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    job: Mapped["ImportJob"] = relationship(back_populates="errors")


class GeneratedReport(Base, UidMixin, TimestampMixin):
    """Record of every export so figures can be traced back (spec s47, s96)."""

    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    report_type: Mapped[str] = mapped_column(String(60), nullable=False)
    period_label: Mapped[Optional[str]] = mapped_column(String(60))
    fmt: Mapped[str] = mapped_column(String(10), default="pdf", nullable=False)
    filters_json: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Totals recorded at generation time; the integrity tests assert these
    #: still equal a fresh query of the database.
    totals_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stored_path: Mapped[Optional[str]] = mapped_column(String(500))
    generated_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ChatMessage(Base):
    """Assistant transcript. Answers always cite database evidence (s34)."""

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(20), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Structured evidence rows backing the answer.
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    intent: Mapped[Optional[str]] = mapped_column(String(60))
    used_llm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
