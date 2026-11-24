"""Dashboard, action centre, salary timeline, period detail and ledger pages."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import (
    document_agent,
    document_request_agent,
    monthly_close_agent,
    verification_agent,
)
from ..audit import AuditSource, record as audit_record
from ..db import get_db
from ..deps import base_context, get_current_user, verify_csrf
from ..enums import (
    AllocationStatus,
    AnomalyStatus,
    ArrearStatus,
    DeductionStatus,
    Direction,
    PaymentHealth,
    ReconOutcome,
    Severity,
    VerificationDimension,
    VerificationLevel,
)
from ..models import (
    Anomaly,
    Arrear,
    BankTransaction,
    BenefitDistribution,
    Deduction,
    Dispute,
    Employee,
    Entitlement,
    Notification,
    PaymentAllocation,
    PaymentSanction,
    Reconciliation,
    SalaryPeriod,
    User,
)
from ..security import can_access_employee, is_admin, visible_employees

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


# ---------------------------------------------------------------------------
# Employee resolution
# ---------------------------------------------------------------------------
def resolve_employee(db: Session, user: User, employee_uid: Optional[str] = None) -> Optional[Employee]:
    """Which employee's records is this page about?

    An EMPLOYEE user is always locked to their own record; passing another
    employee's id simply has no effect, because access is checked rather than
    trusted.
    """
    if employee_uid:
        employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        if not can_access_employee(db, user, employee):
            from ..security import PermissionDenied

            raise PermissionDenied("You do not have access to this employee's records.")
        return employee

    if user.employee_id:
        return db.get(Employee, user.employee_id)
    return None


def _employee_totals(db: Session, employee: Employee) -> dict:
    recons = (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id == employee.id)
        .all()
    )
    due = [r for r in recons if r.outcome != ReconOutcome.AWAITING_CONFIRMATION.value]

    entitled = sum(r.entitled_paise for r in due)
    received = sum(r.credited_paise for r in due)
    pending = sum(max(0, r.difference_paise) for r in due)

    deductions = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id)
        .all()
    )
    deducted = sum(d.amount_paise for d in deductions
                   if d.status in {DeductionStatus.DOCUMENTED.value, DeductionStatus.EXPECTED.value})
    unexplained = sum(d.amount_paise for d in deductions
                      if d.status == DeductionStatus.UNEXPLAINED.value)

    arrears = (
        db.query(Arrear)
        .filter(Arrear.employee_id == employee.id,
                Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value]))
        .all()
    )
    unverified = (
        db.query(BankTransaction)
        .filter(BankTransaction.employee_id == employee.id,
                BankTransaction.direction == Direction.CREDIT.value,
                BankTransaction.verification_level < VerificationLevel.OFFICIAL_DOCUMENT)
        .count()
    )

    return {
        "entitled_paise": entitled,
        "received_paise": received,
        "pending_paise": pending,
        "deducted_paise": deducted,
        "unexplained_paise": unexplained,
        "arrears_paise": sum(a.remaining_paise for a in arrears),
        "unverified_count": unverified,
        "health": monthly_close_agent.payment_health(db, employee),
        "months_total": len(due),
        "months_reconciled": sum(1 for r in due
                                 if r.outcome == ReconOutcome.FULLY_RECONCILED.value),
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)

    if employee is None and is_admin(user):
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

    if employee is None:
        context.update({
            "title": "No employee record linked",
            "message": ("Your account is not linked to an employee record yet, so there are "
                        "no payment records to show. Please ask your administrator to link "
                        "your account."),
            "code": 200,
        })
        return _templates().TemplateResponse("error.html", context)

    totals = _employee_totals(db, employee)
    recons = sorted(
        db.query(Reconciliation).filter(Reconciliation.employee_id == employee.id).all(),
        key=lambda r: r.period.sort_key if r.period else 0,
    )
    monitor = monthly_close_agent.monitor(db, employee.organization_id, employee)

    context.update({
        "employee": employee,
        "totals": totals,
        "recons": recons,
        "monitor": monitor,
        "health": totals["health"],
        "PaymentHealth": PaymentHealth,
        "chart_data": _chart_data(recons),
    })
    return _templates().TemplateResponse("dashboard.html", context)


def _chart_data(recons: list[Reconciliation]) -> dict:
    """Series for the dashboard charts, with a tabular equivalent (spec s87)."""
    return {
        "labels": [r.period.short_label for r in recons],
        "expected": [r.entitled_paise / 100 for r in recons],
        "received": [r.credited_paise / 100 for r in recons],
        "difference": [r.difference_paise / 100 for r in recons],
        "delay": [r.delay_days or 0 for r in recons],
    }


@router.get("/action-center", response_class=HTMLResponse)
def action_center(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    """Spec s64: the single 'what needs my attention' page."""
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)

    scope_employee = employee if not is_admin(user) or employee_uid else None
    monitor = monthly_close_agent.monitor(db, user.organization_id, scope_employee)

    requests_list = []
    if employee is not None:
        requests_list = [r.as_dict(context["locale"])
                         for r in document_request_agent.for_employee(db, employee)]

    context.update({
        "employee": employee,
        "monitor": monitor,
        "document_requests": requests_list,
    })
    return _templates().TemplateResponse("action_center.html", context)


# ---------------------------------------------------------------------------
# Salary timeline and period detail
# ---------------------------------------------------------------------------
@router.get("/my-salary", response_class=HTMLResponse)
def my_salary(request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user),
              employee_uid: Optional[str] = None, year: Optional[int] = None):
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)
    if employee is None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    year = year or date.today().year
    recons = sorted(
        [r for r in db.query(Reconciliation)
            .filter(Reconciliation.employee_id == employee.id).all()
         if r.period and r.period.year == year],
        key=lambda r: r.period.sort_key,
    )
    years = sorted({p.year for p in db.query(SalaryPeriod)
                    .filter(SalaryPeriod.organization_id == employee.organization_id).all()})

    context.update({
        "employee": employee,
        "recons": recons,
        "year": year,
        "years": years,
        "annual": monthly_close_agent.annual_summary(db, employee.organization_id, year, employee),
        "totals": _employee_totals(db, employee),
    })
    return _templates().TemplateResponse("my_salary.html", context)


@router.get("/periods/{uid}", response_class=HTMLResponse)
def period_detail(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Spec s91: the reconciliation explanation tree for one month."""
    context = base_context(request, db, user)
    recon = db.query(Reconciliation).filter(Reconciliation.uid == uid).first()
    if recon is None:
        raise HTTPException(status_code=404, detail="Period not found")

    employee = db.get(Employee, recon.employee_id)
    if not can_access_employee(db, user, employee):
        from ..security import PermissionDenied

        raise PermissionDenied("You do not have access to this employee's records.")

    entitlement = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id,
                Entitlement.period_id == recon.period_id,
                Entitlement.is_current.is_(True))
        .first()
    )
    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == recon.period_id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    sanctions = (
        db.query(PaymentSanction)
        .filter(PaymentSanction.employee_id == employee.id,
                PaymentSanction.period_id == recon.period_id)
        .all()
    )
    deductions = (
        db.query(Deduction)
        .filter(Deduction.employee_id == employee.id,
                Deduction.period_id == recon.period_id)
        .all()
    )
    superseded = []
    if entitlement:
        superseded = (
            db.query(Entitlement)
            .filter(Entitlement.employee_id == employee.id,
                    Entitlement.period_id == recon.period_id,
                    Entitlement.is_current.is_(False))
            .all()
        )

    from ..audit import history

    context.update({
        "employee": employee,
        "recon": recon,
        "period": recon.period,
        "entitlement": entitlement,
        "superseded_entitlements": superseded,
        "allocations": allocations,
        "sanctions": sanctions,
        "deductions": deductions,
        "documents": document_agent.evidence_for(db, "Reconciliation", recon.id),
        "document_requests": [r.as_dict(context["locale"])
                              for r in document_request_agent.for_reconciliation(recon)],
        "verification_gap": verification_agent.describe_gap(
            recon.credit_verification_level, recon.sanction_verification_level, context["locale"]),
        "audit_entries": history(db, "Reconciliation", recon.id, limit=30),
        "VerificationDimension": VerificationDimension,
    })
    return _templates().TemplateResponse("period_detail.html", context)


