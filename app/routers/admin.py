"""Administration: employees, rules, rule builder, simulation, audit, data quality."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import arrear_agent, monthly_close_agent, rule_agent
from ..audit import AuditSource, record as audit_record, snapshot
from ..db import get_db, utcnow
from ..deps import base_context, get_current_user, require_admin_user, verify_csrf
from ..enums import (
    AnomalyStatus,
    ComponentKind,
    Direction,
    EmployeeStatus,
    FundingSource,
    JobType,
    LocationLevel,
    PeriodStatus,
    ProrationPolicy,
    Role,
    RuleStatus,
    TransactionStatus,
    VerificationLevel,
)
from ..models import (
    Anomaly,
    AuditLog,
    BankTransaction,
    Department,
    Designation,
    Document,
    Employee,
    EmployeeBankAccount,
    Entitlement,
    GovernmentOrder,
    KnowledgeConflict,
    Location,
    Organization,
    Reconciliation,
    SalaryPeriod,
    SalaryRule,
    SalaryRuleComponent,
    Scheme,
    User,
)
from ..money import format_money, to_paise
from ..security import (
    PermissionDenied,
    can_access_employee,
    encrypt_value,
    hash_password,
    is_admin,
    last4,
    require_write,
    visible_employees,
)
from ..services import jobs
from ..services.textutils import normalize_name

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


# ---------------------------------------------------------------------------
# Admin dashboard (spec s48)
# ---------------------------------------------------------------------------
@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_admin_user),
               year: Optional[int] = None):
    context = base_context(request, db, user)
    year = year or date.today().year

    ids = [e.id for e in visible_employees(db, user).all()]
    recons = (
        db.query(Reconciliation)
        .join(SalaryPeriod, SalaryPeriod.id == Reconciliation.period_id)
        .filter(Reconciliation.organization_id == user.organization_id,
                Reconciliation.employee_id.in_(ids or [-1]),
                SalaryPeriod.year == year)
        .all()
    )

    totals = {
        "employees": len(ids),
        "entitled_paise": sum(r.entitled_paise for r in recons),
        "sanctioned_paise": sum(r.sanctioned_paise or 0 for r in recons),
        "received_paise": sum(r.credited_paise for r in recons),
        "pending_paise": sum(max(0, r.difference_paise) for r in recons),
        "excess_paise": sum(abs(min(0, r.difference_paise)) for r in recons),
        "deducted_paise": sum(r.deducted_paise for r in recons),
        "unexplained_paise": sum(r.unexplained_deduction_paise for r in recons),
        "arrears_paise": sum(a.remaining_paise for a in
                             arrear_agent.open_arrears_for_ids(db, ids)) if ids else 0,
        "unverified": db.query(BankTransaction).filter(
            BankTransaction.organization_id == user.organization_id,
            BankTransaction.direction == Direction.CREDIT.value,
            BankTransaction.verification_level < VerificationLevel.OFFICIAL_DOCUMENT).count(),
        "high_risk": db.query(Anomaly).filter(
            Anomaly.organization_id == user.organization_id,
            Anomaly.status == AnomalyStatus.OPEN.value,
            Anomaly.severity.in_(["HIGH", "CRITICAL"])).count(),
    }

    monitor = monthly_close_agent.monitor(db, user.organization_id)
    periods = sorted(db.query(SalaryPeriod)
                     .filter(SalaryPeriod.organization_id == user.organization_id).all(),
                     key=lambda p: -p.sort_key)

    context.update({
        "totals": totals,
        "monitor": monitor,
        "year": year,
        "years": sorted({p.year for p in periods}) or [year],
        "periods": periods[:12],
        "employees": visible_employees(db, user).order_by(Employee.full_name).limit(50).all(),
        "annual": monthly_close_agent.annual_summary(db, user.organization_id, year),
    })
    return _templates().TemplateResponse("admin.html", context)


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@router.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user), q: Optional[str] = None,
                   status_filter: Optional[str] = None, error: Optional[str] = None):
    context = base_context(request, db, user)
    if not is_admin(user) and user.role not in {Role.AUDITOR.value, Role.READ_ONLY.value}:
        raise PermissionDenied("This page requires an administrator or auditor account.")

    query = visible_employees(db, user)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(Employee.full_name.ilike(like)
                             | Employee.employee_code.ilike(like))
    if status_filter:
        query = query.filter(Employee.status == status_filter)

    employees = query.order_by(Employee.full_name).all()
    health = {e.id: monthly_close_agent.payment_health(db, e) for e in employees}

    context.update({
        "employees": employees,
        "health": health,
        "q": q,
        "status_filter": status_filter,
        "statuses": EmployeeStatus.values(),
        "designations": db.query(Designation).filter(
            Designation.organization_id == user.organization_id).all(),
        "departments": db.query(Department).filter(
            Department.organization_id == user.organization_id).all(),
        "schemes": db.query(Scheme).filter(Scheme.organization_id == user.organization_id).all(),
        "locations": db.query(Location).filter(
            Location.organization_id == user.organization_id).all(),
        "error": error,
    })
    return _templates().TemplateResponse("employees.html", context)


@router.post("/employees")
def create_employee(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_admin_user),
                    employee_code: str = Form(...), full_name: str = Form(...),
                    designation_id: Optional[int] = Form(None),
                    department_id: Optional[int] = Form(None),
                    scheme_id: Optional[int] = Form(None),
                    location_id: Optional[int] = Form(None),
                    joining_date: str = Form(""), phone: str = Form(""),
                    bank_name: str = Form(""), account_number: str = Form(""),
                    ifsc: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    code = employee_code.strip()
    if not code or not full_name.strip():
        return RedirectResponse(url="/employees?error=An employee ID and name are required.",
                                status_code=status.HTTP_303_SEE_OTHER)

    existing = (
        db.query(Employee)
        .filter(Employee.organization_id == user.organization_id,
                Employee.employee_code == code)
        .first()
    )
    if existing:
        return RedirectResponse(
            url=f"/employees?error=Employee ID '{code}' is already in use.",
            status_code=status.HTTP_303_SEE_OTHER)

    employee = Employee(
        organization_id=user.organization_id,
        employee_code=code,
        full_name=full_name.strip(),
        name_normalized=normalize_name(full_name),
        designation_id=designation_id or None,
        department_id=department_id or None,
        scheme_id=scheme_id or None,
        location_id=location_id or None,
        phone=phone.strip() or None,
        status=EmployeeStatus.ACTIVE.value,
    )
    if joining_date:
        try:
            employee.joining_date = date.fromisoformat(joining_date)
        except ValueError:
            pass

    db.add(employee)
    db.flush()

    if account_number.strip():
        db.add(EmployeeBankAccount(
            employee_id=employee.id, bank_name=bank_name.strip() or None,
            account_number_encrypted=encrypt_value(account_number.strip()),
            account_last4=last4(account_number), ifsc=ifsc.strip().upper() or None,
            account_holder_name=employee.full_name,
            effective_from=employee.joining_date, is_primary=True,
        ))

    audit_record(db, action="CREATE_EMPLOYEE", entity_type="Employee", entity_id=employee.id,
                 entity_uid=employee.uid, organization_id=user.organization_id, actor=user,
                 new_value={"employee_code": code, "full_name": employee.full_name},
                 reason="Employee added", source=AuditSource.HUMAN)
    db.commit()

    return RedirectResponse(url=f"/employees/{employee.uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/employees/{uid}", response_class=HTMLResponse)
def employee_profile(uid: str, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Spec s88: the full employee profile."""
    context = base_context(request, db, user)
    employee = db.query(Employee).filter(Employee.uid == uid).first()
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to this employee.")

    recons = sorted(
        db.query(Reconciliation).filter(Reconciliation.employee_id == employee.id).all(),
        key=lambda r: -(r.period.sort_key if r.period else 0),
    )
    from ..audit import history
    from ..models import Dispute

    context.update({
        "employee": employee,
        "recons": recons,
        "transactions": db.query(BankTransaction).filter(
            BankTransaction.employee_id == employee.id
        ).order_by(BankTransaction.txn_date.desc()).limit(30).all(),
        "documents": db.query(Document).filter(
            Document.employee_id == employee.id, Document.is_current.is_(True)).all(),
        "disputes": db.query(Dispute).filter(Dispute.employee_id == employee.id).all(),
        "health": monthly_close_agent.payment_health(db, employee),
        "audit_entries": history(db, "Employee", employee.id, limit=25),
        "current_rule": _current_rule(db, employee),
    })
    return _templates().TemplateResponse("employee_profile.html", context)


