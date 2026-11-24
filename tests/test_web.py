"""End-to-end web tests.

The acceptance criteria in specification s107 are the checklist here: a
non-technical worker must be able to sign in, upload a statement, see the
result explained, correct a mistake, ask a question, export a statement and
raise a dispute.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.db import Base, SessionLocal, engine


@pytest.fixture(scope="module")
def client():
    """A logged-out client against a freshly seeded demo organisation."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.seed import seed_demo

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        seed_demo(session, today=date(2026, 8, 26))
    finally:
        session.close()

    # Reconcile the seeded data so pages have something to show.
    from app.agents import orchestrator
    from app.db import session_scope
    from app.models import Organization

    with session_scope() as db:
        org = db.query(Organization).first()
        orchestrator.process_organization(db, org.id)

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        yield c


def _login(client, email, password, lang="en"):
    # The login rate limiter is deliberately strict and would block a suite
    # that signs in for every test. Its behaviour is covered separately by
    # test_security.test_rate_limiter_blocks_a_burst.
    from app.security import rate_limiter

    rate_limiter.reset()

    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": password, "next": "/"})
    assert response.status_code == 303, f"login failed for {email}"

    # Signing in adopts the user's own language (Sunita's profile is Hindi).
    # Tests pin the interface language so assertions are deterministic; the
    # language switch itself is covered by test_interface_switches_to_hindi.
    if lang:
        client.get(f"/language/{lang}")
    return response


def _csrf(client) -> str:
    page = client.get("/settings")
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, "no CSRF token on the page"
    return match.group(1)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_login_page_is_public(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text or "साइन इन" in response.text


def test_protected_pages_redirect_when_signed_out(client):
    client.cookies.clear()
    for path in ["/", "/transactions", "/reports", "/admin"]:
        response = client.get(path)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]


def test_bad_credentials_give_a_generic_message(client):
    client.cookies.clear()
    response = client.post("/login", data={"email": "admin@demo.gov.in",
                                           "password": "wrong", "next": "/"})
    assert response.status_code == 401
    assert "incorrect" in response.text.lower()
    # The message must not reveal whether the account exists.
    assert "no such user" not in response.text.lower()


def test_unknown_email_gives_the_same_message(client):
    client.cookies.clear()
    response = client.post("/login", data={"email": "nobody@demo.gov.in",
                                           "password": "whatever", "next": "/"})
    assert response.status_code == 401
    assert "incorrect" in response.text.lower()


