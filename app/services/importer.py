"""Bank statement import engine (spec s74, s75).

Pipeline: detect -> parse -> validate -> de-duplicate -> normalise ->
match employee -> classify -> match month -> reconcile -> audit.

Two guarantees:

* **Re-importing the same statement never doubles the ledger.** Each source
  row gets a deterministic ``row_hash``; a row already present is counted as a
  duplicate and skipped, not inserted again.
* **No row is silently discarded.** Anything unparseable becomes an
  :class:`ImportError` row that the user can see and act on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..db import utcnow
from ..enums import (
    Direction,
    JobStatus,
    PaymentClassification,
    TransactionStatus,
    VerificationLevel,
)
from ..models import (
    BankTransaction,
    Document,
    Employee,
    EmployeeBankAccount,
    ImportError as ImportErrorRow,
    ImportJob,
    Organization,
)
from .parsers.statement import ParsedRow, ParseResult, StatementParseError, parse_statement
from .textutils import name_similarity, normalize_narration


@dataclass
class ImportOutcome:
    job: ImportJob
    imported: list[BankTransaction] = field(default_factory=list)
    duplicates: int = 0
    failed: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def summary_line(self) -> str:
        return (
            f"{len(self.imported)} transaction(s) imported, "
            f"{self.duplicates} duplicate row(s) skipped, "
            f"{self.failed} row(s) could not be read."
        )


def compute_row_hash(organization_id: int, employee_id: Optional[int], row: ParsedRow) -> str:
    """Deterministic fingerprint of a source row.

    Deliberately includes the amount, date, direction and normalised narration
    but NOT the running balance -- some banks re-issue statements with
    recalculated balances, and that must not create phantom new rows.
    """
    parts = [
        str(organization_id),
        str(employee_id or ""),
        row.txn_date.isoformat(),
        row.direction,
        str(row.amount_paise),
        normalize_narration(row.description)[:200],
        (row.utr or "").upper(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _resolve_account(db: Session, employee: Employee,
                     txn_date: date) -> Optional[EmployeeBankAccount]:
    """The employee's account in force on ``txn_date`` (spec s24).

    Correctly attributing a payment to the account that was active at the time
    is what stops an account change from looking like duplicate payments.
    """
    candidates = [
        a for a in employee.bank_accounts
        if (a.effective_from is None or a.effective_from <= txn_date)
        and (a.effective_to is None or a.effective_to >= txn_date)
    ]
    if not candidates:
        return employee.primary_account
    for account in candidates:
        if account.is_primary:
            return account
    return candidates[0]


def suggest_employee(db: Session, organization_id: int, result: ParseResult,
                     account_holder: Optional[str] = None) -> tuple[Optional[Employee], str]:
    """Suggest which employee a statement belongs to.

    Cautious by design (spec s23): an account-number match is decisive, but a
    name match alone only suggests, and an ambiguous name returns nothing.
    """
    holder = account_holder or result.account_holder

    if result.account_last4:
        matches = (
            db.query(EmployeeBankAccount)
            .join(Employee, Employee.id == EmployeeBankAccount.employee_id)
            .filter(Employee.organization_id == organization_id,
                    EmployeeBankAccount.account_last4 == result.account_last4)
            .all()
        )
        if len(matches) == 1:
            return matches[0].employee, (
                f"Matched on bank account ending {result.account_last4}."
            )
        if len(matches) > 1 and holder:
            scored = [(name_similarity(holder, m.employee.full_name), m) for m in matches]
            scored.sort(key=lambda kv: -kv[0])
            if scored[0][0] >= 0.9 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
                return scored[0][1].employee, (
                    f"Matched on account ending {result.account_last4} and the account holder name."
                )
        if matches:
            return None, (
                f"More than one employee has an account ending {result.account_last4}. "
                "Please choose the correct employee."
            )

    if holder:
        employees = db.query(Employee).filter(Employee.organization_id == organization_id).all()
        scored = sorted(((name_similarity(holder, e.full_name), e) for e in employees),
                        key=lambda kv: -kv[0])
        if scored and scored[0][0] >= 0.92:
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if scored[0][0] - runner_up >= 0.1:
                return scored[0][1], (
                    f"Suggested from the account holder name '{holder}'. "
                    "Please confirm this is the right employee."
                )
        return None, (
            f"The account holder name '{holder}' did not clearly match a single employee. "
            "Please choose the employee this statement belongs to."
        )

    return None, "The statement does not identify its account holder. Please choose the employee."


def import_statement(
    db: Session,
    *,
    organization: Organization,
    filename: str,
    content: bytes,
    employee: Optional[Employee],
    document: Optional[Document] = None,
    created_by_id: Optional[int] = None,
    actor_label: str = "system",
    dayfirst: bool = True,
) -> ImportOutcome:
    """Parse and import a statement file. Always records an ImportJob."""
    job = ImportJob(
        organization_id=organization.id,
        employee_id=employee.id if employee else None,
        document_id=document.id if document else None,
        source_type=_source_type(filename),
        filename=filename,
        status=JobStatus.PROCESSING.value,
        started_at=utcnow(),
        created_by_id=created_by_id,
    )
    db.add(job)
    db.flush()

    outcome = ImportOutcome(job=job)

    try:
        result = parse_statement(filename, content, dayfirst=dayfirst)
    except StatementParseError as exc:
        job.status = JobStatus.FAILED.value
        job.finished_at = utcnow()
        job.summary_json = {"error": str(exc)}
        db.add(ImportErrorRow(import_job_id=job.id, error=str(exc), severity="HIGH"))
        db.flush()
        outcome.messages.append(str(exc))
        return outcome

    job.detected_format = result.detected_format
    job.rows_total = len(result.rows) + len(result.errors)

    # --- Record every unreadable row ---
    for err in result.errors:
        db.add(ImportErrorRow(
            import_job_id=job.id,
            row_number=err.row_number,
            page_number=err.page_number,
            raw_row=err.raw[:2000],
            error=err.error,
            severity=err.severity,
        ))
    outcome.failed = len(result.errors)

    if result.pages_failed:
        db.add(ImportErrorRow(
            import_job_id=job.id,
            error=(
                "No transactions could be read from page(s) "
                + ", ".join(str(p) for p in result.pages_failed)
                + ". If these pages are scanned images, upload a clearer copy or enter "
                  "those transactions manually."
            ),
            severity="MEDIUM",
        ))

    # --- Insert transactions ---
    for row in result.rows:
        row_hash = compute_row_hash(organization.id, employee.id if employee else None, row)

        existing = (
            db.query(BankTransaction)
            .filter(BankTransaction.organization_id == organization.id,
                    BankTransaction.row_hash == row_hash)
            .first()
        )
        if existing is not None:
            outcome.duplicates += 1
            continue

        account = _resolve_account(db, employee, row.txn_date) if employee else None

        txn = BankTransaction(
            organization_id=organization.id,
            employee_id=employee.id if employee else None,
            bank_account_id=account.id if account else None,
            import_job_id=job.id,
            document_id=document.id if document else None,
            txn_date=row.txn_date,
            value_date=row.value_date,
            description=row.description,
            narration_raw=row.raw,
            narration_normalized=row.narration_normalized,
            direction=row.direction,
            amount_paise=row.amount_paise,
            balance_paise=row.balance_paise,
            utr=row.utr,
            reference_number=row.reference,
            counterparty=row.counterparty,
            bank_name=result.bank_name or (account.bank_name if account else None),
            row_hash=row_hash,
            source_row_number=row.row_number,
            classification=PaymentClassification.UNKNOWN.value,
            status=TransactionStatus.IMPORTED.value,
            verification_level=VerificationLevel.BANK_STATEMENT,
        )
        db.add(txn)
        outcome.imported.append(txn)

    db.flush()

    job.rows_imported = len(outcome.imported)
    job.rows_duplicate = outcome.duplicates
    job.rows_failed = outcome.failed
    job.status = (JobStatus.REQUIRES_REVIEW.value if outcome.failed or result.pages_failed
                  else JobStatus.COMPLETED.value)
    job.finished_at = utcnow()
    job.summary_json = {
        "detected_format": result.detected_format,
        "bank_name": result.bank_name,
        "account_last4": result.account_last4,
        "account_holder": result.account_holder,
        "pages_read": result.pages_read,
        "pages_failed": result.pages_failed,
        "notes": result.notes,
        "imported": len(outcome.imported),
        "duplicates": outcome.duplicates,
        "failed": outcome.failed,
    }
    db.flush()

    audit_record(
        db,
        action="IMPORT_STATEMENT",
        entity_type="ImportJob",
        entity_id=job.id,
        entity_uid=job.uid,
        organization_id=organization.id,
        actor_label=actor_label,
        new_value=job.summary_json,
        source=AuditSource.IMPORT,
        reason=f"Imported {filename}",
    )

    outcome.messages.append(outcome.summary_line)
    return outcome


def _source_type(filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return {
        ".pdf": "BANK_STATEMENT_PDF",
        ".csv": "BANK_STATEMENT_CSV",
        ".txt": "BANK_STATEMENT_CSV",
        ".xlsx": "BANK_STATEMENT_XLSX",
        ".xls": "BANK_STATEMENT_XLSX",
    }.get(ext, "MANUAL")


def record_manual_transaction(
    db: Session,
    *,
    organization: Organization,
    employee: Employee,
    txn_date: date,
    amount_paise: int,
    description: str,
    direction: str = Direction.CREDIT.value,
    is_cash: bool = False,
    utr: Optional[str] = None,
    created_by_id: Optional[int] = None,
    actor_label: str = "system",
) -> BankTransaction:
    """Record a payment by hand, including cash (spec s25).

    A manual or cash entry is marked as such and keeps verification LEVEL 0.
    It is never given the same standing as an independently evidenced bank
    credit, no matter who entered it.
    """
    row = ParsedRow(
        row_number=0,
        txn_date=txn_date,
        amount_paise=amount_paise,
        direction=direction,
        description=description,
        utr=utr,
    )
    row_hash = compute_row_hash(organization.id, employee.id, row)

    txn = BankTransaction(
        organization_id=organization.id,
        employee_id=employee.id,
        bank_account_id=(_resolve_account(db, employee, txn_date).id
                         if _resolve_account(db, employee, txn_date) else None),
        txn_date=txn_date,
        description=description,
        narration_raw=description,
        narration_normalized=normalize_narration(description),
        direction=direction,
        amount_paise=amount_paise,
        utr=utr,
        row_hash=row_hash,
        classification=PaymentClassification.UNKNOWN.value,
        status=TransactionStatus.IMPORTED.value,
        is_manual_entry=True,
        is_cash=is_cash,
        verification_level=VerificationLevel.USER_CLAIM,
        notes=("CASH PAYMENT - DOCUMENT VERIFICATION REQUIRED" if is_cash else
               "Manually entered - not independently verified"),
    )
    db.add(txn)
    db.flush()

    audit_record(
        db,
        action="RECORD_MANUAL_PAYMENT",
        entity_type="BankTransaction",
        entity_id=txn.id,
        entity_uid=txn.uid,
        organization_id=organization.id,
        actor_label=actor_label,
        new_value={"amount_paise": amount_paise, "date": txn_date.isoformat(),
                   "is_cash": is_cash, "description": description},
        source=AuditSource.HUMAN,
        reason="Manual payment entry",
    )
    return txn