def _current_rule(db: Session, employee: Employee) -> Optional[SalaryRule]:
    today = date.today()
    resolution = rule_agent.resolve_for_range(db, employee, today, today)
    return resolution.primary_rule


# ---------------------------------------------------------------------------
# Salary rules and the no-code builder (spec s79, s80)
# ---------------------------------------------------------------------------
@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_admin_user), error: Optional[str] = None):
    context = base_context(request, db, user)
    rules = (
        db.query(SalaryRule)
        .filter(SalaryRule.organization_id == user.organization_id)
        .order_by(SalaryRule.effective_from.desc(), SalaryRule.rule_code)
        .all()
    )
    conflicts = (
        db.query(KnowledgeConflict)
        .filter(KnowledgeConflict.organization_id == user.organization_id,
                KnowledgeConflict.status == "OPEN")
        .all()
    )
    context.update({
        "rules": rules,
        "conflicts": conflicts,
        "designations": db.query(Designation).filter(
            Designation.organization_id == user.organization_id).all(),
        "departments": db.query(Department).filter(
            Department.organization_id == user.organization_id).all(),
        "schemes": db.query(Scheme).filter(Scheme.organization_id == user.organization_id).all(),
        "locations": db.query(Location).filter(
            Location.organization_id == user.organization_id).all(),
        "orders": db.query(GovernmentOrder).filter(
            GovernmentOrder.organization_id == user.organization_id).all(),
        "proration_policies": ProrationPolicy.values(),
        "component_kinds": ComponentKind.values(),
        "funding_sources": FundingSource.values(),
        "RuleStatus": RuleStatus,
        "error": error,
    })
    return _templates().TemplateResponse("rules.html", context)


