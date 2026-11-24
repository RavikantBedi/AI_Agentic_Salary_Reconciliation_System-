"""Dispute management and automatic complaint preparation (spec s45, s46)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import document_agent
from ..audit import AuditSource, record as audit_record
from ..db import get_db, utcnow
from ..deps import base_context, get_current_user, verify_csrf
from ..enums import DisputeStatus, VerificationLevel
from ..models import (
    Dispute,
    DisputeEvent,
    Employee,
    PaymentAllocation,
    PaymentSanction,
    Reconciliation,
    User,
)
from ..money import format_money
from ..security import PermissionDenied, can_access_employee, is_admin, require_write, visible_employees

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


def _get_dispute(db: Session, user: User, uid: str) -> Dispute:
    dispute = db.query(Dispute).filter(Dispute.uid == uid).first()
    if dispute is None:
        raise HTTPException(status_code=404, detail="Dispute not found")
    employee = db.get(Employee, dispute.employee_id)
    if not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to this dispute.")
    return dispute


@router.get("/disputes", response_class=HTMLResponse)
def disputes_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  status_filter: Optional[str] = None):
    context = base_context(request, db, user)
    ids = [e.id for e in visible_employees(db, user).all()]

    query = db.query(Dispute).filter(Dispute.organization_id == user.organization_id)
    if not is_admin(user):
        query = query.filter(Dispute.employee_id.in_(ids or [-1]))
    if status_filter:
        query = query.filter(Dispute.status == status_filter)

    context.update({
        "disputes": query.order_by(Dispute.created_at.desc()).all(),
        "statuses": DisputeStatus.values(),
        "status_filter": status_filter,
    })
    return _templates().TemplateResponse("disputes.html", context)


@router.get("/disputes/new", response_class=HTMLResponse)
def new_dispute(request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user),
                recon_uid: Optional[str] = None):
    context = base_context(request, db, user)

    recon = None
    prepared = None
    if recon_uid:
        recon = db.query(Reconciliation).filter(Reconciliation.uid == recon_uid).first()
        if recon is not None:
            employee = db.get(Employee, recon.employee_id)
            if not can_access_employee(db, user, employee):
                raise PermissionDenied("You do not have access to this record.")
            prepared = prepare_report(db, recon, context["locale"])

    context.update({
        "recon": recon,
        "prepared": prepared,
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "reconciliations": _open_problem_periods(db, user),
    })
    return _templates().TemplateResponse("dispute_new.html", context)


def _open_problem_periods(db: Session, user: User) -> list[Reconciliation]:
    from ..enums import ReconOutcome, SHORTFALL_OUTCOMES

    ids = [e.id for e in visible_employees(db, user).all()]
    rows = (
        db.query(Reconciliation)
        .filter(Reconciliation.employee_id.in_(ids or [-1]))
        .all()
    )
    problems = [r for r in rows
                if r.outcome in SHORTFALL_OUTCOMES
                or r.outcome == ReconOutcome.EXCESS_CREDIT.value]
    return sorted(problems, key=lambda r: -(r.period.sort_key if r.period else 0))


# ---------------------------------------------------------------------------
# Automatic complaint preparation (spec s46)
# ---------------------------------------------------------------------------
def prepare_report(db: Session, recon: Reconciliation, locale: str = "en") -> str:
    """Build a strictly factual complaint document.

    Contains only figures and dates drawn from the ledger, with a chronology
    and an evidence list. It states what is unexplained and asks for
    clarification -- it never alleges wrongdoing (spec s46, s58).
    """
    employee = recon.employee
    period = recon.period

    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == period.id,
                PaymentAllocation.status != "REJECTED")
        .all()
    )
    sanctions = (
        db.query(PaymentSanction)
        .filter(PaymentSanction.employee_id == employee.id,
                PaymentSanction.period_id == period.id)
        .all()
    )
    documents = document_agent.evidence_for(db, "Reconciliation", recon.id)

    account = employee.primary_account
    lines: list[str] = []

    lines.append(f"SUBJECT: Request for clarification regarding {period.label} "
                 f"{'honorarium' if employee.designation and 'ANGANWADI' in (employee.designation.name or '').upper() else 'salary'} payment")
    lines.append("")
    lines.append("EMPLOYEE DETAILS")
    lines.append(f"  Name              : {employee.full_name}")
    lines.append(f"  Employee ID       : {employee.employee_code}")
    if employee.designation:
        lines.append(f"  Designation       : {employee.designation.name}")
    if employee.department:
        lines.append(f"  Department        : {employee.department.name}")
    if employee.scheme:
        lines.append(f"  Scheme            : {employee.scheme.name}")
    if employee.location:
        lines.append(f"  Location          : {employee.location.path}")
    if account:
        # Never print a full account number in a document that gets shared.
        lines.append(f"  Bank account      : {account.masked} ({account.bank_name or 'bank not recorded'})")
    lines.append("")

    lines.append(f"PERIOD: {period.label}")
    lines.append("")
    lines.append("FINANCIAL POSITION")
    lines.append(f"  Amount due under the applicable rule : {format_money(recon.entitled_paise)}")
    if recon.entitlement and recon.entitlement.rule:
        rule = recon.entitlement.rule
        basis = rule.government_order.order_number if rule.government_order else (rule.source_note or rule.label)
        lines.append(f"  Basis of entitlement                 : {basis}")
    if recon.sanctioned_paise is not None:
        lines.append(f"  Amount officially sanctioned         : {format_money(recon.sanctioned_paise)}")
    else:
        lines.append("  Amount officially sanctioned         : no sanction record available to the employee")
    lines.append(f"  Amount credited to the bank account  : {format_money(recon.credited_paise)}")
    if recon.deducted_paise:
        lines.append(f"  Authorised deductions on record      : {format_money(recon.deducted_paise)}")
    lines.append(f"  Difference                           : {format_money(abs(recon.difference_paise))} "
                 f"{'short' if recon.difference_paise > 0 else 'in excess'}")
    lines.append("")

    lines.append("TRANSACTION DETAILS")
    if allocations:
        for allocation in sorted(allocations, key=lambda a: a.transaction.txn_date if a.transaction else None):
            txn = allocation.transaction
            if txn is None:
                continue
            lines.append(f"  - {txn.txn_date.isoformat()}  {format_money(allocation.amount_paise)}"
                         + (f"  UTR/Ref: {txn.utr}" if txn.utr else "  (no reference recorded)"))
            if txn.description:
                lines.append(f"      Narration: {txn.description[:160]}")
    else:
        lines.append("  No credit has been identified for this period in the available bank records.")
    lines.append("")

    if sanctions:
        lines.append("SANCTION RECORDS")
        for sanction in sanctions:
            lines.append(f"  - {sanction.sanction_number or 'number not recorded'} dated "
                         f"{sanction.sanction_date.isoformat() if sanction.sanction_date else 'not recorded'}: "
                         f"{format_money(sanction.amount_paise)}")
        lines.append("")

    lines.append("CHRONOLOGY")
    lines.append(f"  - Salary period                : {period.cycle_start.isoformat()} to {period.cycle_end.isoformat()}")
    if period.expected_pay_date:
        lines.append(f"  - Payment normally expected on : {period.expected_pay_date.isoformat()}")
    if recon.first_credit_date:
        lines.append(f"  - First credit received on     : {recon.first_credit_date.isoformat()}"
                     + (f" ({recon.delay_days} days after the expected date)"
                        if recon.delay_days and recon.delay_days > 0 else ""))
    else:
        lines.append("  - No credit has been received to date")
    lines.append("")

    lines.append("EVIDENCE AVAILABLE")
    lines.append(f"  - Bank statement records held in the employee's payment file "
                 f"(verification level {recon.credit_verification_level})")
    for doc in documents:
        lines.append(f"  - {doc.title} ({doc.category}, uploaded {doc.created_at.date().isoformat()})")
    if not documents:
        lines.append("  - No official payment or sanction document has been provided to the employee")
    lines.append("")

    lines.append("CLARIFICATION REQUESTED")
    lines.append("  The employee respectfully requests clarification on the following:")
    lines.append(f"  1. Whether the full amount of {format_money(recon.entitled_paise)} was sanctioned "
                 f"for {period.label}, and if not, the reason for the lower sanction.")
    if recon.difference_paise > 0:
        lines.append(f"  2. The reason for the difference of {format_money(recon.difference_paise)} "
                     "between the amount due and the amount credited.")
        lines.append("  3. If a deduction was applied, the authority and rule under which it was made, "
                     "and a copy of the relevant order.")
        lines.append("  4. The expected date on which any outstanding amount will be paid.")
    else:
        lines.append("  2. The basis of the additional amount credited, so that it can be recorded correctly.")
    lines.append("")
    lines.append("NOTE")
    lines.append("  This statement has been prepared from the employee's own bank records and the")
    lines.append("  documents available to them. It records a difference that the available evidence")
    lines.append("  does not explain, and makes no allegation of any kind. The employee will readily")
    lines.append("  update these records on receipt of the relevant payment or sanction documents.")

    return "\n".join(lines)


@router.post("/disputes")
def create_dispute(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   employee_id: int = Form(...), recon_uid: str = Form(""),
                   title: str = Form(...), issue_type: str = Form("SHORT_PAYMENT"),
                   description: str = Form(""), authority: str = Form(""),
                   generated_report: str = Form(""),
                   csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    employee = db.get(Employee, employee_id)
    if employee is None or not can_access_employee(db, user, employee):
        raise PermissionDenied("You do not have access to that employee.")

    recon = None
    if recon_uid:
        recon = db.query(Reconciliation).filter(Reconciliation.uid == recon_uid).first()

    dispute = Dispute(
        organization_id=user.organization_id,
        employee_id=employee.id,
        period_id=recon.period_id if recon else None,
        reconciliation_id=recon.id if recon else None,
        issue_type=issue_type,
        title=title.strip()[:300],
        description=description.strip() or None,
        expected_paise=recon.entitled_paise if recon else None,
        received_paise=recon.credited_paise if recon else None,
        difference_paise=recon.difference_paise if recon else None,
        authority=authority.strip() or None,
        status=DisputeStatus.OPEN.value,
        generated_report=generated_report or (prepare_report(db, recon) if recon else None),
        created_by_id=user.id,
    )
    db.add(dispute)
    db.flush()

    db.add(DisputeEvent(dispute_id=dispute.id, event="CREATED",
                        note="Dispute raised", actor_user_id=user.id,
                        to_status=DisputeStatus.OPEN.value))

    audit_record(db, action="CREATE_DISPUTE", entity_type="Dispute", entity_id=dispute.id,
                 entity_uid=dispute.uid, organization_id=user.organization_id, actor=user,
                 new_value={"title": dispute.title, "issue_type": issue_type,
                            "difference_paise": dispute.difference_paise},
                 reason="Dispute raised", source=AuditSource.HUMAN)
    uid = dispute.uid
    db.commit()

    return RedirectResponse(url=f"/disputes/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/disputes/{uid}", response_class=HTMLResponse)
def dispute_detail(uid: str, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    dispute = _get_dispute(db, user, uid)

    from ..audit import history

    context.update({
        "dispute": dispute,
        "events": dispute.events,
        "documents": document_agent.evidence_for(db, "Dispute", dispute.id),
        "statuses": DisputeStatus.values(),
        "audit_entries": history(db, "Dispute", dispute.id, limit=30),
        "can_close": is_admin(user),
    })
    return _templates().TemplateResponse("dispute_detail.html", context)


@router.post("/disputes/{uid}/status")
def update_status(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  new_status: str = Form(...), note: str = Form(""),
                  response_text: str = Form(""), csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    dispute = _get_dispute(db, user, uid)

    if new_status not in DisputeStatus.values():
        raise HTTPException(status_code=400, detail="Unknown status")

    # Closing or resolving a dispute is a high-impact action (spec s84).
    if new_status in {DisputeStatus.RESOLVED.value, DisputeStatus.CLOSED.value,
                      DisputeStatus.REJECTED.value} and not is_admin(user):
        raise PermissionDenied("Only an administrator can resolve or close a dispute.")

    before = dispute.status
    dispute.status = new_status
    if response_text.strip():
        dispute.response = response_text.strip()
    if new_status == DisputeStatus.SUBMITTED.value and dispute.submitted_at is None:
        dispute.submitted_at = utcnow()
    if new_status in {DisputeStatus.RESOLVED.value, DisputeStatus.CLOSED.value,
                      DisputeStatus.REJECTED.value}:
        dispute.resolved_at = utcnow()
        dispute.resolution = note.strip() or dispute.resolution

    db.add(DisputeEvent(dispute_id=dispute.id, event="STATUS_CHANGED",
                        note=note.strip() or None, actor_user_id=user.id,
                        from_status=before, to_status=new_status))

    audit_record(db, action="UPDATE_DISPUTE_STATUS", entity_type="Dispute",
                 entity_id=dispute.id, entity_uid=dispute.uid,
                 organization_id=user.organization_id, actor=user,
                 old_value={"status": before}, new_value={"status": new_status},
                 reason=note.strip() or f"Status changed to {new_status}",
                 source=AuditSource.HUMAN)
    db.commit()

    return RedirectResponse(url=f"/disputes/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/disputes/{uid}/report", response_class=HTMLResponse)
def dispute_report(uid: str, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    dispute = _get_dispute(db, user, uid)

    report = dispute.generated_report
    if not report and dispute.reconciliation_id:
        recon = db.get(Reconciliation, dispute.reconciliation_id)
        if recon:
            report = prepare_report(db, recon, context["locale"])

    context.update({"dispute": dispute, "report": report or "No report has been prepared."})
    return _templates().TemplateResponse("dispute_report.html", context)