# ---------------------------------------------------------------------------
# Ledger pages
# ---------------------------------------------------------------------------
@router.get("/arrears", response_class=HTMLResponse)
def arrears_page(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)

    query = db.query(Arrear)
    if employee is not None:
        query = query.filter(Arrear.employee_id == employee.id)
    else:
        ids = [e.id for e in visible_employees(db, user).all()]
        query = query.filter(Arrear.employee_id.in_(ids or [-1]))

    arrears = sorted(query.all(),
                     key=lambda a: a.origin_period.sort_key if a.origin_period else 0)
    context.update({
        "employee": employee,
        "arrears": arrears,
        "total_outstanding": sum(a.remaining_paise for a in arrears),
        "total_settled": sum(a.settled_paise for a in arrears),
    })
    return _templates().TemplateResponse("arrears.html", context)


@router.get("/deductions", response_class=HTMLResponse)
def deductions_page(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)

    query = db.query(Deduction)
    if employee is not None:
        query = query.filter(Deduction.employee_id == employee.id)
    else:
        ids = [e.id for e in visible_employees(db, user).all()]
        query = query.filter(Deduction.employee_id.in_(ids or [-1]))

    deductions = sorted(query.all(), key=lambda d: d.period.sort_key if d.period else 0)
    context.update({
        "employee": employee,
        "deductions": deductions,
        "documented_total": sum(d.amount_paise for d in deductions
                                if d.status == DeductionStatus.DOCUMENTED.value),
        "expected_total": sum(d.amount_paise for d in deductions
                              if d.status == DeductionStatus.EXPECTED.value),
        "unexplained_total": sum(d.amount_paise for d in deductions
                                 if d.status == DeductionStatus.UNEXPLAINED.value),
        "DeductionStatus": DeductionStatus,
    })
    return _templates().TemplateResponse("deductions.html", context)