@router.post("/rules")
def create_rule(request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_admin_user),
                rule_code: str = Form(...), name: str = Form(...),
                effective_from: str = Form(...), effective_to: str = Form(""),
                scope_state: str = Form(""),
                scope_designation_id: Optional[int] = Form(None),
                scope_department_id: Optional[int] = Form(None),
                scope_scheme_id: Optional[int] = Form(None),
                scope_location_id: Optional[int] = Form(None),
                proration_policy: str = Form(ProrationPolicy.NONE.value),
                allow_mid_month_split: str = Form(""),
                government_order_id: Optional[int] = Form(None),
                source_note: str = Form(""),
                component_code: list[str] = Form([]),
                component_label: list[str] = Form([]),
                component_kind: list[str] = Form([]),
                component_funding: list[str] = Form([]),
                component_amount: list[str] = Form([]),
                component_prorated: list[str] = Form([]),
                csrf_field: str = Form("", alias="csrf")):
    """Create a DRAFT rule. Draft rules never affect calculations (spec s80)."""
    verify_csrf(request, csrf_field)
    require_write(user)

    try:
        eff_from = date.fromisoformat(effective_from)
    except ValueError:
        return RedirectResponse(url="/rules?error=The effective-from date is not valid.",
                                status_code=status.HTTP_303_SEE_OTHER)

    eff_to = None
    if effective_to:
        try:
            eff_to = date.fromisoformat(effective_to)
        except ValueError:
            eff_to = None

    if not government_order_id and not source_note.strip():
        return RedirectResponse(
            url=("/rules?error=A rule needs a source. Link a government order or "
                 "describe the source, so the amount can be traced."),
            status_code=status.HTTP_303_SEE_OTHER)

    version = 1 + (
        db.query(SalaryRule)
        .filter(SalaryRule.organization_id == user.organization_id,
                SalaryRule.rule_code == rule_code.strip())
        .count()
    )

    rule = SalaryRule(
        organization_id=user.organization_id,
        rule_code=rule_code.strip(), version=version, name=name.strip(),
        status=RuleStatus.DRAFT.value,
        effective_from=eff_from, effective_to=eff_to,
        scope_state=scope_state.strip() or None,
        scope_designation_id=scope_designation_id or None,
        scope_department_id=scope_department_id or None,
        scope_scheme_id=scope_scheme_id or None,
        scope_location_id=scope_location_id or None,
        proration_policy=(proration_policy if proration_policy in ProrationPolicy.values()
                          else ProrationPolicy.NONE.value),
        allow_mid_month_split=bool(allow_mid_month_split),
        government_order_id=government_order_id or None,
        source_note=source_note.strip() or None,
        created_by_id=user.id,
    )
    db.add(rule)
    db.flush()

    added = 0
    for index, code in enumerate(component_code):
        if not code.strip():
            continue
        try:
            amount = to_paise(component_amount[index]) if index < len(component_amount) else 0
        except Exception:  # noqa: BLE001
            continue
        db.add(SalaryRuleComponent(
            rule_id=rule.id, code=code.strip().upper(),
            label_en=(component_label[index] if index < len(component_label) else code).strip(),
            kind=(component_kind[index] if index < len(component_kind)
                  else ComponentKind.BASE.value),
            funding_source=(component_funding[index] if index < len(component_funding) else None),
            calc_method="FIXED", amount_paise=amount,
            is_prorated=str(index) in component_prorated,
            sort_order=index,
            source_note=source_note.strip() or None,
        ))
        added += 1

    if added == 0:
        db.rollback()
        return RedirectResponse(url="/rules?error=A rule needs at least one component.",
                                status_code=status.HTTP_303_SEE_OTHER)

    audit_record(db, action="CREATE_RULE", entity_type="SalaryRule", entity_id=rule.id,
                 entity_uid=rule.uid, organization_id=user.organization_id, actor=user,
                 new_value={"rule_code": rule.rule_code, "version": version,
                            "effective_from": eff_from.isoformat(), "components": added},
                 reason="Salary rule drafted", source=AuditSource.HUMAN)
    db.commit()

    return RedirectResponse(url=f"/rules/{rule.uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/rules/{uid}", response_class=HTMLResponse)
