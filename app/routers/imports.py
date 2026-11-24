"""Bank statement import, manual payment entry, and one-click processing."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import document_agent, orchestrator
from ..audit import AuditSource, record as audit_record
from ..db import get_db
from ..deps import base_context, get_current_user, verify_csrf
from ..enums import DocumentCategory, Direction, JobType, JobStatus
from ..models import AIJob, Employee, ImportJob, Organization, User
from ..money import to_paise
from ..security import (
    PermissionDenied,
    UploadRejected,
    can_access_employee,
    is_admin,
    require_write,
    validate_upload,
    visible_employees,
)
from ..services import importer, jobs

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user), error: Optional[str] = None):
    context = base_context(request, db, user)
    recent = (
        db.query(ImportJob)
        .filter(ImportJob.organization_id == user.organization_id)
        .order_by(ImportJob.created_at.desc())
        .limit(20)
        .all()
    )
    context.update({
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "recent_imports": recent,
        "error": error,
        "max_mb": context["settings"].max_upload_mb,
    })
    return _templates().TemplateResponse("import.html", context)


@router.post("/import")
async def import_statement(request: Request, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user),
                           statement: UploadFile = File(...),
                           employee_id: Optional[int] = Form(None),
                           date_format: str = Form("dayfirst"),
                           csrf_field: str = Form("", alias="csrf")):
    """Spec s74: the eleven-step import pipeline."""
    verify_csrf(request, csrf_field)
    require_write(user)

    content = await statement.read()
    filename = statement.filename or "statement"

    # --- Step 3: validate before anything touches the ledger ---
    try:
        mime = validate_upload(filename, content)
    except UploadRejected as exc:
        return RedirectResponse(url=f"/import?error={str(exc)[:300]}",
                                status_code=status.HTTP_303_SEE_OTHER)

    organization = db.get(Organization, user.organization_id)

    employee = None
    if employee_id:
        employee = db.get(Employee, employee_id)
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee.")
    elif user.employee_id:
        employee = db.get(Employee, user.employee_id)

    # --- Store the statement itself as evidence before parsing it ---
    document = document_agent.store(
        db, organization=organization, content=content, filename=filename,
        title=f"Bank statement - {filename}",
        category=DocumentCategory.BANK_STATEMENT.value,
        mime_type=mime, uploaded_by=user,
        employee_id=employee.id if employee else None,
        notes="Uploaded through the statement import screen.",
    )

    # --- Parse and import ---
    outcome = importer.import_statement(
        db, organization=organization, filename=filename, content=content,
        employee=employee, document=document, created_by_id=user.id,
        actor_label=f"{user.full_name} <{user.email}>",
        dayfirst=(date_format != "monthfirst"),
    )

    if outcome.job.status == JobStatus.FAILED.value:
        db.commit()
        return RedirectResponse(url=f"/import/jobs/{outcome.job.uid}",
                                status_code=status.HTTP_303_SEE_OTHER)

    # --- Steps 6-10 run through the orchestrator ---
    if employee is not None:
        for txn in outcome.imported:
            orchestrator.process_transaction(db, txn, employee, user.id)
        orchestrator.process_employee(db, employee, user.id)
    else:
        # Without a named employee we still store the rows, but they cannot be
        # reconciled until someone says who they belong to.
        suggestion, note = importer.suggest_employee(
            db, organization.id, _reparse(filename, content)
        )
        outcome.job.summary_json = {**(outcome.job.summary_json or {}),
                                    "employee_suggestion": suggestion.full_name if suggestion else None,
                                    "employee_suggestion_id": suggestion.id if suggestion else None,
                                    "employee_note": note}

    db.commit()
    return RedirectResponse(url=f"/import/jobs/{outcome.job.uid}",
                            status_code=status.HTTP_303_SEE_OTHER)


def _reparse(filename: str, content: bytes):
    """Parse again purely to read the statement header for a suggestion."""
    from ..services.parsers.statement import ParseResult, StatementParseError, parse_statement

    try:
        return parse_statement(filename, content)
    except StatementParseError:
        return ParseResult()


@router.get("/import/jobs/{uid}", response_class=HTMLResponse)
def import_job_detail(uid: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    job = db.query(ImportJob).filter(ImportJob.uid == uid).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Import not found")
    if job.organization_id != user.organization_id:
        raise PermissionDenied("You do not have access to this import.")

    from ..models import BankTransaction

    transactions = (
        db.query(BankTransaction)
        .filter(BankTransaction.import_job_id == job.id)
        .order_by(BankTransaction.txn_date)
        .all()
    )
    context.update({
        "job": job,
        "errors": job.errors,
        "transactions": transactions,
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
    })
    return _templates().TemplateResponse("import_job.html", context)


# ---------------------------------------------------------------------------
# Manual and cash entry (spec s25)
# ---------------------------------------------------------------------------
@router.get("/payments/manual", response_class=HTMLResponse)
def manual_page(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    context.update({
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "today": date.today().isoformat(),
    })
    return _templates().TemplateResponse("manual_payment.html", context)


@router.post("/payments/manual")
def manual_submit(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  employee_id: int = Form(...), txn_date: str = Form(...),
                  amount: str = Form(...), description: str = Form(...),
                  payment_mode: str = Form("CASH"), utr: str = Form(""),
                  csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    employee = db.get(Employee, employee_id)
    if employee is None or not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to that employee.")

    try:
        when = date.fromisoformat(txn_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="The date is not valid.")
    try:
        amount_paise = to_paise(amount)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="The amount is not valid.")
    if amount_paise <= 0:
        raise HTTPException(status_code=400, detail="The amount must be greater than zero.")

    organization = db.get(Organization, user.organization_id)
    txn = importer.record_manual_transaction(
        db, organization=organization, employee=employee, txn_date=when,
        amount_paise=amount_paise, description=description,
        direction=Direction.CREDIT.value,
        is_cash=(payment_mode == "CASH"),
        utr=utr.strip() or None,
        created_by_id=user.id, actor_label=f"{user.full_name} <{user.email}>",
    )
    orchestrator.process_transaction(db, txn, employee, user.id)
    orchestrator.process_employee(db, employee, user.id)
    db.commit()

    return RedirectResponse(url=f"/transactions/{txn.uid}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# One-click processing (spec s102)
# ---------------------------------------------------------------------------
@router.post("/process")
def process_now(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user),
                employee_id: Optional[int] = Form(None),
                csrf_field: str = Form("", alias="csrf")):
    """Queue a full processing run and show its progress."""
    verify_csrf(request, csrf_field)
    require_write(user)

    if employee_id:
        employee = db.get(Employee, employee_id)
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee.")
        job = jobs.enqueue(db, organization_id=user.organization_id,
                           job_type=JobType.RECONCILE_PERIOD.value,
                           payload={"employee_id": employee.id}, created_by_id=user.id,
                           message=f"Processing records for {employee.full_name}")
    elif is_admin(user):
        job = jobs.enqueue(db, organization_id=user.organization_id,
                           job_type=JobType.RECONCILE_ORG.value, created_by_id=user.id,
                           message="Processing all employees")
    elif user.employee_id:
        job = jobs.enqueue(db, organization_id=user.organization_id,
                           job_type=JobType.RECONCILE_PERIOD.value,
                           payload={"employee_id": user.employee_id}, created_by_id=user.id,
                           message="Processing your records")
    else:
        raise PermissionDenied("There is nothing to process for this account.")

    uid = job.uid
    db.commit()
    return RedirectResponse(url=f"/jobs/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/jobs/{uid}", response_class=HTMLResponse)
def job_detail(uid: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    job = db.query(AIJob).filter(AIJob.uid == uid).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.organization_id != user.organization_id:
        raise PermissionDenied("You do not have access to this job.")
    context["job"] = job
    return _templates().TemplateResponse("job.html", context)


@router.get("/api/jobs/{uid}")
def job_status(uid: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Polled by the job page so progress updates without a reload."""
    job = db.query(AIJob).filter(AIJob.uid == uid).first()
    if job is None or job.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "uid": job.uid, "status": job.status, "percent": job.percent,
        "progress": job.progress, "total": job.total, "message": job.message,
        "error": job.error, "result": job.result_json,
    }


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    rows = (
        db.query(AIJob)
        .filter(AIJob.organization_id == user.organization_id)
        .order_by(AIJob.created_at.desc())
        .limit(50)
        .all()
    )
    context["jobs"] = rows
    return _templates().TemplateResponse("jobs.html", context)
