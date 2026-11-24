"""Shared test fixtures.

Each test module gets a throwaway SQLite database in a temp directory, so the
suite never touches the developer's real data and tests cannot leak state into
one another.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the application at a temporary database BEFORE app.config is imported.
_TMP = tempfile.mkdtemp(prefix="srs-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP).as_posix()}/test.db"
os.environ["STORAGE_DIR"] = str(Path(_TMP) / "documents")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["LLM_ENABLED"] = "false"
os.environ["ENVIRONMENT"] = "test"

from app.db import Base, SessionLocal, engine, session_scope  # noqa: E402
from app.enums import (  # noqa: E402
    ComponentKind,
    FundingSource,
    LocationLevel,
    PeriodStatus,
    ProrationPolicy,
    RuleStatus,
)
from app.models import (  # noqa: E402
    Department,
    Designation,
    Employee,
    EmployeeBankAccount,
    GovernmentOrder,
    Location,
    Organization,
    SalaryPeriod,
    SalaryRule,
    SalaryRuleComponent,
    Scheme,
)
from app.security import encrypt_value, last4  # noqa: E402
from app.services.textutils import month_end, normalize_name  # noqa: E402


def rupees(amount: float) -> int:
    """Rupees to paise, for readable test literals."""
    return int(round(amount * 100))


@pytest.fixture(scope="function")
def db():
    """A fresh, fully-created database for each test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def org(db):
    organization = Organization(
        name="Test ICDS Project", org_type="GOVERNMENT_SCHEME", country="India",
        currency="INR", state="Uttar Pradesh", pay_day_of_month=7, pay_lag_months=1,
    )
    db.add(organization)
    db.flush()
    return organization


@pytest.fixture
def structure(db, org):
    """Locations, department, scheme and designations."""
    state = Location(organization_id=org.id, level=LocationLevel.STATE.value, name="Uttar Pradesh")
    db.add(state)
    db.flush()
    district = Location(organization_id=org.id, parent_id=state.id,
                        level=LocationLevel.DISTRICT.value, name="Lucknow")
    db.add(district)
    db.flush()
    block = Location(organization_id=org.id, parent_id=district.id,
                     level=LocationLevel.BLOCK.value, name="Sarojininagar")
    db.add(block)
    db.flush()

    department = Department(organization_id=org.id, name="WCD")
    scheme = Scheme(organization_id=org.id, name="ICDS")
    db.add_all([department, scheme])
    db.flush()

    aww = Designation(organization_id=org.id, name="Anganwadi Worker", code="AWW", category="AWW")
    awh = Designation(organization_id=org.id, name="Anganwadi Helper", code="AWH", category="AWH")
    db.add_all([aww, awh])
    db.flush()

    return {"state": state, "district": district, "block": block,
            "department": department, "scheme": scheme, "aww": aww, "awh": awh}


@pytest.fixture
def periods(db, org):
    """January to December 2026."""
    made = {}
    for month in range(1, 13):
        start = date(2026, month, 1)
        end = month_end(2026, month)
        pay_month = month + 1 if month < 12 else 1
        pay_year = 2026 if month < 12 else 2027
        period = SalaryPeriod(
            organization_id=org.id, year=2026, month=month,
            cycle_start=start, cycle_end=end,
            expected_pay_date=date(pay_year, pay_month, 7),
            status=PeriodStatus.OPEN.value,
        )
        db.add(period)
        made[month] = period
    db.flush()
    return made


@pytest.fixture
def gov_order(db, org):
    order = GovernmentOrder(
        organization_id=org.id, title="Test honorarium order",
        order_number="TEST/2025/001", issuing_authority="Test Authority",
        order_date=date(2025, 3, 25), effective_from=date(2025, 4, 1),
        state="Uttar Pradesh", status="ACTIVE",
    )
    db.add(order)
    db.flush()
    return order


def make_rule(db, org, designation, code, components, *,
              effective_from=date(2025, 4, 1), effective_to=None,
              proration=ProrationPolicy.CALENDAR_DAYS.value,
              status=RuleStatus.ACTIVE.value, gov_order=None,
              allow_split=False, version=1):
    """Create a salary rule. ``components`` is a list of (code, kind, funding, rupees)."""
    rule = SalaryRule(
        organization_id=org.id, rule_code=code, version=version,
        name=f"{designation.name} rule", status=status,
        effective_from=effective_from, effective_to=effective_to,
        scope_state=org.state, scope_designation_id=designation.id,
        proration_policy=proration, allow_mid_month_split=allow_split,
        government_order_id=gov_order.id if gov_order else None,
        source_note=None if gov_order else "Test source",
    )
    db.add(rule)
    db.flush()
    for order_idx, (ccode, kind, funding, amount) in enumerate(components):
        db.add(SalaryRuleComponent(
            rule_id=rule.id, code=ccode, label_en=ccode.replace("_", " ").title(),
            kind=kind, funding_source=funding, calc_method="FIXED",
            amount_paise=rupees(amount), is_prorated=True, sort_order=order_idx,
        ))
    db.flush()
    return rule