def rule_detail(uid: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_admin_user)):
    context = base_context(request, db, user)
    rule = db.query(SalaryRule).filter(SalaryRule.uid == uid).first()
    if rule is None or rule.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    from ..audit import history

    context.update({
        "rule": rule,
        "gross_paise": sum(c.amount_paise for c in rule.components
                           if c.kind != ComponentKind.DEDUCTION.value),
        "deduction_paise": sum(c.amount_paise for c in rule.components
                               if c.kind == ComponentKind.DEDUCTION.value),
        "audit_entries": history(db, "SalaryRule", rule.id, limit=25),
        "RuleStatus": RuleStatus,
    })
    return _templates().TemplateResponse("rule_detail.html", context)


@router.post("/rules/{uid}/simulate")
def simulate_rule(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_admin_user),
                  csrf_field: str = Form("", alias="csrf")):
    """Spec s80: simulate before publishing. Nothing is written."""
    verify_csrf(request, csrf_field)
    rule = db.query(SalaryRule).filter(SalaryRule.uid == uid).first()
    if rule is None or rule.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    job = jobs.enqueue(db, organization_id=user.organization_id,
                       job_type=JobType.RULE_SIMULATION.value,
                       payload={"rule_id": rule.id}, created_by_id=user.id,
                       message=f"Simulating {rule.label}")
    job_uid = job.uid
    db.commit()
    return RedirectResponse(url=f"/jobs/{job_uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/{uid}/activate")
def activate_rule(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_admin_user),
                  reason: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    """Spec s84: activating a rule is a high-impact action needing approval."""
    verify_csrf(request, csrf_field)
    require_write(user)

    rule = db.query(SalaryRule).filter(SalaryRule.uid == uid).first()
    if rule is None or rule.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    if not rule.has_source:
        raise HTTPException(status_code=400,
                            detail="This rule has no source. Link a government order first.")

    before = snapshot(rule, ("status", "effective_to", "approved_at"))
    superseded = rule_agent.activate(db, rule, approver_id=user.id)

    audit_record(db, action="ACTIVATE_RULE", entity_type="SalaryRule", entity_id=rule.id,
                 entity_uid=rule.uid, organization_id=user.organization_id, actor=user,
                 old_value=before, new_value={"status": rule.status,
                                              "superseded": [r.label for r in superseded]},
                 reason=reason or "Rule approved and activated", source=AuditSource.HUMAN)

    rule_agent.detect_conflicts(db, user.organization_id)
    db.commit()

    return RedirectResponse(url=f"/rules/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rules/{uid}/what-if")
def what_if(uid: str, request: Request, db: Session = Depends(get_db),
            user: User = Depends(get_current_user),
            employee_id: int = Form(...), csrf_field: str = Form("", alias="csrf")):
    """Spec s81: a scenario view that never touches real records."""
    verify_csrf(request, csrf_field)
    context = base_context(request, db, user)

    rule = db.query(SalaryRule).filter(SalaryRule.uid == uid).first()
    employee = db.get(Employee, employee_id)
    if rule is None or employee is None or not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to that record.")

    result = arrear_agent.simulate_retroactive_change(db, rule, [employee])
    db.rollback()   # discard everything the simulation touched

    context.update({
        "rule": rule, "employee": employee, "result": result,
        "gross_paise": sum(c.amount_paise for c in rule.components
                           if c.kind != ComponentKind.DEDUCTION.value),
        "deduction_paise": sum(c.amount_paise for c in rule.components
                               if c.kind == ComponentKind.DEDUCTION.value),
        "audit_entries": [],
        "RuleStatus": RuleStatus,
        "what_if": True,
    })
    return _templates().TemplateResponse("rule_detail.html", context)


