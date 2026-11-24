"""Sign in, sign out, language switching and password change."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..config import settings
from ..db import get_db
from ..deps import base_context, get_current_user, get_locale, get_optional_user, verify_csrf
from ..enums import Locale
from ..models import User
from ..security import (
    create_session_token,
    csrf_token,
    hash_password,
    is_locked,
    password_problems,
    register_failed_login,
    register_successful_login,
    verify_password,
)

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


def _set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie, token,
        max_age=settings.session_max_age,
        httponly=True,                 # not readable by JavaScript
        samesite="lax",                # blocks cross-site form posts
        secure=settings.cookie_secure,
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db),
               next: str = "/", error: Optional[str] = None):
    user = get_optional_user(request, db)
    if user is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    context = base_context(request, db, None)
    context.update({
        "next": next,
        "error": error,
        "demo_accounts": _demo_accounts(db),
        # A fresh installation has no accounts at all. Without this the login
        # page is a locked door with no instructions.
        "no_accounts": db.query(User).count() == 0,
    })
    return _templates().TemplateResponse("login.html", context)


def _demo_accounts(db: Session) -> list[dict]:
    """Show demo credentials on the login page only for seeded demo data."""
    from ..models import Organization
    from ..services.seed import DEMO_PASSWORDS

    org = db.query(Organization).filter(Organization.name.like("ICDS%")).first()
    if org is None or not (org.settings_json or {}).get("demo"):
        return []
    labels = {
        "admin@demo.gov.in": "Administrator - full access",
        "sunita@demo.gov.in": "Anganwadi Worker - sees only her own records",
        "auditor@demo.gov.in": "Auditor - read-only, full evidence access",
        "block@demo.gov.in": "Block Supervisor - limited to one block",
    }
    return [{"email": e, "password": p, "label": labels.get(e, "")}
            for e, p in DEMO_PASSWORDS.items()]


@router.post("/login")
def login_submit(request: Request, db: Session = Depends(get_db),
                 email: str = Form(...), password: str = Form(...),
                 next: str = Form("/")):
    email_clean = (email or "").strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()

    # A single generic message for every failure mode, so the form cannot be
    # used to discover which email addresses exist.
    generic = "Email or password is incorrect."

    if user is None:
        return _login_error(request, db, next, generic)

    if is_locked(user):
        return _login_error(
            request, db, next,
            f"This account is temporarily locked after too many failed attempts. "
            f"Please try again in {settings.lockout_minutes} minutes.",
        )

    if not user.is_active:
        return _login_error(request, db, next,
                            "This account is not active. Please contact your administrator.")

    if not verify_password(password, user.password_hash):
        register_failed_login(db, user)
        audit_record(db, action="LOGIN_FAILED", entity_type="User", entity_id=user.id,
                     organization_id=user.organization_id, actor_label=email_clean,
                     reason="Incorrect password", source=AuditSource.HUMAN,
                     ip_address=request.client.host if request.client else None,
                     user_agent=request.headers.get("user-agent"), commit=True)
        return _login_error(request, db, next, generic)

    register_successful_login(db, user)
    token = create_session_token(user.id, user.role)

    audit_record(db, action="LOGIN", entity_type="User", entity_id=user.id,
                 entity_uid=user.uid, organization_id=user.organization_id, actor=user,
                 reason="Signed in", source=AuditSource.HUMAN,
                 ip_address=request.client.host if request.client else None,
                 user_agent=request.headers.get("user-agent"), commit=True)

    target = next if next.startswith("/") else "/"
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, token)
    response.set_cookie("srs_lang", user.locale, max_age=60 * 60 * 24 * 365,
                        samesite="lax", path="/")
    return response


def _login_error(request: Request, db: Session, next: str, message: str):
    context = base_context(request, db, None)
    context.update({"next": next, "error": message, "demo_accounts": _demo_accounts(db)})
    return _templates().TemplateResponse("login.html", context, status_code=401)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db),
           csrf_field: str = Form("", alias="csrf")):
    user = get_optional_user(request, db)
    if user is not None:
        verify_csrf(request, csrf_field)
        audit_record(db, action="LOGOUT", entity_type="User", entity_id=user.id,
                     organization_id=user.organization_id, actor=user,
                     source=AuditSource.HUMAN, commit=True)

    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


@router.get("/logout")
def logout_get():
    """Convenience for a plain link; the POST form is the audited path."""
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


@router.get("/language/{locale}")
def switch_language(locale: str, request: Request):
    """Switch interface language and return to where the user was."""
    if locale not in {Locale.EN.value, Locale.HI.value}:
        locale = Locale.EN.value
    back = request.headers.get("referer") or "/"
    response = RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("srs_lang", locale, max_age=60 * 60 * 24 * 365,
                        samesite="lax", path="/")
    return response


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), saved: Optional[str] = None):
    from ..agents import llm

    context = base_context(request, db, user)
    chosen = llm.provider()
    context.update({
        "saved": saved,
        "llm_status": llm.status_note(context["locale"]),
        "llm_active": llm.available(),
        "llm_provider": chosen,
        "llm_model": llm.model_name(),
        "llm_problem": llm.configuration_problem(),
        "llm_options": [p for p in llm.PROVIDERS.values() if p.free_tier],
    })
    return _templates().TemplateResponse("settings.html", context)


@router.post("/settings/profile")
def update_profile(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   full_name: str = Form(...), locale: str = Form("en"),
                   phone: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    before = {"full_name": user.full_name, "locale": user.locale, "phone": user.phone}

    user.full_name = full_name.strip()[:160] or user.full_name
    user.locale = locale if locale in {Locale.EN.value, Locale.HI.value} else user.locale
    user.phone = phone.strip()[:20] or None

    audit_record(db, action="UPDATE_PROFILE", entity_type="User", entity_id=user.id,
                 organization_id=user.organization_id, actor=user,
                 old_value=before,
                 new_value={"full_name": user.full_name, "locale": user.locale,
                            "phone": user.phone},
                 source=AuditSource.HUMAN)
    db.commit()

    response = RedirectResponse(url="/settings?saved=profile", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("srs_lang", user.locale, max_age=60 * 60 * 24 * 365,
                        samesite="lax", path="/")
    return response


@router.post("/settings/password")
def change_password(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user),
                    current_password: str = Form(...), new_password: str = Form(...),
                    confirm_password: str = Form(...),
                    csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    context = base_context(request, db, user)

    def fail(message: str):
        from ..agents import llm

        context.update({"password_error": message, "llm_status": llm.status_note(context["locale"]),
                        "llm_active": llm.available()})
        return _templates().TemplateResponse("settings.html", context, status_code=400)

    if not verify_password(current_password, user.password_hash):
        return fail("Your current password is not correct.")
    if new_password != confirm_password:
        return fail("The new passwords do not match.")

    problems = password_problems(new_password)
    if problems:
        return fail(" ".join(problems))

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # The hash itself is never logged -- only the fact that it changed.
    audit_record(db, action="CHANGE_PASSWORD", entity_type="User", entity_id=user.id,
                 organization_id=user.organization_id, actor=user,
                 reason="Password changed by the user", source=AuditSource.HUMAN)
    db.commit()

    return RedirectResponse(url="/settings?saved=password", status_code=status.HTTP_303_SEE_OTHER)
