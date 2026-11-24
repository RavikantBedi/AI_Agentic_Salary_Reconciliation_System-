"""Request dependencies: authentication, locale, template context, guards."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .enums import Locale, Role
from .i18n import t
from .models import Employee, Notification, Organization, User
from .security import (
    PermissionDenied,
    csrf_token,
    is_admin,
    is_read_only,
    read_session_token,
)


class NotAuthenticated(Exception):
    """Raised when a page requires a signed-in user and there is none."""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Resolve the signed-in user, or raise :class:`NotAuthenticated`."""
    token = request.cookies.get(settings.session_cookie)
    payload = read_session_token(token or "")
    if not payload:
        raise NotAuthenticated()

    user = db.get(User, payload.get("uid"))
    if user is None or not user.is_active:
        raise NotAuthenticated()

    # The role is re-read from the database on every request, so a role change
    # takes effect immediately rather than living on in an old cookie.
    request.state.user = user
    request.state.csrf = csrf_token(token or "")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    try:
        return get_current_user(request, db)
    except NotAuthenticated:
        return None


def require_admin_user(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user):
        raise PermissionDenied("This page requires an administrator account.")
    return user


def require_writer(user: User = Depends(get_current_user)) -> User:
    if is_read_only(user):
        raise PermissionDenied(
            "This account has read-only access. Auditors can inspect every record "
            "and its evidence, but cannot change financial data."
        )
    return user


def get_locale(request: Request, user: Optional[User] = None) -> str:
    """Locale precedence: explicit query -> cookie -> user profile -> default."""
    requested = request.query_params.get("lang")
    if requested in {Locale.EN.value, Locale.HI.value}:
        return requested
    cookie = request.cookies.get("srs_lang")
    if cookie in {Locale.EN.value, Locale.HI.value}:
        return cookie
    if user is not None and user.locale in {Locale.EN.value, Locale.HI.value}:
        return user.locale
    return settings.default_locale


def current_employee(db: Session, user: User) -> Optional[Employee]:
    if user.employee_id:
        return db.get(Employee, user.employee_id)
    return None


def base_context(request: Request, db: Session, user: Optional[User] = None) -> dict:
    """Context every template needs."""
    locale = get_locale(request, user)
    organization = None
    unread = 0
    employee = None

    if user is not None:
        if user.organization_id:
            organization = db.get(Organization, user.organization_id)
        employee = current_employee(db, user)
        unread = (
            db.query(Notification)
            .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
            .count()
        )

    return {
        "request": request,
        "user": user,
        "employee": employee,
        "organization": organization,
        "locale": locale,
        "t": lambda key, **kw: t(key, locale, **kw),
        "csrf": getattr(request.state, "csrf", ""),
        "unread_notifications": unread,
        "is_admin": is_admin(user) if user else False,
        "is_read_only": is_read_only(user) if user else False,
        "currency": organization.currency if organization else settings.default_currency,
        "settings": settings,
        "Role": Role,
    }


def verify_csrf(request: Request, submitted: Optional[str]) -> None:
    """Validate a CSRF token from a form post."""
    from .security import csrf_valid

    token = request.cookies.get(settings.session_cookie) or ""
    if not csrf_valid(token, submitted or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Your session has expired. Please reload the page and try again.")


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path or "/"
    return RedirectResponse(url=f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)
