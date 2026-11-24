"""Transaction list, detail page and the human-in-the-loop corrections (s40, s44)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import (
    allocation_agent,
    classification_agent,
    document_agent,
    month_matching_agent,
    orchestrator,
    reconciliation_agent,
)
from ..audit import AuditSource, record as audit_record, snapshot
from ..db import get_db
from ..deps import base_context, get_current_user, verify_csrf
from ..enums import (
    AllocationMethod,
    AllocationStatus,
    Direction,
    PaymentClassification,
    TransactionStatus,
    VerificationLevel,
)
from ..models import (
    BankTransaction,
    Employee,
    MonthCandidate,
    PaymentAllocation,
    SalaryPeriod,
    User,
)
from ..money import format_money, to_paise
from ..security import (
    PermissionDenied,
    can_access_employee,
    decrypt_value,
    is_admin,
    mask_account,
    require_write,
    visible_employees,
)

router = APIRouter()

TXN_AUDIT_FIELDS = ("classification", "classification_confidence", "status",
                    "employee_id", "verification_level", "notes")


def _templates():
    from ..main import templates

    return templates


def _get_txn(db: Session, user: User, uid: str) -> BankTransaction:
    txn = db.query(BankTransaction).filter(BankTransaction.uid == uid).first()
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.organization_id != user.organization_id:
        raise PermissionDenied("You do not have access to this transaction.")
    if txn.employee_id:
        employee = db.get(Employee, txn.employee_id)
        if not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to this transaction.")
    elif not is_admin(user):
        raise PermissionDenied("You do not have access to this transaction.")
    return txn


@router.get("/transactions", response_class=HTMLResponse)
def list_transactions(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user),
                      employee_uid: Optional[str] = None,
                      status_filter: Optional[str] = None,
                      classification: Optional[str] = None,
                      direction: Optional[str] = None,
                      q: Optional[str] = None,
                      min_amount: Optional[str] = None,
                      page: int = 1):
    context = base_context(request, db, user)
    page = max(1, page)
    per_page = 50

    visible_ids = [e.id for e in visible_employees(db, user).all()]
    query = db.query(BankTransaction).filter(
        BankTransaction.organization_id == user.organization_id
    )
    if not is_admin(user):
        query = query.filter(BankTransaction.employee_id.in_(visible_ids or [-1]))

    selected_employee = None
    if employee_uid:
        selected_employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if selected_employee and can_access_employee(db, user, selected_employee):
            query = query.filter(BankTransaction.employee_id == selected_employee.id)

    if status_filter:
        query = query.filter(BankTransaction.status == status_filter)
    if classification:
        query = query.filter(BankTransaction.classification == classification)
    if direction:
        query = query.filter(BankTransaction.direction == direction)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(BankTransaction.description.ilike(like)
                             | BankTransaction.utr.ilike(like)
                             | BankTransaction.counterparty.ilike(like))
    if min_amount:
        try:
            query = query.filter(BankTransaction.amount_paise >= to_paise(min_amount))
        except Exception:  # noqa: BLE001 - a bad filter must not break the page
            pass

    total = query.count()
    rows = (query.order_by(BankTransaction.txn_date.desc(), BankTransaction.id.desc())
                 .offset((page - 1) * per_page).limit(per_page).all())

    context.update({
        "transactions": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "selected_employee": selected_employee,
        "filters": {"status": status_filter, "classification": classification,
                    "direction": direction, "q": q, "min_amount": min_amount},
        "statuses": TransactionStatus.values(),
        "classifications": PaymentClassification.values(),
    })
    return _templates().TemplateResponse("transactions.html", context)


@router.get("/transactions/{uid}", response_class=HTMLResponse)
def transaction_detail(uid: str, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """Spec s40: every fact about one transaction, including AI reasoning."""
    context = base_context(request, db, user)
    txn = _get_txn(db, user, uid)

    candidates = sorted(
        db.query(MonthCandidate).filter(MonthCandidate.transaction_id == txn.id).all(),
        key=lambda c: -c.probability,
    )
    periods = sorted(
        db.query(SalaryPeriod).filter(
            SalaryPeriod.organization_id == user.organization_id).all(),
        key=lambda p: -p.sort_key,
    )
    duplicates = allocation_agent.find_possible_duplicates(db, txn)

    from ..audit import history

    account_display = None
    if txn.bank_account_id:
        from ..models import EmployeeBankAccount

        account = db.get(EmployeeBankAccount, txn.bank_account_id)
        if account:
            account_display = account.masked

    context.update({
        "txn": txn,
        "candidates": candidates,
        "periods": periods,
        "allocations": txn.allocations,
        "duplicates": duplicates,
        "documents": document_agent.evidence_for(db, "BankTransaction", txn.id),
        "audit_entries": history(db, "BankTransaction", txn.id, limit=40),
        "account_display": account_display,
        "classifications": PaymentClassification.values(),
        "remaining_paise": allocation_agent.remaining_paise(db, txn),
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
    })
    return _templates().TemplateResponse("transaction_detail.html", context)


# ---------------------------------------------------------------------------
# Human-in-the-loop actions (spec s44)
# ---------------------------------------------------------------------------
@router.post("/transactions/{uid}/assign-month")
def assign_month(uid: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user),
                 period_id: int = Form(...), amount: str = Form(""),
                 kind: str = Form(PaymentClassification.SALARY.value),
                 reason: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    period = db.get(SalaryPeriod, period_id)
    if period is None or period.organization_id != user.organization_id:
        raise HTTPException(status_code=400, detail="Unknown salary period")

    employee = db.get(Employee, txn.employee_id) if txn.employee_id else None
    if employee is None:
        raise HTTPException(status_code=400,
                            detail="Link this transaction to an employee before assigning a month.")

    remaining = allocation_agent.remaining_paise(db, txn)
    if amount.strip():
        try:
            amount_paise = to_paise(amount)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="The amount could not be understood.")
    else:
        amount_paise = remaining

    if amount_paise <= 0:
        raise HTTPException(status_code=400,
                            detail="This transaction is already fully assigned.")

    try:
        allocation = allocation_agent.allocate(
            db, txn, employee, period, amount_paise,
            kind=kind if kind in PaymentClassification.values() else PaymentClassification.SALARY.value,
            method=AllocationMethod.MANUAL.value,
            status=AllocationStatus.CONFIRMED.value,
            confidence=1.0,
            created_by_id=user.id,
            reasoning=reason or f"Assigned to {period.label} by {user.full_name}",
        )
    except allocation_agent.AllocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    month_matching_agent.confirm_month(db, txn, period)
    # Learn the organisation's habitual pay lag from this correction (s76).
    month_matching_agent.learn_from_correction(db, txn, period)

    if allocation.kind == PaymentClassification.ARREAR.value:
        allocation_agent.settle_arrears(db, allocation)

    audit_record(db, action="ASSIGN_SALARY_MONTH", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user,
                 new_value={"period": period.label, "amount_paise": amount_paise, "kind": kind},
                 reason=reason or "Salary month assigned by a user", source=AuditSource.HUMAN)

    reconciliation_agent.reconcile(db, employee, period)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/allocations/{allocation_id}/{action}")
def allocation_action(uid: str, allocation_id: int, action: str, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(get_current_user),
                      csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    allocation = db.get(PaymentAllocation, allocation_id)
    if allocation is None or allocation.transaction_id != txn.id:
        raise HTTPException(status_code=404, detail="Allocation not found")

    before = {"status": allocation.status}
    if action == "confirm":
        allocation_agent.confirm(db, allocation, user.id)
        if allocation.kind == PaymentClassification.ARREAR.value:
            allocation_agent.settle_arrears(db, allocation)
    elif action == "reject":
        allocation_agent.reject(db, allocation, user.id)
    else:
        raise HTTPException(status_code=400, detail="Unknown action")

    audit_record(db, action=f"ALLOCATION_{action.upper()}", entity_type="PaymentAllocation",
                 entity_id=allocation.id, entity_uid=allocation.uid,
                 organization_id=user.organization_id, actor=user,
                 old_value=before, new_value={"status": allocation.status},
                 source=AuditSource.HUMAN)

    if allocation.period_id and allocation.employee:
        period = db.get(SalaryPeriod, allocation.period_id)
        if period:
            reconciliation_agent.reconcile(db, allocation.employee, period)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/split")
def split_allocation(uid: str, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user),
                     allocation_id: int = Form(...),
                     period_ids: list[int] = Form(...),
                     amounts: list[str] = Form(...),
                     csrf_field: str = Form("", alias="csrf")):
    """Spec s44: split one allocation across several months."""
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    allocation = db.get(PaymentAllocation, allocation_id)
    if allocation is None or allocation.transaction_id != txn.id:
        raise HTTPException(status_code=404, detail="Allocation not found")

    if len(period_ids) != len(amounts):
        raise HTTPException(status_code=400, detail="Each part needs a month and an amount.")

    parts: list[tuple[Optional[int], int]] = []
    for period_id, raw in zip(period_ids, amounts):
        if not str(raw).strip():
            continue
        try:
            parts.append((period_id, to_paise(raw)))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"The amount '{raw}' is not valid.")

    if not parts:
        raise HTTPException(status_code=400, detail="No parts were provided.")

    try:
        created = allocation_agent.split(db, allocation, parts, user.id)
    except allocation_agent.AllocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit_record(db, action="SPLIT_ALLOCATION", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user,
                 new_value={"parts": [{"period_id": p, "amount_paise": a} for p, a in parts]},
                 reason="Allocation split by a user", source=AuditSource.HUMAN)

    employee = db.get(Employee, txn.employee_id)
    for allocation_row in created:
        if allocation_row.period_id:
            period = db.get(SalaryPeriod, allocation_row.period_id)
            if period and employee:
                reconciliation_agent.reconcile(db, employee, period)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/classify")
def reclassify(uid: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user),
               classification: str = Form(...), reason: str = Form(""),
               csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    if classification not in PaymentClassification.values():
        raise HTTPException(status_code=400, detail="Unknown classification")

    before = snapshot(txn, TXN_AUDIT_FIELDS)
    txn.classification = classification
    txn.classification_confidence = 1.0
    # Locking stops the next automatic run from overwriting a human decision.
    txn.classification_locked = True
    txn.classification_evidence_json = {
        "signals": [{"name": "human_decision",
                     "detail": f"Set by {user.full_name}" + (f": {reason}" if reason else ""),
                     "weight": 1.0, "suggests": classification}],
        "alternatives": [],
    }

    audit_record(db, action="RECLASSIFY_TRANSACTION", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user, old_value=before, new_value=snapshot(txn, TXN_AUDIT_FIELDS),
                 reason=reason or "Classification corrected by a user", source=AuditSource.HUMAN)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/link-employee")
def link_employee(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  employee_id: int = Form(...), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    employee = db.get(Employee, employee_id)
    if employee is None or not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to that employee.")

    before = snapshot(txn, TXN_AUDIT_FIELDS)
    txn.employee_id = employee.id
    account = employee.primary_account
    if account:
        txn.bank_account_id = account.id

    audit_record(db, action="LINK_TRANSACTION_EMPLOYEE", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user, old_value=before, new_value=snapshot(txn, TXN_AUDIT_FIELDS),
                 reason=f"Linked to {employee.full_name}", source=AuditSource.HUMAN)

    orchestrator.process_transaction(db, txn, employee, user.id)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/reprocess")
def reprocess(uid: str, request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user),
              csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    employee = db.get(Employee, txn.employee_id) if txn.employee_id else None
    result = orchestrator.process_transaction(db, txn, employee, user.id)

    audit_record(db, action="REPROCESS_TRANSACTION", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user,
                 new_value={"classification": result.classification,
                            "allocations": result.allocations_created,
                            "needs_review": result.needs_review},
                 reason="Reprocessed on request", source=AuditSource.agent("orchestrator"))
    if employee:
        reconciliation_agent.reconcile_employee(db, employee)
    db.commit()

    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/transactions/{uid}/note")
def add_note(uid: str, request: Request, db: Session = Depends(get_db),
             user: User = Depends(get_current_user),
             note: str = Form(...), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    txn = _get_txn(db, user, uid)

    before = {"notes": txn.notes}
    stamp = f"[{user.full_name}] {note.strip()}"
    txn.notes = f"{txn.notes}\n{stamp}" if txn.notes else stamp

    audit_record(db, action="ADD_TRANSACTION_NOTE", entity_type="BankTransaction",
                 entity_id=txn.id, entity_uid=txn.uid, organization_id=user.organization_id,
                 actor=user, old_value=before, new_value={"notes": txn.notes},
                 reason="Explanation added", source=AuditSource.HUMAN)
    db.commit()
    return RedirectResponse(url=f"/transactions/{uid}", status_code=status.HTTP_303_SEE_OTHER)