# ---------------------------------------------------------------------------
# Monthly close
# ---------------------------------------------------------------------------
@router.post("/periods/{period_id}/close")
def close_period(period_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_admin_user),
                 reason: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    period = db.get(SalaryPeriod, period_id)
    if period is None or period.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Period not found")

    monthly_close_agent.close_period(db, period, user, reason or None)
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/periods/{period_id}/reopen")
def reopen_period(period_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_admin_user),
                  reason: str = Form(...), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    period = db.get(SalaryPeriod, period_id)
    if period is None or period.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Period not found")

    if not reason.strip():
        raise HTTPException(status_code=400,
                            detail="A reason is required to reopen a closed month.")

    monthly_close_agent.reopen_period(db, period, user, reason.strip())
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Audit log (spec s51, s90)
# ---------------------------------------------------------------------------
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user),
               entity_type: Optional[str] = None, action: Optional[str] = None,
               page: int = 1):
    context = base_context(request, db, user)
    if not is_admin(user) and user.role != Role.AUDITOR.value:
        raise PermissionDenied("The audit log requires an administrator or auditor account.")

    page = max(1, page)
    per_page = 100
    query = db.query(AuditLog).filter(AuditLog.organization_id == user.organization_id)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)

    total = query.count()
    entries = (query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .offset((page - 1) * per_page).limit(per_page).all())

    context.update({
        "entries": entries,
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "entity_type": entity_type,
        "action": action,
        "entity_types": sorted({e[0] for e in db.query(AuditLog.entity_type).distinct().all()}),
        "actions": sorted({a[0] for a in db.query(AuditLog.action).distinct().all()}),
    })
    return _templates().TemplateResponse("audit.html", context)


# ---------------------------------------------------------------------------
# Data quality dashboard (spec s82)
# ---------------------------------------------------------------------------
@router.get("/data-quality", response_class=HTMLResponse)
def data_quality(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_admin_user)):
    context = base_context(request, db, user)
    org_id = user.organization_id

    employees = db.query(Employee).filter(Employee.organization_id == org_id).all()
    periods = db.query(SalaryPeriod).filter(SalaryPeriod.organization_id == org_id).all()

    missing_rule = []
    for employee in employees:
        resolution = rule_agent.resolve_for_range(db, employee, date.today(), date.today())
        if not resolution.resolved:
            missing_rule.append({"employee": employee,
                                 "problem": resolution.problem or "No rule matched"})

    checks = {
        "missing_employee_code": [e for e in employees if not e.employee_code],
        "missing_bank_account": [e for e in employees if not e.bank_accounts],
        "missing_joining_date": [e for e in employees if not e.joining_date],
        "missing_salary_rule": missing_rule,
        "unmatched_transactions": db.query(BankTransaction).filter(
            BankTransaction.organization_id == org_id,
            BankTransaction.employee_id.is_(None)).all(),
        "review_transactions": db.query(BankTransaction).filter(
            BankTransaction.organization_id == org_id,
            BankTransaction.status == TransactionStatus.REVIEW_REQUIRED.value).all(),
        "low_confidence_documents": db.query(Document).filter(
            Document.organization_id == org_id,
            Document.extraction_confidence < 0.6,
            Document.ocr_status.notin_(["NOT_REQUIRED", "PENDING"])).all(),
        "conflicts": db.query(KnowledgeConflict).filter(
            KnowledgeConflict.organization_id == org_id,
            KnowledgeConflict.status == "OPEN").all(),
        "unverified_credits": db.query(BankTransaction).filter(
            BankTransaction.organization_id == org_id,
            BankTransaction.direction == Direction.CREDIT.value,
            BankTransaction.verification_level < VerificationLevel.OFFICIAL_DOCUMENT).count(),
        "duplicate_employee_flags": db.query(Anomaly).filter(
            Anomaly.organization_id == org_id,
            Anomaly.code == "POSSIBLE_DUPLICATE_EMPLOYEE",
            Anomaly.status == AnomalyStatus.OPEN.value).all(),
        "incomplete_periods": [p for p in periods
                               if db.query(Reconciliation).filter(
                                   Reconciliation.period_id == p.id).count() < len(employees)],
    }

    context.update({"checks": checks, "employee_count": len(employees)})
    return _templates().TemplateResponse("data_quality.html", context)


# ---------------------------------------------------------------------------
# Agentic monitor trigger (spec s63)
# ---------------------------------------------------------------------------
@router.post("/admin/monitor")
def run_monitor(request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_admin_user),
                csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    job = jobs.enqueue(db, organization_id=user.organization_id,
                       job_type=JobType.AGENTIC_MONITOR.value, created_by_id=user.id,
                       message="Running the periodic inspection")
    uid = job.uid
    db.commit()
    return RedirectResponse(url=f"/jobs/{uid}", status_code=status.HTTP_303_SEE_OTHER)
