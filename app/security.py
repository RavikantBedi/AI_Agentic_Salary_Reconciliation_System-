"""Authentication, authorisation, field encryption, masking and rate limiting.

Specification coverage: s49 (roles), s50 (security controls), s78 (privacy).

Key decisions
-------------
* Passwords use bcrypt directly. ``passlib`` is not used because its bcrypt
  backend reads ``bcrypt.__about__`` which modern bcrypt releases removed.
* Bank account numbers are encrypted with Fernet using a key derived from
  ``SECRET_KEY``; only the last four digits are stored in clear, for display
  and matching. Full numbers are never rendered in any template (s50).
* Authorisation is *data scoped*: :func:`employee_scope_filter` returns a
  SQLAlchemy condition, so a role restriction cannot be forgotten at the
  template layer -- it is applied in the query itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import timedelta
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .enums import ADMIN_ROLES, READ_ONLY_ROLES, Role
from .models import Employee, Location, User

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
#: bcrypt truncates silently past 72 bytes; we pre-hash so long passphrases
#: keep their full entropy instead of being cut off.
_BCRYPT_MAX = 72


def _prepare_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX:
        return base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prepare_password(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


PASSWORD_RULES_EN = (
    "At least 8 characters, including one letter and one number."
)
PASSWORD_RULES_HI = (
    "कम से कम 8 अक्षर, जिनमें एक अक्षर और एक अंक हो।"
)


def password_problems(password: str) -> list[str]:
    """Return a list of human-readable problems; empty means acceptable."""
    problems: list[str] = []
    if len(password or "") < 8:
        problems.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password or ""):
        problems.append("Password must contain at least one letter.")
    if not re.search(r"\d", password or ""):
        problems.append("Password must contain at least one number.")
    return problems


# ---------------------------------------------------------------------------
# Field-level encryption for account numbers
# ---------------------------------------------------------------------------
def _fernet() -> Fernet:
    raw = settings.encryption_key or settings.secret_key
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_value(plaintext: Optional[str]) -> Optional[str]:
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: Optional[str]) -> Optional[str]:
    """Decrypt, returning ``None`` when the key changed or data is corrupt.

    Never raises: a bank account that cannot be decrypted must degrade to
    "unavailable", not break the page.
    """
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def mask_account(account_number: Optional[str]) -> str:
    """``123456781234`` -> ``XXXXXX1234`` (spec s50)."""
    if not account_number:
        return "XXXXXXXXXX"
    digits = re.sub(r"\D", "", account_number)
    if len(digits) <= 4:
        return "XXXXXX" + digits
    return "XXXXXX" + digits[-4:]


def last4(account_number: Optional[str]) -> Optional[str]:
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else digits or None


def mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return "—"
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "X" * len(digits)
    return "X" * (len(digits) - 4) + digits[-4:]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="srs-session")


def create_session_token(user_id: int, role: str) -> str:
    return _serializer.dumps({"uid": user_id, "role": role, "n": secrets.token_hex(4)})


def read_session_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None


def csrf_token(session_token: str) -> str:
    """Derive a CSRF token from the session so no extra storage is needed."""
    return hmac.new(settings.secret_key.encode(), (session_token or "").encode(),
                    hashlib.sha256).hexdigest()[:32]


def csrf_valid(session_token: str, submitted: str) -> bool:
    return hmac.compare_digest(csrf_token(session_token), submitted or "")


# ---------------------------------------------------------------------------
# Login throttling (spec s50, s95)
# ---------------------------------------------------------------------------
def register_failed_login(db: Session, user: User) -> None:
    user.failed_attempts = (user.failed_attempts or 0) + 1
    if user.failed_attempts >= settings.max_login_attempts:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_attempts = 0
    db.commit()


def register_successful_login(db: Session, user: User) -> None:
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.commit()


def is_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > utcnow())


# ---------------------------------------------------------------------------
# In-process rate limiter
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter.

    Process-local by design: this deployment target is a single uvicorn
    process. Behind multiple workers, put a shared limiter (Redis / reverse
    proxy) in front -- documented in docs/ARCHITECTURE.md.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(bucket) >= max_requests:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            # Opportunistic cleanup so the dict cannot grow without bound.
            if len(self._hits) > 4096:
                for k in [k for k, v in self._hits.items() if not v or max(v) < cutoff]:
                    self._hits.pop(k, None)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
class PermissionDenied(Exception):
    """Raised when a user attempts an action outside their role or scope."""


def is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def is_read_only(user: User) -> bool:
    """Auditors and read-only users may never mutate financial state (s49)."""
    return user.role in READ_ONLY_ROLES


def can_write(user: User) -> bool:
    return not is_read_only(user)


def require_write(user: User) -> None:
    if is_read_only(user):
        raise PermissionDenied(
            "This account has read-only access. Auditors can inspect every "
            "record and its evidence, but cannot change financial data."
        )


def require_admin(user: User) -> None:
    if not is_admin(user):
        raise PermissionDenied("This action requires an administrator account.")


def require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN.value:
        raise PermissionDenied("This action requires a super administrator account.")


def scope_location_ids(db: Session, user: User) -> Optional[list[int]]:
    """Location subtree a scoped admin may see. ``None`` means unrestricted."""
    if user.role in {Role.SUPER_ADMIN.value, Role.DEPARTMENT_ADMIN.value,
                     Role.AUDITOR.value, Role.READ_ONLY.value}:
        return None
    if user.role not in {Role.DISTRICT_ADMIN.value, Role.BLOCK_ADMIN.value}:
        return None
    if not user.scope_location_id:
        return None

    root = db.get(Location, user.scope_location_id)
    if root is None:
        return []
    ids, frontier = [root.id], [root.id]
    while frontier:
        children = db.query(Location.id).filter(Location.parent_id.in_(frontier)).all()
        child_ids = [c[0] for c in children]
        if not child_ids:
            break
        ids.extend(child_ids)
        frontier = child_ids
    return ids


def employee_scope_filter(db: Session, user: User):
    """A SQLAlchemy condition restricting Employee rows to what ``user`` may see.

    Applying authorisation inside the query (rather than filtering after the
    fact) is what prevents cross-employee data leakage (spec s78, s95).
    """
    conditions = [Employee.organization_id == user.organization_id]

    if user.role == Role.EMPLOYEE.value:
        # An employee sees exactly one record: their own. If no employee
        # record is linked, they see none -- never everything.
        conditions.append(Employee.id == (user.employee_id or -1))
        return conditions

    if user.role == Role.DEPARTMENT_ADMIN.value and user.scope_department_id:
        conditions.append(Employee.department_id == user.scope_department_id)

    loc_ids = scope_location_ids(db, user)
    if loc_ids is not None:
        conditions.append(Employee.location_id.in_(loc_ids) if loc_ids else Employee.id == -1)

    return conditions


def can_access_employee(db: Session, user: User, employee: Employee) -> bool:
    if employee is None:
        return False
    if employee.organization_id != user.organization_id:
        return False
    if user.role == Role.EMPLOYEE.value:
        return employee.id == user.employee_id
    if user.role == Role.DEPARTMENT_ADMIN.value and user.scope_department_id:
        if employee.department_id != user.scope_department_id:
            return False
    loc_ids = scope_location_ids(db, user)
    if loc_ids is not None and employee.location_id not in loc_ids:
        return False
    return True


def require_employee_access(db: Session, user: User, employee: Employee) -> None:
    if not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to this employee's records.")


def visible_employees(db: Session, user: User):
    """Query object for employees the user may see."""
    q = db.query(Employee)
    for cond in employee_scope_filter(db, user):
        q = q.filter(cond)
    return q


# ---------------------------------------------------------------------------
# Upload validation (spec s50, s95)
# ---------------------------------------------------------------------------
#: Magic-number prefixes. Extension alone is never trusted.
_MAGIC = {
    b"%PDF": "application/pdf",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"PK\x03\x04": "application/zip",     # xlsx is a zip container
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
    b"RIFF": "image/webp",
}

#: Content that must never be accepted even if the extension looks safe.
_DANGEROUS_SIGNATURES = (
    b"MZ",              # Windows executable
    b"\x7fELF",         # Linux executable
    b"#!",              # script shebang
    b"<?php",
    b"<script",
)


class UploadRejected(Exception):
    """Raised when an uploaded file fails validation."""


def validate_upload(filename: str, content: bytes) -> str:
    """Validate an upload and return the detected MIME type.

    Checks extension allow-list, size, dangerous signatures, and (for binary
    formats) magic-number agreement with the claimed extension.
    """
    if not filename:
        raise UploadRejected("The file has no name.")

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in settings.allowed_upload_extensions:
        allowed = ", ".join(settings.allowed_upload_extensions)
        raise UploadRejected(f"File type '{ext or 'unknown'}' is not accepted. Allowed types: {allowed}")

    if not content:
        raise UploadRejected("The file is empty.")
    if len(content) > settings.max_upload_bytes:
        raise UploadRejected(
            f"The file is {len(content) / 1048576:.1f} MB. The maximum allowed size is "
            f"{settings.max_upload_mb} MB. Please upload a smaller or compressed file."
        )

    head = content[:2048]
    for sig in _DANGEROUS_SIGNATURES:
        if head.lstrip()[: len(sig)] == sig:
            raise UploadRejected("This file appears to contain executable content and was rejected.")

    detected = None
    for magic, mime in _MAGIC.items():
        if content.startswith(magic):
            detected = mime
            break

    # Text formats have no magic number; validate they decode as text.
    if ext in {".csv", ".txt"}:
        try:
            content[:8192].decode("utf-8")
        except UnicodeDecodeError:
            try:
                content[:8192].decode("latin-1")
            except UnicodeDecodeError:
                raise UploadRejected("This file is not readable text.") from None
        return "text/csv" if ext == ".csv" else "text/plain"

    if detected is None:
        raise UploadRejected(
            "The file content does not match a supported document format. "
            "Please upload a PDF, image, CSV or Excel file."
        )

    # Extension/content agreement.
    expected = {
        ".pdf": {"application/pdf"},
        ".png": {"image/png"},
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
        ".webp": {"image/webp"},
        ".tif": {"image/tiff"},
        ".tiff": {"image/tiff"},
        ".xlsx": {"application/zip"},
        ".xls": {"application/zip"},
    }.get(ext)
    if expected and detected not in expected:
        raise UploadRejected(
            f"The file is named '{ext}' but its content is {detected}. "
            "Please upload the file in its correct format."
        )

    if ext in {".xlsx", ".xls"}:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return detected


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_filename(filename: str) -> str:
    """Strip any path component and unsafe characters from an upload name."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip()
    return (cleaned or "document")[:180]