@pytest.fixture
def aww_rule(db, org, structure, gov_order):
    """Anganwadi Worker: 4500 central + 5500 state + 500 allowance = 10500 net."""
    return make_rule(db, org, structure["aww"], "TEST-AWW-001", [
        ("CENTRAL_SHARE", ComponentKind.BASE.value, FundingSource.CENTRAL.value, 4500),
        ("STATE_SHARE", ComponentKind.BASE.value, FundingSource.STATE.value, 5500),
        ("ALLOWANCE", ComponentKind.ALLOWANCE.value, FundingSource.STATE.value, 500),
    ], gov_order=gov_order)


def make_employee(db, org, structure, code="EMP-001", name="Test Worker",
                  designation_key="aww", joining=date(2020, 1, 1),
                  relieving=None, account="30124567894321"):
    employee = Employee(
        organization_id=org.id, employee_code=code, full_name=name,
        name_normalized=normalize_name(name),
        designation_id=structure[designation_key].id,
        department_id=structure["department"].id,
        scheme_id=structure["scheme"].id,
        location_id=structure["block"].id,
        joining_date=joining, relieving_date=relieving, status="ACTIVE",
    )
    db.add(employee)
    db.flush()
    if account:
        db.add(EmployeeBankAccount(
            employee_id=employee.id, bank_name="Test Bank",
            account_number_encrypted=encrypt_value(account),
            account_last4=last4(account), ifsc="TEST0001234",
            account_holder_name=name, effective_from=joining, is_primary=True,
        ))
        db.flush()
    return employee


@pytest.fixture
def employee(db, org, structure, aww_rule):
    return make_employee(db, org, structure)


def add_credit(db, org, employee, when, amount, description,
               utr=None, direction="CREDIT", counterparty="TEST TREASURY"):
    """Insert a bank transaction the way the importer would."""
    import hashlib

    from app.enums import PaymentClassification, TransactionStatus, VerificationLevel
    from app.models import BankTransaction
    from app.services.textutils import normalize_narration

    paise = rupees(amount)
    parts = [str(org.id), str(employee.id), when.isoformat(), direction, str(paise),
             normalize_narration(description)[:200], (utr or "").upper()]
    row_hash = hashlib.sha256("|".join(parts).encode()).hexdigest()

    account = employee.primary_account
    txn = BankTransaction(
        organization_id=org.id, employee_id=employee.id,
        bank_account_id=account.id if account else None,
        txn_date=when, value_date=when, description=description,
        narration_raw=description, narration_normalized=normalize_narration(description),
        direction=direction, amount_paise=paise, utr=utr, counterparty=counterparty,
        bank_name="Test Bank", row_hash=row_hash,
        classification=PaymentClassification.UNKNOWN.value,
        status=TransactionStatus.IMPORTED.value,
        verification_level=VerificationLevel.BANK_STATEMENT,
    )
    db.add(txn)
    db.flush()
    return txn


def add_sanction(db, org, employee, period, amount, number="SAN/001",
                 level=None, when=None):
    from app.enums import PaymentClassification, PaymentLifecycle, VerificationLevel
    from app.models import PaymentSanction

    sanction = PaymentSanction(
        organization_id=org.id, employee_id=employee.id, period_id=period.id,
        sanction_number=number, sanction_date=when or period.cycle_end,
        amount_paise=rupees(amount), authority="Test CDPO",
        purpose=PaymentClassification.HONORARIUM.value,
        verification_level=level if level is not None else VerificationLevel.OFFICIAL_DOCUMENT,
        lifecycle=PaymentLifecycle.SUCCESSFUL.value,
    )
    db.add(sanction)
    db.flush()
    return sanction


def run_pipeline(db, employee):
    """Run the full orchestrator pipeline for one employee."""
    from app.agents import orchestrator

    return orchestrator.process_employee(db, employee)


def recon_for(db, employee, period):
    from app.models import Reconciliation

    return (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id == employee.id,
                Reconciliation.period_id == period.id)
        .first()
    )
