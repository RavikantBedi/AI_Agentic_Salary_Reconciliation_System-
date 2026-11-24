"""Demo data builder.

Creates a realistic Anganwadi (ICDS) organisation whose ledger deliberately
contains the edge cases the specification calls out, so every engine can be
exercised end to end:

    Sunita Devi (AWW)
        Jan  fully reconciled
        Feb  short credit with no authorising record
        Mar  one month paid in two separate credits
        Apr  one credit covering two months
        May  (settled by the April/May credit)
        Jun  not paid
        Jul  credited, then reversed
        Aug  not yet due

    Rekha Kumari (AWH)
        Jun  sanctioned but never credited
        Jul  sanctioned, only partly credited

    Raj Kumar (AWW)
        Jan  credit with no month named (uncertain month mapping)
        Feb  more received than expected
        Mar  sanctioned less than the entitlement

This is demo data, clearly labelled as such. It is created only by the
``seed`` command, never on a normal application start.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..enums import (
    BenefitCategory,
    BenefitStatus,
    ComponentKind,
    Direction,
    DocumentCategory,
    EmployeeStatus,
    FundingSource,
    GovernmentOrderStatus,
    LocationLevel,
    OcrStatus,
    PaymentClassification,
    PaymentLifecycle,
    PeriodStatus,
    ProrationPolicy,
    Role,
    RuleStatus,
    TransactionStatus,
    VerificationLevel,
)
from ..models import (
    BenefitDistribution,
    BenefitItem,
    Department,
    Designation,
    Document,
    Employee,
    EmployeeBankAccount,
    EvidenceLink,
    GovernmentOrder,
    Location,
    Organization,
    PaymentSanction,
    SalaryPeriod,
    SalaryRule,
    SalaryRuleComponent,
    Scheme,
    User,
)
from ..security import encrypt_value, hash_password, last4
from .textutils import add_months, month_end, normalize_name, normalize_narration

DEMO_PASSWORDS = {
    "admin@demo.gov.in": "Admin@12345",
    "sunita@demo.gov.in": "Worker@12345",
    "auditor@demo.gov.in": "Audit@12345",
    "block@demo.gov.in": "Block@12345",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _rupees(amount: float) -> int:
    """Rupees -> paise, for readable seed literals."""
    return int(round(amount * 100))


def _txn_hash(org_id: int, employee_id: int, txn_date: date, direction: str,
              amount_paise: int, description: str) -> str:
    parts = [str(org_id), str(employee_id), txn_date.isoformat(), direction,
             str(amount_paise), normalize_narration(description)[:200], ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _credit(db: Session, org: Organization, employee: Employee, when: date,
            amount: float, description: str, *, utr: Optional[str] = None,
            counterparty: str = "DIRECTORATE OF ICDS UTTAR PRADESH",
            direction: str = Direction.CREDIT.value):
    """Create one bank transaction exactly as an import would."""
    from ..models import BankTransaction

    paise = _rupees(amount)
    account = employee.primary_account
    txn = BankTransaction(
        organization_id=org.id,
        employee_id=employee.id,
        bank_account_id=account.id if account else None,
        txn_date=when,
        value_date=when,
        description=description,
        narration_raw=description,
        narration_normalized=normalize_narration(description),
        direction=direction,
        amount_paise=paise,
        utr=utr,
        counterparty=counterparty,
        bank_name=account.bank_name if account else "State Bank of India",
        row_hash=_txn_hash(org.id, employee.id, when, direction, paise, description),
        classification=PaymentClassification.UNKNOWN.value,
        status=TransactionStatus.IMPORTED.value,
        verification_level=VerificationLevel.BANK_STATEMENT,
    )
    db.add(txn)
    db.flush()
    return txn


def _make_go_pdf(title: str, lines: list[str]) -> bytes:
    """Render a small government-order PDF so the evidence vault holds real files."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    import io as _io

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title)
    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    for line in lines:
        story.append(Paragraph(line, styles["Normal"]))
        story.append(Spacer(1, 6))
    doc.build(story)
    return buf.getvalue()


