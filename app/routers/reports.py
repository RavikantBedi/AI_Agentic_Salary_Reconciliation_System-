"""Report browsing and export."""

from __future__ import annotations

from datetime import date
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..agents import monthly_close_agent
from ..audit import AuditSource, record as audit_record
from ..db import get_db
from ..deps import base_context, get_current_user
from ..enums import PROVENANCE_LABELS
from ..models import Employee, SalaryPeriod, User
from ..security import PermissionDenied, can_access_employee, is_admin, visible_employees
from ..services import reports as report_service

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


def _scoped_ids(db: Session, user: User) -> Optional[list[int]]:
    """Employee ids this user may include in a report."""
    if is_admin(user) or user.role in {"AUDITOR", "READ_ONLY"}:
        return [e.id for e in visible_employees(db, user).all()]
    return [e.id for e in visible_employees(db, user).all()]


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), year: Optional[int] = None):
    context = base_context(request, db, user)
    year = year or date.today().year

    years = sorted({p.year for p in db.query(SalaryPeriod)
                    .filter(SalaryPeriod.organization_id == user.organization_id).all()}) or [year]

    employee = db.get(Employee, user.employee_id) if user.employee_id else None
    annual = None
    if employee is not None:
        annual = monthly_close_agent.annual_summary(db, user.organization_id, year, employee)
    elif is_admin(user):
        annual = monthly_close_agent.annual_summary(db, user.organization_id, year)

    context.update({
        "report_types": report_service.REPORT_TYPES,
        "year": year,
        "years": years,
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "employee": employee,
        "annual": annual,
        "formats": ["pdf", "excel", "csv"],
    })
    return _templates().TemplateResponse("reports.html", context)


def _build(db: Session, user: User, report_type: str, year: int,
           employee: Optional[Employee]) -> report_service.Report:
    ids = _scoped_ids(db, user)

    if report_type == "employee_statement":
        if employee is None:
            raise HTTPException(status_code=400,
                                detail="Choose an employee for a payment statement.")
        return report_service.employee_statement(db, employee, year)

    if report_type == "pending_payments":
        return report_service.pending_payments(db, user.organization_id, ids)
    if report_type == "discrepancies":
        return report_service.discrepancy_report(db, user.organization_id, year, ids)
    if report_type == "verification":
        return report_service.verification_report(db, user.organization_id, ids)
    if report_type == "deductions":
        return report_service.deduction_report(db, user.organization_id, ids)
    if report_type == "benefits":
        return report_service.benefit_report(db, user.organization_id, ids)
    if report_type in {"government_payments", "annual_salary", "monthly_salary",
                       "department_summary"}:
        return report_service.government_payment_report(db, user.organization_id, year, ids)
    if report_type == "arrears":
        return report_service.pending_payments(db, user.organization_id, ids)

    raise HTTPException(status_code=400, detail="Unknown report type")


@router.get("/reports/view/{report_type}", response_class=HTMLResponse)
def view_report(report_type: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user),
                year: Optional[int] = None, employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    year = year or date.today().year

    employee = None
    if employee_uid:
        employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee.")
    elif report_type == "employee_statement" and user.employee_id:
        employee = db.get(Employee, user.employee_id)

    report = _build(db, user, report_type, year, employee)

    context.update({
        "report": report,
        "report_type": report_type,
        "year": year,
        "employee": employee,
        "formats": ["pdf", "excel", "csv"],
        "provenance_labels": {k: v[0] for k, v in PROVENANCE_LABELS.items()},
    })
    return _templates().TemplateResponse("report_view.html", context)


@router.get("/reports/export/{report_type}.{fmt}")
def export_report(report_type: str, fmt: str, request: Request,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user),
                  year: Optional[int] = None, employee_uid: Optional[str] = None):
    year = year or date.today().year

    employee = None
    if employee_uid:
        employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee.")
    elif report_type == "employee_statement" and user.employee_id:
        employee = db.get(Employee, user.employee_id)

    report = _build(db, user, report_type, year, employee)

    try:
        content, mime, extension = report_service.export(report, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    report_service.record_generation(
        db, organization_id=user.organization_id, report=report, report_type=report_type,
        fmt=fmt, user=user, employee_id=employee.id if employee else None,
        filters={"year": year, "employee_uid": employee_uid},
    )
    audit_record(db, action="EXPORT_REPORT", entity_type="Report", entity_id=None,
                 organization_id=user.organization_id, actor=user,
                 new_value={"report_type": report_type, "format": fmt, "year": year,
                            "totals": report.totals},
                 reason="Report exported", source=AuditSource.HUMAN)
    db.commit()

    stem = f"{report_type}_{year}"
    if employee is not None:
        stem += f"_{employee.employee_code}"
    filename = f"{stem}.{extension}".replace(" ", "_")

    return Response(
        content=content, media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'},
    )
