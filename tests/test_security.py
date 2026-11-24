"""Security tests required by specification s95.

Covers unauthorised employee access, cross-employee leakage, privilege
escalation, malicious uploads, injection, session abuse and data exposure.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.enums import Role
from app.security import (
    PermissionDenied,
    UploadRejected,
    can_access_employee,
    decrypt_value,
    encrypt_value,
    hash_password,
    mask_account,
    rate_limiter,
    require_admin,
    require_write,
    validate_upload,
    verify_password,
    visible_employees,
)
from conftest import add_credit, make_employee, rupees


def _user(db, org, role, employee=None, scope_location=None):
    from app.models import User

    user = User(
        organization_id=org.id,
        email=f"{role.lower()}-{employee.id if employee else 'x'}@test.gov.in",
        full_name=f"{role} user", password_hash=hash_password("Test1234"),
        role=role, employee_id=employee.id if employee else None,
        scope_location_id=scope_location.id if scope_location else None,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def test_password_hashing_is_not_reversible():
    hashed = hash_password("CorrectHorse7")
    assert hashed != "CorrectHorse7"
    assert "CorrectHorse7" not in hashed
    assert verify_password("CorrectHorse7", hashed)
    assert not verify_password("correcthorse7", hashed)
    assert not verify_password("", hashed)


def test_password_minimum_length_enforced():
    with pytest.raises(ValueError):
        hash_password("short")


def test_long_passphrase_keeps_full_entropy():
    """bcrypt truncates at 72 bytes; pre-hashing must prevent a false match."""
    a = "A" * 80 + "first"
    b = "A" * 80 + "second"
    hashed = hash_password(a)
    assert verify_password(a, hashed)
    assert not verify_password(b, hashed), "long passwords must not collide"


# ---------------------------------------------------------------------------
# Field encryption and masking (spec s50, s78)
# ---------------------------------------------------------------------------
def test_account_numbers_encrypted_and_masked(db, org, structure, aww_rule):
    employee = make_employee(db, org, structure, account="30124567894321")
    account = employee.bank_accounts[0]

    assert account.account_number_encrypted != "30124567894321"
    assert "30124567894321" not in account.account_number_encrypted
    assert decrypt_value(account.account_number_encrypted) == "30124567894321"

    assert account.masked == "XXXXXX4321"
    assert mask_account("30124567894321") == "XXXXXX4321"


def test_corrupt_ciphertext_degrades_quietly():
    assert decrypt_value("not-a-valid-token") is None
    assert decrypt_value(None) is None


# ---------------------------------------------------------------------------
# Cross-employee access (spec s78, s95)
# ---------------------------------------------------------------------------
def test_employee_cannot_access_another_employee(db, org, structure, aww_rule):
    alice = make_employee(db, org, structure, code="E-A", name="Alice Kumari")
    bob = make_employee(db, org, structure, code="E-B", name="Bob Kumar")
    alice_user = _user(db, org, Role.EMPLOYEE.value, alice)

    assert can_access_employee(db, alice_user, alice) is True
    assert can_access_employee(db, alice_user, bob) is False

    visible = visible_employees(db, alice_user).all()
    assert visible == [alice]


def test_employee_without_linked_record_sees_nothing(db, org, structure, aww_rule):
    make_employee(db, org, structure, code="E-C", name="Carol Devi")
    orphan_user = _user(db, org, Role.EMPLOYEE.value, None)

    assert visible_employees(db, orphan_user).all() == []


def test_block_admin_scope_is_limited_to_subtree(db, org, structure, aww_rule):
    from app.models import Location

    other_block = Location(organization_id=org.id, parent_id=structure["district"].id,
                           level="BLOCK", name="Other Block")
    db.add(other_block)
    db.flush()

    inside = make_employee(db, org, structure, code="E-IN", name="Inside Worker")
    outside = make_employee(db, org, structure, code="E-OUT", name="Outside Worker")
    outside.location_id = other_block.id
    db.flush()

    supervisor = _user(db, org, Role.BLOCK_ADMIN.value, None,
                       scope_location=structure["block"])

    assert can_access_employee(db, supervisor, inside) is True
    assert can_access_employee(db, supervisor, outside) is False


def test_admin_cannot_reach_another_organization(db, org, structure, aww_rule):
    from app.models import Organization

    other_org = Organization(name="Other Org", currency="INR")
    db.add(other_org)
    db.flush()

    outsider = make_employee(db, org, structure, code="E-X", name="Outsider")
    outsider.organization_id = other_org.id
    db.flush()

    admin = _user(db, org, Role.SUPER_ADMIN.value)
    assert can_access_employee(db, admin, outsider) is False


# ---------------------------------------------------------------------------
# Privilege escalation (spec s95)
# ---------------------------------------------------------------------------
def test_employee_cannot_perform_admin_actions(db, org, structure, aww_rule):
    employee = make_employee(db, org, structure)
    user = _user(db, org, Role.EMPLOYEE.value, employee)

    with pytest.raises(PermissionDenied):
        require_admin(user)


def test_auditor_is_read_only(db, org, structure, aww_rule):
    auditor = _user(db, org, Role.AUDITOR.value)

    with pytest.raises(PermissionDenied):
        require_write(auditor)

    # But an auditor may still read everything.
    make_employee(db, org, structure, code="E-AUD", name="Audited Worker")
    assert len(visible_employees(db, auditor).all()) >= 1


def test_verification_level_cannot_be_forged(db, org, structure, aww_rule):
    """Spec s108: no fake government API verification."""
    from app.agents import verification_agent

    with pytest.raises(verification_agent.VerificationRefused):
        verification_agent.record_verification(
            db, organization_id=org.id, entity_type="Reconciliation", entity_id=1,
            dimension="OFFICIALLY_SANCTIONED", level=4, source="pretend API",
        )


def test_official_verification_requires_a_document(db, org, structure, aww_rule):
    from app.agents import verification_agent

    with pytest.raises(verification_agent.VerificationRefused):
        verification_agent.record_verification(
            db, organization_id=org.id, entity_type="Reconciliation", entity_id=1,
            dimension="OFFICIALLY_SANCTIONED", level=2, source="I say so",
            evidence_document=None,
        )


# ---------------------------------------------------------------------------
# Upload validation (spec s50, s95)
# ---------------------------------------------------------------------------
def test_executable_upload_rejected():
    with pytest.raises(UploadRejected):
        validate_upload("payload.exe", b"MZ\x90\x00")
    with pytest.raises(UploadRejected):
        validate_upload("payload.pdf", b"MZ\x90\x00 disguised")
    with pytest.raises(UploadRejected):
        validate_upload("script.pdf", b"#!/bin/sh\nrm -rf /")


def test_extension_content_mismatch_rejected():
    with pytest.raises(UploadRejected):
        validate_upload("fake.png", b"%PDF-1.4 actually a pdf")


def test_oversized_upload_rejected():
    from app.config import settings

    with pytest.raises(UploadRejected) as exc:
        validate_upload("big.csv", b"x" * (settings.max_upload_bytes + 1))
    assert "maximum allowed size" in str(exc.value)


def test_empty_and_unknown_uploads_rejected():
    with pytest.raises(UploadRejected):
        validate_upload("empty.pdf", b"")
    with pytest.raises(UploadRejected):
        validate_upload("thing.zip", b"PK\x03\x04")


def test_valid_uploads_accepted():
    assert validate_upload("statement.pdf", b"%PDF-1.7 ...") == "application/pdf"
    assert validate_upload("data.csv", b"date,amount\n01/01/2026,100") == "text/csv"
    assert validate_upload("photo.png", b"\x89PNG\r\n\x1a\n rest") == "image/png"


def test_path_traversal_in_filename_neutralised():
    from app.security import safe_filename

    assert "/" not in safe_filename("../../etc/passwd")
    assert "\\" not in safe_filename("..\\..\\windows\\system32\\cfg")
    assert safe_filename("../../etc/passwd") == "passwd"


# ---------------------------------------------------------------------------
# Injection (spec s95)
# ---------------------------------------------------------------------------
def test_sql_injection_in_search_is_harmless(db, org, structure, aww_rule):
    """The ORM parameterises queries; a payload is treated as literal text."""
    from app.models import Employee

    make_employee(db, org, structure, code="E-SQL", name="Normal Worker")
    payload = "'; DROP TABLE employees; --"

    results = (
        db.query(Employee)
        .filter(Employee.organization_id == org.id,
                Employee.full_name.ilike(f"%{payload}%"))
        .all()
    )
    assert results == []
    # The table must still exist and be queryable.
    assert db.query(Employee).count() >= 1


def test_script_payload_is_stored_not_executed(db, org, structure, aww_rule):
    """Jinja autoescaping handles rendering; storage keeps the literal text."""
    employee = make_employee(db, org, structure, code="E-XSS",
                             name="<script>alert(1)</script>")
    db.flush()
    assert "<script>" in employee.full_name  # stored verbatim, escaped at render


# ---------------------------------------------------------------------------
# Rate limiting and sessions (spec s50)
# ---------------------------------------------------------------------------
def test_rate_limiter_blocks_a_burst():
    rate_limiter.reset("test-key")
    allowed = sum(1 for _ in range(10) if rate_limiter.allow("test-key", 5, 60))
    assert allowed == 5
    assert rate_limiter.allow("test-key", 5, 60) is False
    rate_limiter.reset("test-key")
    assert rate_limiter.allow("test-key", 5, 60) is True


def test_session_token_cannot_be_tampered_with():
    from app.security import create_session_token, read_session_token

    token = create_session_token(42, Role.EMPLOYEE.value)
    assert read_session_token(token)["uid"] == 42

    assert read_session_token(token[:-4] + "AAAA") is None
    assert read_session_token("complete.nonsense") is None
    assert read_session_token("") is None


def test_csrf_token_is_bound_to_the_session():
    from app.security import create_session_token, csrf_token, csrf_valid

    session_a = create_session_token(1, Role.EMPLOYEE.value)
    session_b = create_session_token(2, Role.EMPLOYEE.value)

    assert csrf_valid(session_a, csrf_token(session_a))
    assert not csrf_valid(session_a, csrf_token(session_b))
    assert not csrf_valid(session_a, "forged")


def test_account_locks_after_repeated_failures(db, org, structure, aww_rule):
    from app.config import settings
    from app.security import is_locked, register_failed_login

    user = _user(db, org, Role.EMPLOYEE.value)
    for _ in range(settings.max_login_attempts):
        register_failed_login(db, user)

    assert is_locked(user) is True


# ---------------------------------------------------------------------------
# Sensitive data exposure (spec s78)
# ---------------------------------------------------------------------------
def test_audit_log_never_stores_a_password(db, org, structure, aww_rule):
    from app.audit import record
    from app.models import AuditLog

    user = _user(db, org, Role.EMPLOYEE.value)
    record(db, action="CHANGE_PASSWORD", entity_type="User", entity_id=user.id,
           organization_id=org.id, actor=user, reason="Password changed by the user")

    entry = db.query(AuditLog).filter(AuditLog.action == "CHANGE_PASSWORD").first()
    blob = f"{entry.old_value_json} {entry.new_value_json} {entry.reason}"
    assert "Test1234" not in blob
    assert "$2b$" not in blob


def test_dispute_report_masks_the_account_number(db, org, structure, employee,
                                                 periods, aww_rule):
    from app.routers.disputes import prepare_report
    from conftest import recon_for, run_pipeline

    add_credit(db, org, employee, date(2026, 2, 7), 8500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="S1")
    run_pipeline(db, employee)

    report = prepare_report(db, recon_for(db, employee, periods[1]))
    assert "30124567894321" not in report, "a full account number must never be printed"
    assert "XXXXXX4321" in report


# ---------------------------------------------------------------------------
# Production hardening
# ---------------------------------------------------------------------------
def test_api_schema_is_withdrawn_together_with_the_docs_page():
    """DEBUG=false must remove the docs page AND the schema behind it.

    /openapi.json enumerates every route and parameter. Hiding the docs page
    while leaving the schema reachable would be hardening in appearance only.
    """
    from app.config import settings
    from app.main import app

    assert app.docs_url == ("/api/docs" if settings.debug else None)
    assert app.openapi_url == ("/openapi.json" if settings.debug else None)


def test_encryption_key_can_be_pinned_independently_of_secret_key(monkeypatch):
    """Rotating SECRET_KEY must not destroy stored bank account numbers.

    Without a separate ENCRYPTION_KEY the cipher key derives from SECRET_KEY,
    so rotating the signing key would silently make every stored account
    number unreadable. Pinning ENCRYPTION_KEY is what prevents that.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "secret_key", "first-signing-key")
    monkeypatch.setattr(settings, "encryption_key", "pinned-encryption-key")
    token = encrypt_value("30124567894321")
    assert decrypt_value(token) == "30124567894321"

    # Rotate only the signing key: the data must survive.
    monkeypatch.setattr(settings, "secret_key", "second-signing-key")
    assert decrypt_value(token) == "30124567894321"

    # Losing the encryption key degrades quietly rather than crashing, which
    # is precisely why it must be set deliberately and kept.
    monkeypatch.setattr(settings, "encryption_key", "a-different-encryption-key")
    assert decrypt_value(token) is None


def test_secret_key_alone_does_not_protect_data_across_rotation(monkeypatch):
    """Demonstrates the failure mode the README warns about."""
    from app.config import settings

    monkeypatch.setattr(settings, "encryption_key", "")     # not configured
    monkeypatch.setattr(settings, "secret_key", "original-key")
    token = encrypt_value("40987654321098")
    assert decrypt_value(token) == "40987654321098"

    monkeypatch.setattr(settings, "secret_key", "rotated-key")
    assert decrypt_value(token) is None, (
        "rotating SECRET_KEY without ENCRYPTION_KEY loses the data -- "
        "this is the documented warning, asserted so it cannot regress silently"
    )