def _store_document(db: Session, org: Organization, *, title: str, category: str,
                    content: bytes, filename: str, uploaded_by_id: Optional[int],
                    document_date: Optional[date] = None,
                    issuing_authority: Optional[str] = None,
                    reference_number: Optional[str] = None) -> Document:
    """Write a file into the evidence vault and register it."""
    digest = hashlib.sha256(content).hexdigest()
    folder = settings.storage_dir / str(org.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:16]}_{filename}"
    path.write_bytes(content)

    doc = Document(
        organization_id=org.id,
        category=category,
        title=title,
        original_filename=filename,
        stored_path=str(path),
        mime_type="application/pdf",
        size_bytes=len(content),
        sha256=digest,
        page_count=1,
        document_date=document_date,
        issuing_authority=issuing_authority,
        reference_number=reference_number,
        ocr_status=OcrStatus.PENDING.value,
        uploaded_by_id=uploaded_by_id,
        notes="Demo document created by the seed command.",
    )
    db.add(doc)
    db.flush()
    return doc


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def seed_demo(db: Session, today: Optional[date] = None) -> dict:
    """Build the whole demo organisation. Returns a summary dict."""
    today = today or date.today()

    existing = db.query(Organization).filter(Organization.name.like("ICDS%")).first()
    if existing:
        return {"created": False, "organization_id": existing.id,
                "message": "Demo data already exists."}

    # --- Organisation & hierarchy ---
    org = Organization(
        name="ICDS Uttar Pradesh - Lucknow Project",
        org_type="GOVERNMENT_SCHEME",
        country="India",
        currency="INR",
        state="Uttar Pradesh",
        default_locale="en",
        pay_day_of_month=7,
        pay_lag_months=1,
        onboarding_step=11,
        settings_json={"demo": True},
    )
    db.add(org)
    db.flush()

    state = Location(organization_id=org.id, level=LocationLevel.STATE.value,
                     name="Uttar Pradesh", code="UP")
    db.add(state)
    db.flush()
    district = Location(organization_id=org.id, parent_id=state.id,
                        level=LocationLevel.DISTRICT.value, name="Lucknow", code="UP-LKO")
    db.add(district)
    db.flush()
    block = Location(organization_id=org.id, parent_id=district.id,
                     level=LocationLevel.BLOCK.value, name="Sarojininagar", code="UP-LKO-SRN")
    db.add(block)
    db.flush()
    centre_a = Location(organization_id=org.id, parent_id=block.id,
                        level=LocationLevel.CENTRE.value, name="Anganwadi Centre 12", code="AWC-12")
    centre_b = Location(organization_id=org.id, parent_id=block.id,
                        level=LocationLevel.CENTRE.value, name="Anganwadi Centre 27", code="AWC-27")
    db.add_all([centre_a, centre_b])
    db.flush()

    department = Department(organization_id=org.id, name="Women and Child Development",
                            code="WCD", description="Department administering the ICDS scheme.")
    scheme = Scheme(organization_id=org.id, name="Anganwadi Services (ICDS)", code="ICDS",
                    funding_pattern="Centre and State share",
                    description="Integrated Child Development Services.")
    db.add_all([department, scheme])
    db.flush()

    aww = Designation(organization_id=org.id, name="Anganwadi Worker", code="AWW", category="AWW")
    awh = Designation(organization_id=org.id, name="Anganwadi Helper", code="AWH", category="AWH")
    db.add_all([aww, awh])
    db.flush()

    # --- Users ---
    admin = User(
        organization_id=org.id, email="admin@demo.gov.in", full_name="Programme Officer (Demo)",
        password_hash=hash_password(DEMO_PASSWORDS["admin@demo.gov.in"]),
        role=Role.SUPER_ADMIN.value, locale="en", is_active=True,
    )
    auditor = User(
        organization_id=org.id, email="auditor@demo.gov.in", full_name="Internal Auditor (Demo)",
        password_hash=hash_password(DEMO_PASSWORDS["auditor@demo.gov.in"]),
        role=Role.AUDITOR.value, locale="en", is_active=True,
    )
    db.add_all([admin, auditor])
    db.flush()

    # --- Government order + rules ---
    go_pdf = _make_go_pdf(
        "Government of Uttar Pradesh - Department of Women and Child Development",
        [
            "Order Number: WCD/ICDS/2025/1147",
            "Dated: 25 March 2025",
            "Subject: Revision of monthly honorarium for Anganwadi Workers and Helpers.",
            "",
            "With effect from 1 April 2025, the monthly honorarium payable shall be:",
            "Anganwadi Worker: Rs. 10,000 per month "
            "(Central share Rs. 4,500 and State share Rs. 5,500).",
            "Anganwadi Helper: Rs. 5,000 per month "
            "(Central share Rs. 2,250 and State share Rs. 2,750).",
            "",
            "In addition, an Anganwadi Worker shall receive a monthly mobile and "
            "communication allowance of Rs. 500 from the State share.",
            "",
            "This order supersedes all previous orders on the subject.",
            "",
            "(This is a demonstration document generated for testing. It is not a real "
            "government order.)",
        ],
    )
    go_doc = _store_document(
        db, org, title="UP WCD Honorarium Revision Order 2025",
        category=DocumentCategory.GOVERNMENT_ORDER.value, content=go_pdf,
        filename="WCD-ICDS-2025-1147.pdf", uploaded_by_id=admin.id,
        document_date=date(2025, 3, 25),
        issuing_authority="Department of Women and Child Development, Government of Uttar Pradesh",
        reference_number="WCD/ICDS/2025/1147",
    )

    gov_order = GovernmentOrder(
        organization_id=org.id,
        title="Revision of monthly honorarium for Anganwadi Workers and Helpers",
        order_number="WCD/ICDS/2025/1147",
        issuing_authority="Department of Women and Child Development, Government of Uttar Pradesh",
        order_date=date(2025, 3, 25),
        effective_from=date(2025, 4, 1),
        state="Uttar Pradesh",
        department_id=department.id,
        scheme_id=scheme.id,
        document_id=go_doc.id,
        status=GovernmentOrderStatus.ACTIVE.value,
        summary="AWW honorarium Rs. 10,000 plus Rs. 500 allowance; AWH honorarium Rs. 5,000.",
    )
    db.add(gov_order)
    db.flush()
    db.add(EvidenceLink(document_id=go_doc.id, entity_type="GovernmentOrder",
                        entity_id=gov_order.id, role="PRIMARY",
                        note="The order this rule is based on."))

    def make_rule(code: str, name: str, designation: Designation,
                  components: list[tuple]) -> SalaryRule:
        rule = SalaryRule(
            organization_id=org.id, rule_code=code, version=1, name=name,
            description=f"Monthly entitlement for {designation.name} under the ICDS scheme.",
            status=RuleStatus.ACTIVE.value,
            effective_from=date(2025, 4, 1),
            scope_state="Uttar Pradesh",
            scope_department_id=department.id,
            scope_scheme_id=scheme.id,
            scope_designation_id=designation.id,
            scope_category=designation.category,
            proration_policy=ProrationPolicy.CALENDAR_DAYS.value,
            allow_mid_month_split=False,
            government_order_id=gov_order.id,
            source_note="Government Order WCD/ICDS/2025/1147 dated 25 March 2025",
            created_by_id=admin.id,
            approved_by_id=admin.id,
            approved_at=None,
        )
        db.add(rule)
        db.flush()
        for order_idx, (ccode, label_en, label_hi, kind, funding, amount) in enumerate(components):
            db.add(SalaryRuleComponent(
                rule_id=rule.id, code=ccode, label_en=label_en, label_hi=label_hi,
                kind=kind, funding_source=funding, calc_method="FIXED",
                amount_paise=_rupees(amount), is_prorated=True, sort_order=order_idx,
                source_note="Clause 2, Government Order WCD/ICDS/2025/1147",
            ))
        db.flush()
        return rule

    make_rule(
        "UP-AWW-2025-001", "Anganwadi Worker honorarium (Uttar Pradesh)", aww,
        [
            ("CENTRAL_SHARE", "Central Government share", "केंद्र सरकार का अंश",
             ComponentKind.BASE.value, FundingSource.CENTRAL.value, 4500),
            ("STATE_SHARE", "State Government share", "राज्य सरकार का अंश",
             ComponentKind.BASE.value, FundingSource.STATE.value, 5500),
            ("MOBILE_ALLOWANCE", "Mobile and communication allowance", "मोबाइल एवं संचार भत्ता",
             ComponentKind.ALLOWANCE.value, FundingSource.STATE.value, 500),
        ],
    )
    make_rule(
        "UP-AWH-2025-001", "Anganwadi Helper honorarium (Uttar Pradesh)", awh,
        [
            ("CENTRAL_SHARE", "Central Government share", "केंद्र सरकार का अंश",
             ComponentKind.BASE.value, FundingSource.CENTRAL.value, 2250),
            ("STATE_SHARE", "State Government share", "राज्य सरकार का अंश",
             ComponentKind.BASE.value, FundingSource.STATE.value, 2750),
        ],
    )

    # --- Salary periods: January .. current month ---
    periods: dict[tuple[int, int], SalaryPeriod] = {}
    cursor = date(today.year, 1, 1)
    while cursor <= date(today.year, today.month, 1):
        start = date(cursor.year, cursor.month, 1)
        end = month_end(cursor.year, cursor.month)
        pay_anchor = add_months(start, org.pay_lag_months)
        period = SalaryPeriod(
            organization_id=org.id, year=cursor.year, month=cursor.month,
            cycle_start=start, cycle_end=end,
            expected_pay_date=date(pay_anchor.year, pay_anchor.month, org.pay_day_of_month),
            status=PeriodStatus.OPEN.value,
        )
        db.add(period)
        periods[(cursor.year, cursor.month)] = period
        cursor = add_months(start, 1)
    db.flush()

    year = today.year

    # --- Employees ---
    def make_employee(code: str, name: str, designation: Designation, location: Location,
                      joining: date, bank: str, account_no: str,
                      dob: Optional[date] = None) -> Employee:
        employee = Employee(
            organization_id=org.id, employee_code=code, full_name=name,
            name_normalized=normalize_name(name), date_of_birth=dob, gender="Female",
            designation_id=designation.id, department_id=department.id,
            scheme_id=scheme.id, location_id=location.id,
            joining_date=joining, status=EmployeeStatus.ACTIVE.value,
        )
        db.add(employee)
        db.flush()
        db.add(EmployeeBankAccount(
            employee_id=employee.id, bank_name=bank, branch="Lucknow Main",
            account_number_encrypted=encrypt_value(account_no),
            account_last4=last4(account_no), ifsc="SBIN0001234",
            account_holder_name=name, effective_from=joining, is_primary=True,
        ))
        db.flush()
        return employee

    sunita = make_employee("AWW-LKO-0012", "Sunita Devi", aww, centre_a,
                           date(2019, 6, 1), "State Bank of India", "30124567894321",
                           date(1988, 7, 14))
    rekha = make_employee("AWH-LKO-0027", "Rekha Kumari", awh, centre_b,
                          date(2021, 2, 15), "Punjab National Bank", "40987654321098",
                          date(1992, 1, 9))
    raj = make_employee("AWW-LKO-0031", "Raj Kumar", aww, centre_b,
                        date(2020, 8, 10), "State Bank of India", "30124567891234",
                        date(1990, 3, 22))
    raj.gender = "Male"

    worker_user = User(
        organization_id=org.id, email="sunita@demo.gov.in", full_name="Sunita Devi",
        password_hash=hash_password(DEMO_PASSWORDS["sunita@demo.gov.in"]),
        role=Role.EMPLOYEE.value, locale="hi", employee_id=sunita.id, is_active=True,
    )
    block_admin = User(
        organization_id=org.id, email="block@demo.gov.in", full_name="Block Supervisor (Demo)",
        password_hash=hash_password(DEMO_PASSWORDS["block@demo.gov.in"]),
        role=Role.BLOCK_ADMIN.value, locale="en", scope_location_id=block.id, is_active=True,
    )
    db.add_all([worker_user, block_admin])
    db.flush()

    P = lambda m: periods[(year, m)]  # noqa: E731

    # =======================================================================
    # Sunita Devi - the full tour of edge cases
    # =======================================================================
    _credit(db, org, sunita, date(year, 2, 7), 10500,
            "NEFT-SBIN0001234-DIRECTORATE OF ICDS UP-JANUARY HONORARIUM-UTRN202602070011",
            utr="UTRN202602070011")

    _credit(db, org, sunita, date(year, 3, 9), 8500,
            "NEFT-DIRECTORATE OF ICDS UP-FEBRUARY HONORARIUM-UTRN202603090012",
            utr="UTRN202603090012")

    # March paid in two parts (spec s11)
    _credit(db, org, sunita, date(year, 4, 6), 6000,
            "NEFT-DIRECTORATE OF ICDS UP-MARCH HONORARIUM PART-UTRN202604060013",
            utr="UTRN202604060013")
    _credit(db, org, sunita, date(year, 4, 14), 4500,
            "NEFT-DIRECTORATE OF ICDS UP-MARCH HONORARIUM BALANCE-UTRN202604140014",
            utr="UTRN202604140014")

    # One credit covering two months (spec s10)
    _credit(db, org, sunita, date(year, 6, 8), 21000,
            "NEFT-DIRECTORATE OF ICDS UP-HONORARIUM APRIL MAY 2026-UTRN202606080015",
            utr="UTRN202606080015")

    # June: nothing at all -> NOT_PAID

    # July credited then reversed (spec s15)
    _credit(db, org, sunita, date(year, 8, 7), 10500,
            "NEFT-DIRECTORATE OF ICDS UP-JULY HONORARIUM-UTRN202608070016",
            utr="UTRN202608070016")
    _credit(db, org, sunita, date(year, 8, 12), 10500,
            "NEFT RETURN - PAYMENT REVERSED - UTRN202608070016",
            utr="UTRN202608070016", direction=Direction.DEBIT.value)

    # Official sanctions for the months where a record exists (three-way legs)
    for month, amount in [(1, 10500), (2, 10500), (3, 10500)]:
        db.add(PaymentSanction(
            organization_id=org.id, employee_id=sunita.id, period_id=P(month).id,
            sanction_number=f"ICDS/LKO/SAN/{year}/{month:02d}/0012",
            sanction_date=date(year, month, 28) if month < 12 else date(year, 12, 28),
            amount_paise=_rupees(amount),
            authority="Child Development Project Officer, Sarojininagar",
            purpose=PaymentClassification.HONORARIUM.value,
            verification_level=VerificationLevel.OFFICIAL_DOCUMENT,
            lifecycle=PaymentLifecycle.SUCCESSFUL.value,
            notes="Demo sanction record.",
        ))

    # =======================================================================
    # Rekha Kumari - sanction vs credit divergence
    # =======================================================================
    for month, credited in [(1, 5000), (2, 5000), (3, 5000), (4, 5000), (5, 5000)]:
        pay_month = month + 1
        _credit(db, org, rekha, date(year, pay_month, 7), credited,
                f"NEFT-ICDS UP-{P(month).label.upper()} HONORARIUM-UTRR{year}{pay_month:02d}07{month:02d}",
                utr=f"UTRR{year}{pay_month:02d}07{month:02d}",
                counterparty="DIRECTORATE OF ICDS UTTAR PRADESH")

    # June: sanctioned but never credited (spec s29)
    db.add(PaymentSanction(
        organization_id=org.id, employee_id=rekha.id, period_id=P(6).id,
        sanction_number=f"ICDS/LKO/SAN/{year}/06/0027",
        sanction_date=date(year, 6, 28), amount_paise=_rupees(5000),
        authority="Child Development Project Officer, Sarojininagar",
        purpose=PaymentClassification.HONORARIUM.value,
        verification_level=VerificationLevel.OFFICIAL_DOCUMENT,
        lifecycle=PaymentLifecycle.SUCCESSFUL.value,
        notes="Sanctioned; no matching bank credit found.",
    ))

    # July: sanctioned 5000, only 3000 credited
    db.add(PaymentSanction(
        organization_id=org.id, employee_id=rekha.id, period_id=P(7).id,
        sanction_number=f"ICDS/LKO/SAN/{year}/07/0027",
        sanction_date=date(year, 7, 28), amount_paise=_rupees(5000),
        authority="Child Development Project Officer, Sarojininagar",
        purpose=PaymentClassification.HONORARIUM.value,
        verification_level=VerificationLevel.OFFICIAL_DOCUMENT,
        lifecycle=PaymentLifecycle.SUCCESSFUL.value,
    ))
    _credit(db, org, rekha, date(year, 8, 7), 3000,
            f"NEFT-ICDS UP-JULY HONORARIUM-UTRR{year}080727",
            utr=f"UTRR{year}080727")

    # =======================================================================
    # Raj Kumar - ambiguous month, excess credit, short sanction
    # =======================================================================
    # No month named anywhere in the narration (spec s9)
    _credit(db, org, raj, date(year, 2, 7), 10500, "NEFT GOVT CREDIT", utr=None,
            counterparty="GOVT OF UP")

    # More than expected (spec s13)
    _credit(db, org, raj, date(year, 3, 7), 12500,
            "NEFT-ICDS UP-FEBRUARY HONORARIUM-UTRJ2026030701",
            utr="UTRJ2026030701")

    # Sanctioned less than the entitlement, and paid exactly what was sanctioned
    db.add(PaymentSanction(
        organization_id=org.id, employee_id=raj.id, period_id=P(3).id,
        sanction_number=f"ICDS/LKO/SAN/{year}/03/0031",
        sanction_date=date(year, 3, 28), amount_paise=_rupees(8000),
        authority="Child Development Project Officer, Sarojininagar",
        purpose=PaymentClassification.HONORARIUM.value,
        verification_level=VerificationLevel.OFFICIAL_DOCUMENT,
        lifecycle=PaymentLifecycle.SUCCESSFUL.value,
        notes="Sanctioned amount is lower than the rule-based entitlement.",
    ))
    _credit(db, org, raj, date(year, 4, 7), 8000,
            "NEFT-ICDS UP-MARCH HONORARIUM-UTRJ2026040701",
            utr="UTRJ2026040701")

    # =======================================================================
    # Benefits ledger (in-kind, kept entirely separate from cash)
    # =======================================================================
    rice = BenefitItem(organization_id=org.id, name="Rice", name_hi="चावल",
                       category=BenefitCategory.FOOD_GRAIN.value, unit="kg",
                       scheme_id=scheme.id, default_expected_quantity=50,
                       estimated_unit_value_paise=_rupees(32))
    wheat = BenefitItem(organization_id=org.id, name="Wheat", name_hi="गेहूँ",
                        category=BenefitCategory.FOOD_GRAIN.value, unit="kg",
                        scheme_id=scheme.id, default_expected_quantity=40,
                        estimated_unit_value_paise=_rupees(28))
    uniform = BenefitItem(organization_id=org.id, name="Uniform (saree)", name_hi="वर्दी (साड़ी)",
                          category=BenefitCategory.UNIFORM.value, unit="set",
                          scheme_id=scheme.id, default_expected_quantity=1,
                          estimated_unit_value_paise=_rupees(650))
    db.add_all([rice, wheat, uniform])
    db.flush()

    for month, (expected, received) in {5: (50, 50), 6: (50, 40), 7: (50, 50)}.items():
        db.add(BenefitDistribution(
            employee_id=sunita.id, period_id=P(month).id, item_id=rice.id,
            expected_quantity=expected, received_quantity=received,
            shortage_quantity=max(0, expected - received),
            estimated_value_paise=_rupees(received * 32),
            distribution_date=date(year, month, 12),
            distribution_location="Block Store, Sarojininagar",
            register_reference=f"REG/{year}/{month:02d}/12",
            verification_level=VerificationLevel.USER_CLAIM,
            status=(BenefitStatus.RECEIVED_FULL.value if received >= expected
                    else BenefitStatus.SHORT_DISTRIBUTION.value),
            notes=("Short distribution recorded; supporting evidence required."
                   if received < expected else None),
        ))
    db.add(BenefitDistribution(
        employee_id=sunita.id, period_id=P(4).id, item_id=uniform.id,
        expected_quantity=1, received_quantity=1, shortage_quantity=0,
        estimated_value_paise=_rupees(650), distribution_date=date(year, 4, 20),
        distribution_location="Block Store, Sarojininagar",
        verification_level=VerificationLevel.USER_CLAIM,
        status=BenefitStatus.RECEIVED_FULL.value,
    ))
    db.flush()

    db.commit()
    return {
        "created": True,
        "organization_id": org.id,
        "employees": [sunita.employee_code, rekha.employee_code, raj.employee_code],
        "periods": len(periods),
        "users": DEMO_PASSWORDS,
    }