@router.get("/benefits", response_class=HTMLResponse)
def benefits_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    """Spec s26: in-kind benefits, shown entirely separately from cash."""
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)

    query = db.query(BenefitDistribution)
    if employee is not None:
        query = query.filter(BenefitDistribution.employee_id == employee.id)
    else:
        ids = [e.id for e in visible_employees(db, user).all()]
        query = query.filter(BenefitDistribution.employee_id.in_(ids or [-1]))

    rows = sorted(query.all(), key=lambda d: d.period.sort_key if d.period else 0, reverse=True)
    context.update({
        "employee": employee,
        "distributions": rows,
        "short_count": sum(1 for d in rows if d.shortage_quantity > 0),
        "estimated_value_paise": sum(d.estimated_value_paise or 0 for d in rows),
    })
    return _templates().TemplateResponse("benefits.html", context)


@router.get("/verification", response_class=HTMLResponse)
def verification_page(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user), employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    employee = resolve_employee(db, user, employee_uid)
    if employee is None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    recons = sorted(
        db.query(Reconciliation).filter(Reconciliation.employee_id == employee.id).all(),
        key=lambda r: r.period.sort_key if r.period else 0,
    )
    levels = []
    for recon in recons:
        levels.append({
            "recon": recon,
            "credit": recon.credit_verification_level,
            "sanction": recon.sanction_verification_level,
            "basis": recon.entitlement_verification_level,
            "gap": verification_agent.describe_gap(
                recon.credit_verification_level, recon.sanction_verification_level,
                context["locale"]),
        })

    context.update({
        "employee": employee,
        "levels": levels,
        "integrations": verification_agent.integrations_available(),
        "level_labels": {i: VerificationLevel.label(i, context["locale"]) for i in range(6)},
    })
    return _templates().TemplateResponse("verification.html", context)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    context["notifications"] = rows
    return _templates().TemplateResponse("notifications.html", context)


@router.post("/notifications/read-all")
def mark_all_read(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    from ..db import utcnow

    (db.query(Notification)
       .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
       .update({Notification.is_read: True, Notification.read_at: utcnow()}))
    db.commit()
    return RedirectResponse(url="/notifications", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
@router.get("/anomalies", response_class=HTMLResponse)
def anomalies_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   severity: Optional[str] = None, status_filter: Optional[str] = None):
    context = base_context(request, db, user)
    ids = [e.id for e in visible_employees(db, user).all()]

    query = db.query(Anomaly).filter(Anomaly.organization_id == user.organization_id)
    if not is_admin(user):
        query = query.filter(Anomaly.employee_id.in_(ids or [-1]))
    if severity:
        query = query.filter(Anomaly.severity == severity)
    query = query.filter(Anomaly.status == (status_filter or AnomalyStatus.OPEN.value))

    order = {Severity.CRITICAL.value: 0, Severity.HIGH.value: 1,
             Severity.MEDIUM.value: 2, Severity.LOW.value: 3}
    rows = sorted(query.all(), key=lambda a: (order.get(a.severity, 9), -(a.amount_paise or 0)))

    context.update({
        "anomalies": rows,
        "severity": severity,
        "status_filter": status_filter or AnomalyStatus.OPEN.value,
        "Severity": Severity,
        "AnomalyStatus": AnomalyStatus,
    })
    return _templates().TemplateResponse("anomalies.html", context)


@router.post("/anomalies/{uid}/status")
def update_anomaly(uid: str, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   new_status: str = Form(...), note: str = Form(""),
                   csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    from ..security import require_write

    require_write(user)

    anomaly = db.query(Anomaly).filter(Anomaly.uid == uid).first()
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    if anomaly.organization_id != user.organization_id:
        from ..security import PermissionDenied

        raise PermissionDenied("You do not have access to this finding.")

    if new_status not in AnomalyStatus.values():
        raise HTTPException(status_code=400, detail="Unknown status")

    before = {"status": anomaly.status, "resolution_note": anomaly.resolution_note}
    from ..db import utcnow

    anomaly.status = new_status
    anomaly.resolution_note = note or anomaly.resolution_note
    anomaly.resolved_by_id = user.id
    anomaly.resolved_at = utcnow() if new_status in {AnomalyStatus.RESOLVED.value,
                                                     AnomalyStatus.DISMISSED.value} else None

    audit_record(db, action="UPDATE_ANOMALY", entity_type="Anomaly", entity_id=anomaly.id,
                 entity_uid=anomaly.uid, organization_id=user.organization_id, actor=user,
                 old_value=before, new_value={"status": new_status, "resolution_note": note},
                 reason=note or f"Marked {new_status}", source=AuditSource.HUMAN)
    db.commit()

    back = request.headers.get("referer") or "/anomalies"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