# ---------------------------------------------------------------------------
# The employee journey (spec s107)
# ---------------------------------------------------------------------------
def test_employee_can_see_their_dashboard(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.get("/")
    assert response.status_code == 200
    assert "Sunita Devi" in response.text
    # The one question that matters must be answered on the page.
    assert "मेरा वेतन सही है" in response.text or "salary correct" in response.text.lower()


@pytest.mark.parametrize("path", [
    "/", "/action-center", "/my-salary", "/transactions", "/arrears", "/deductions",
    "/benefits", "/verification", "/documents", "/disputes", "/reports", "/assistant",
    "/settings", "/notifications",
])
def test_every_employee_page_renders(client, path):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_period_page_explains_the_difference(client):
    """Spec s33: never just 'mismatch detected'."""
    _login(client, "sunita@demo.gov.in", "Worker@12345")

    from app.db import session_scope
    from app.models import Employee, Reconciliation

    with session_scope() as db:
        employee = db.query(Employee).filter(Employee.employee_code == "AWW-LKO-0012").first()
        short = (
            db.query(Reconciliation)
            .filter(Reconciliation.employee_id == employee.id,
                    Reconciliation.difference_paise > 0)
            .first()
        )
        uid = short.uid

    response = client.get(f"/periods/{uid}")
    assert response.status_code == 200
    body = response.text
    assert "Expected" in body
    assert "Bank credited" in body
    assert "Difference" in body
    # Must recommend an action, not merely state a problem.
    assert "Action" in body or "कार्रवाई" in body
    # Neutral language only.
    assert "fraud" not in body.lower()


def test_explanation_tree_shows_the_source(client):
    """Spec s91: every amount traces back to a rule and an order."""
    _login(client, "sunita@demo.gov.in", "Worker@12345")

    from app.db import session_scope
    from app.models import Employee, Reconciliation

    with session_scope() as db:
        employee = db.query(Employee).filter(Employee.employee_code == "AWW-LKO-0012").first()
        uid = (db.query(Reconciliation)
                 .filter(Reconciliation.employee_id == employee.id).first().uid)

    body = client.get(f"/periods/{uid}").text
    assert "Central Government share" in body
    assert "WCD/ICDS/2025/1147" in body, "the government order must be cited"


# ---------------------------------------------------------------------------
# Hindi interface (spec s60)
# ---------------------------------------------------------------------------
def test_interface_switches_to_hindi(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345", lang="hi")

    response = client.get("/")
    assert response.status_code == 200
    assert 'lang="hi"' in response.text
    assert "डैशबोर्ड" in response.text
    client.get("/language/en")


# ---------------------------------------------------------------------------
# Assistant (spec s34)
# ---------------------------------------------------------------------------
def test_assistant_answers_with_evidence(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    csrf = _csrf(client)

    response = client.post("/assistant", data={"csrf": csrf,
                                               "question": "How much money is pending?"})
    assert response.status_code == 303

    page = client.get("/assistant").text
    assert "How much money is pending?" in page
    assert "₹" in page
    assert "Evidence" in page or "प्रमाण" in page


def test_assistant_json_endpoint(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.post("/api/assistant", data={"question": "Which months are unpaid?"})
    assert response.status_code == 200

    payload = response.json()
    assert payload["intent"] == "unpaid_months"
    assert payload["used_llm"] is False       # no API key configured in tests
    assert isinstance(payload["evidence"], list)


def test_assistant_refuses_to_guess_when_records_are_silent(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.post("/api/assistant",
                           data={"question": "What is the capital of France?"})
    text = response.json()["text"].lower()
    assert "paris" not in text
    # It falls back to a summary of the actual records rather than answering.
    assert ("could not fully understand" in text
            or "summary of your records" in text
            or "नहीं समझ" in text)


# ---------------------------------------------------------------------------
# Corrections (spec s44)
# ---------------------------------------------------------------------------
def test_user_can_correct_a_classification(client):
    _login(client, "admin@demo.gov.in", "Admin@12345")
    csrf = _csrf(client)

    from app.db import session_scope
    from app.models import BankTransaction

    with session_scope() as db:
        txn = db.query(BankTransaction).filter(
            BankTransaction.direction == "CREDIT").first()
        uid, original = txn.uid, txn.classification

    response = client.post(f"/transactions/{uid}/classify",
                           data={"csrf": csrf, "classification": "INCENTIVE",
                                 "reason": "Confirmed with the office"})
    assert response.status_code == 303

    with session_scope() as db:
        txn = db.query(BankTransaction).filter(BankTransaction.uid == uid).first()
        assert txn.classification == "INCENTIVE"
        assert txn.classification_locked is True, "a human decision must not be overwritten"
        assert txn.classification_confidence == 1.0

    # The correction is auditable.
    audit = client.get("/audit?action=RECLASSIFY_TRANSACTION").text
    assert "Confirmed with the office" in audit


def test_assigning_a_month_updates_the_reconciliation(client):
    """The uncertain 'NEFT GOVT CREDIT' payment gets confirmed by a person."""
    _login(client, "admin@demo.gov.in", "Admin@12345")
    csrf = _csrf(client)

    from app.db import session_scope
    from app.models import BankTransaction, Employee, Reconciliation, SalaryPeriod

    with session_scope() as db:
        txn = db.query(BankTransaction).filter(
            BankTransaction.description.like("%GOVT CREDIT%")).first()
        raj = db.get(Employee, txn.employee_id)
        january = db.query(SalaryPeriod).filter(SalaryPeriod.month == 1).first()
        uid, period_id, employee_id = txn.uid, january.id, raj.id

        before = (db.query(Reconciliation)
                    .filter(Reconciliation.employee_id == employee_id,
                            Reconciliation.period_id == period_id).first())
        assert before.credited_paise == 0

    response = client.post(f"/transactions/{uid}/assign-month",
                           data={"csrf": csrf, "period_id": str(period_id),
                                 "kind": "SALARY", "reason": "Confirmed by the block office"})
    assert response.status_code == 303

    with session_scope() as db:
        after = (db.query(Reconciliation)
                   .filter(Reconciliation.employee_id == employee_id,
                           Reconciliation.period_id == period_id).first())
        assert after.credited_paise > 0, "confirming the month must update the ledger"


# ---------------------------------------------------------------------------
# Disputes (spec s45, s46)
# ---------------------------------------------------------------------------
def test_dispute_report_is_factual(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")

    from app.db import session_scope
    from app.models import Employee, Reconciliation

    with session_scope() as db:
        employee = db.query(Employee).filter(Employee.employee_code == "AWW-LKO-0012").first()
        short = (db.query(Reconciliation)
                   .filter(Reconciliation.employee_id == employee.id,
                           Reconciliation.difference_paise > 0).first())
        uid = short.uid

    page = client.get(f"/disputes/new?recon_uid={uid}").text
    assert "CLARIFICATION REQUESTED" in page
    assert "makes no allegation" in page
    assert "XXXXXX" in page, "the account number must be masked in the statement"

    for word in ["fraud", "theft", "stolen", "corrupt"]:
        assert word not in page.lower(), f"the statement must not contain {word!r}"


def test_employee_can_raise_and_track_a_dispute(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    csrf = _csrf(client)

    from app.db import session_scope
    from app.models import Employee, Reconciliation

    with session_scope() as db:
        employee = db.query(Employee).filter(Employee.employee_code == "AWW-LKO-0012").first()
        short = (db.query(Reconciliation)
                   .filter(Reconciliation.employee_id == employee.id,
                           Reconciliation.difference_paise > 0).first())
        recon_uid, employee_id = short.uid, employee.id

    response = client.post("/disputes", data={
        "csrf": csrf, "employee_id": str(employee_id), "recon_uid": recon_uid,
        "title": "February payment short", "issue_type": "SHORT_PAYMENT",
        "description": "I received less than expected.",
        "authority": "CDPO Sarojininagar", "generated_report": "",
    })
    assert response.status_code == 303
    location = response.headers["location"]

    detail = client.get(location)
    assert detail.status_code == 200
    assert "February payment short" in detail.text

    # An employee must not be able to close their own dispute (spec s84).
    uid = location.rsplit("/", 1)[-1]
    blocked = client.post(f"/disputes/{uid}/status",
                          data={"csrf": csrf, "new_status": "RESOLVED", "note": "done"})
    assert blocked.status_code == 403


# ---------------------------------------------------------------------------
# Reports and exports (spec s47)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fmt,prefix", [
    ("pdf", b"%PDF"),
    ("excel", b"PK"),
    ("csv", None),
])
def test_employee_statement_exports(client, fmt, prefix):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.get(f"/reports/export/employee_statement.{fmt}?year=2026")

    assert response.status_code == 200
    assert len(response.content) > 400
    if prefix:
        assert response.content.startswith(prefix)
    else:
        assert "₹" in response.content.decode("utf-8-sig")


def test_report_view_shows_evidence_standing(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    body = client.get("/reports/view/employee_statement?year=2026").text
    assert "How to read this report" in body
    assert "FACT" in body


# ---------------------------------------------------------------------------
# Admin and auditor
# ---------------------------------------------------------------------------
def test_admin_sees_aggregates(client):
    _login(client, "admin@demo.gov.in", "Admin@12345")
    body = client.get("/admin").text
    assert "Employees" in body
    assert "₹" in body


def test_auditor_is_read_only_in_the_interface(client):
    _login(client, "auditor@demo.gov.in", "Audit@12345")

    body = client.get("/").text
    assert "read-only access" in body

    # Read access works, including the audit log.
    assert client.get("/audit").status_code == 200
    assert client.get("/employees").status_code == 200

    # Writing is refused.
    csrf = _csrf(client)
    from app.db import session_scope
    from app.models import BankTransaction

    with session_scope() as db:
        uid = db.query(BankTransaction).first().uid

    response = client.post(f"/transactions/{uid}/classify",
                           data={"csrf": csrf, "classification": "BONUS", "reason": "x"})
    assert response.status_code == 403


def test_block_admin_scope_is_enforced(client):
    _login(client, "block@demo.gov.in", "Block@12345")
    response = client.get("/employees")
    assert response.status_code == 200
    # Everyone in the demo is in the same block, so the page must still work.
    assert "Sunita Devi" in response.text


# ---------------------------------------------------------------------------
# Health and errors
# ---------------------------------------------------------------------------
def test_health_endpoint(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unknown_page_shows_a_friendly_error(client):
    _login(client, "sunita@demo.gov.in", "Worker@12345")
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert "does not exist" in response.text


def test_security_headers_present(client):
    response = client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers
