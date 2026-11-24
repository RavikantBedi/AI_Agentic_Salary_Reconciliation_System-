"""Report generation and export (spec s47, s92, s96).

Every report distinguishes what is FACT, what is an OFFICIAL RECORD, what is
an AI INFERENCE and what is UNVERIFIED, so a reader always knows the standing
of each figure.

Export totals are computed from the same query that feeds the on-screen
figures, and are recorded on the :class:`GeneratedReport` row, so the
financial-integrity tests can assert that an export still agrees with the
database (spec s96).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..enums import (
    ArrearStatus,
    DeductionStatus,
    Direction,
    Provenance,
    ReconOutcome,
    VerificationLevel,
)
from ..models import (
    Arrear,
    BankTransaction,
    BenefitDistribution,
    Deduction,
    Employee,
    GeneratedReport,
    Organization,
    Reconciliation,
    SalaryPeriod,
    User,
)
from ..money import format_money


@dataclass
class ReportTable:
    title: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    totals: Optional[list[Any]] = None
    note: Optional[str] = None


@dataclass
class Report:
    title: str
    subtitle: str = ""
    generated_at: datetime = field(default_factory=datetime.now)
    meta: dict = field(default_factory=dict)
    tables: list[ReportTable] = field(default_factory=list)
    provenance_notes: list[tuple[str, str]] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    def add_note(self, provenance: str, text: str) -> None:
        self.provenance_notes.append((provenance, text))


REPORT_TYPES = {
    "employee_statement": "Employee Payment Statement",
    "monthly_salary": "Monthly Salary Report",
    "annual_salary": "Annual Salary Report",
    "pending_payments": "Pending Payment Report",
    "deductions": "Deduction Report",
    "arrears": "Arrear Report",
    "benefits": "Benefit Distribution Report",
    "verification": "Transaction Verification Report",
    "discrepancies": "Discrepancy Report",
    "government_payments": "Government Payment Report",
    "audit": "Audit Report",
    "department_summary": "Department Summary",
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def employee_statement(db: Session, employee: Employee, year: int) -> Report:
    """The statement a worker can take to an office (spec s107)."""
    org = employee.organization
    report = Report(
        title=f"Payment Statement - {employee.full_name}",
        subtitle=f"{org.name} | {year}",
        meta={
            "Employee ID": employee.employee_code,
            "Designation": employee.designation.name if employee.designation else "—",
            "Department": employee.department.name if employee.department else "—",
            "Scheme": employee.scheme.name if employee.scheme else "—",
            "Location": employee.location.path if employee.location else "—",
            "Bank account": employee.primary_account.masked if employee.primary_account else "—",
            "Joining date": employee.joining_date.isoformat() if employee.joining_date else "—",
        },
    )

    recons = sorted(
        [r for r in db.query(Reconciliation)
            .filter(Reconciliation.employee_id == employee.id).all()
         if r.period and r.period.year == year],
        key=lambda r: r.period.sort_key,
    )

    table = ReportTable(
        title="Month by month",
        columns=["Month", "Expected", "Sanctioned", "Received", "Difference",
                 "Delay (days)", "Status"],
    )
    total_expected = total_received = total_difference = 0
    for recon in recons:
        total_expected += recon.entitled_paise
        total_received += recon.credited_paise
        total_difference += recon.difference_paise
        table.rows.append([
            recon.period.label,
            format_money(recon.entitled_paise),
            format_money(recon.sanctioned_paise) if recon.sanctioned_paise is not None else "no record",
            format_money(recon.credited_paise),
            format_money(recon.difference_paise),
            recon.delay_days if recon.delay_days is not None else "—",
            recon.outcome.replace("_", " ").title(),
        ])
    table.totals = ["TOTAL", format_money(total_expected), "",
                    format_money(total_received), format_money(total_difference), "", ""]
    report.tables.append(table)

    # --- Deductions ---
    deductions = db.query(Deduction).filter(Deduction.employee_id == employee.id).all()
    deductions = [d for d in deductions if d.period and d.period.year == year]
    if deductions:
        dtable = ReportTable(title="Deductions",
                             columns=["Month", "Category", "Amount", "Status", "Authority"])
        for d in sorted(deductions, key=lambda d: d.period.sort_key):
            dtable.rows.append([d.period.label, d.category, format_money(d.amount_paise),
                                d.status, d.authority or "—"])
        dtable.totals = ["TOTAL", "", format_money(sum(d.amount_paise for d in deductions)), "", ""]
        report.tables.append(dtable)

    # --- Arrears ---
    arrears = db.query(Arrear).filter(Arrear.employee_id == employee.id).all()
    if arrears:
        atable = ReportTable(
            title="Arrears",
            columns=["Origin month", "Entitled", "Received", "Outstanding", "Settled", "Status"],
        )
        for a in sorted(arrears, key=lambda a: a.origin_period.sort_key):
            atable.rows.append([
                a.origin_period.label, format_money(a.original_entitlement_paise),
                format_money(a.previously_received_paise), format_money(a.outstanding_paise),
                format_money(a.settled_paise), a.status,
            ])
        atable.totals = ["TOTAL", "", "", format_money(sum(a.remaining_paise for a in arrears)), "", ""]
        report.tables.append(atable)

    # --- Benefits, kept visibly separate from cash (spec s26) ---
    benefits = [b for b in db.query(BenefitDistribution)
                .filter(BenefitDistribution.employee_id == employee.id).all()
                if b.period and b.period.year == year]
    if benefits:
        btable = ReportTable(
            title="In-kind benefits (not part of cash entitlement)",
            columns=["Month", "Item", "Expected", "Received", "Shortage", "Status"],
            note="These items are recorded separately and are never converted into salary.",
        )
        for b in sorted(benefits, key=lambda b: b.period.sort_key):
            btable.rows.append([
                b.period.label, b.item.name if b.item else "—",
                f"{b.expected_quantity:g} {b.item.unit if b.item else ''}",
                f"{b.received_quantity:g} {b.item.unit if b.item else ''}",
                f"{b.shortage_quantity:g}" if b.shortage_quantity else "—",
                b.status,
            ])
        report.tables.append(btable)

    report.totals = {
        "expected_paise": total_expected,
        "received_paise": total_received,
        "difference_paise": total_difference,
        "months": len(recons),
    }

    _add_standard_provenance(report, recons)
    return report


def _add_standard_provenance(report: Report, recons: list[Reconciliation]) -> None:
    """Spec s92: label the standing of the figures in the report."""
    credited = sum(r.credited_paise for r in recons)
    sanctioned = sum(r.sanctioned_paise or 0 for r in recons)
    has_sanction = any(r.sanctioned_paise is not None for r in recons)

    report.add_note(Provenance.FACT.value,
                    f"{format_money(credited)} was credited to the bank account, according to "
                    "the bank records held in this system.")
    if has_sanction:
        report.add_note(Provenance.OFFICIAL.value,
                        f"Official sanction records account for {format_money(sanctioned)}.")
    else:
        report.add_note(Provenance.UNVERIFIED.value,
                        "No official sanction records have been supplied. The sanctioned "
                        "amounts shown as 'no record' are unknown, not zero.")

    inferred = [r for r in recons if r.outcome not in
                {ReconOutcome.FULLY_RECONCILED.value, ReconOutcome.AWAITING_CONFIRMATION.value}]
    if inferred:
        report.add_note(Provenance.INFERENCE.value,
                        f"{len(inferred)} month(s) show a difference the available evidence does "
                        "not explain. These are findings that require verification, not "
                        "conclusions about anyone's conduct.")

    weak = [r for r in recons if r.credit_verification_level < VerificationLevel.OFFICIAL_DOCUMENT]
    if weak:
        report.add_note(Provenance.UNVERIFIED.value,
                        f"{len(weak)} month(s) rest on bank evidence alone. A bank credit proves "
                        "money arrived; it does not prove the payment was officially sanctioned.")


def pending_payments(db: Session, organization_id: int,
                     employee_ids: Optional[list[int]] = None) -> Report:
    report = Report(title="Pending Payment Report",
                    subtitle="Amounts outstanding across all periods")
    query = db.query(Arrear).filter(
        Arrear.status.in_([ArrearStatus.OPEN.value, ArrearStatus.PARTIALLY_SETTLED.value])
    )
    if employee_ids is not None:
        query = query.filter(Arrear.employee_id.in_(employee_ids or [-1]))

    table = ReportTable(
        title="Outstanding amounts",
        columns=["Employee", "Employee ID", "Origin month", "Entitled", "Received",
                 "Outstanding", "Reason"],
    )
    total = 0
    for a in sorted(query.all(), key=lambda a: (a.employee.full_name,
                                                a.origin_period.sort_key if a.origin_period else 0)):
        total += a.remaining_paise
        table.rows.append([
            a.employee.full_name, a.employee.employee_code,
            a.origin_period.label if a.origin_period else "—",
            format_money(a.original_entitlement_paise),
            format_money(a.previously_received_paise),
            format_money(a.remaining_paise), a.reason,
        ])
    table.totals = ["TOTAL", "", "", "", "", format_money(total), ""]
    report.tables.append(table)
    report.totals = {"outstanding_paise": total, "rows": len(table.rows)}
    report.add_note(Provenance.INFERENCE.value,
                    "Outstanding amounts are the difference between the rule-based entitlement "
                    "and the payments identified in the available records.")
    return report


def discrepancy_report(db: Session, organization_id: int, year: int,
                       employee_ids: Optional[list[int]] = None) -> Report:
    report = Report(title="Discrepancy Report", subtitle=f"Differences identified in {year}")

    query = (
        db.query(Reconciliation)
        .join(SalaryPeriod, SalaryPeriod.id == Reconciliation.period_id)
        .filter(Reconciliation.organization_id == organization_id, SalaryPeriod.year == year)
    )
    if employee_ids is not None:
        query = query.filter(Reconciliation.employee_id.in_(employee_ids or [-1]))

    rows = [r for r in query.all()
            if r.outcome not in {ReconOutcome.FULLY_RECONCILED.value,
                                 ReconOutcome.AWAITING_CONFIRMATION.value}]

    table = ReportTable(
        title="Months requiring attention",
        columns=["Employee", "Month", "Expected", "Sanctioned", "Received",
                 "Difference", "Finding"],
    )
    total = 0
    for r in sorted(rows, key=lambda r: (r.employee.full_name, r.period.sort_key)):
        total += r.difference_paise
        table.rows.append([
            r.employee.full_name, r.period.label, format_money(r.entitled_paise),
            format_money(r.sanctioned_paise) if r.sanctioned_paise is not None else "no record",
            format_money(r.credited_paise), format_money(r.difference_paise),
            r.outcome.replace("_", " ").title(),
        ])
    table.totals = ["TOTAL", "", "", "", "", format_money(total), ""]
    report.tables.append(table)
    report.totals = {"difference_paise": total, "rows": len(table.rows)}
    report.add_note(Provenance.INFERENCE.value,
                    "Each row records a difference between records. No conclusion is drawn about "
                    "why the difference exists or who is responsible.")
    return report


def verification_report(db: Session, organization_id: int,
                        employee_ids: Optional[list[int]] = None) -> Report:
    report = Report(title="Transaction Verification Report",
                    subtitle="Evidence level of every recorded credit")

    query = db.query(BankTransaction).filter(
        BankTransaction.organization_id == organization_id,
        BankTransaction.direction == Direction.CREDIT.value,
    )
    if employee_ids is not None:
        query = query.filter(BankTransaction.employee_id.in_(employee_ids or [-1]))

    table = ReportTable(
        title="Credits",
        columns=["Date", "Employee", "Amount", "Reference", "Classification",
                 "Verification level", "Source"],
    )
    counts: dict[int, int] = {}
    for txn in sorted(query.all(), key=lambda t: t.txn_date):
        counts[txn.verification_level] = counts.get(txn.verification_level, 0) + 1
        table.rows.append([
            txn.txn_date.isoformat(),
            txn.employee.full_name if txn.employee else "unassigned",
            format_money(txn.amount_paise), txn.utr or "—", txn.classification,
            VerificationLevel.label(txn.verification_level),
            "Manual entry" if txn.is_manual_entry else "Bank statement",
        ])
    report.tables.append(table)

    summary = ReportTable(title="Summary by verification level",
                          columns=["Level", "Description", "Count"])
    for level in sorted(counts):
        summary.rows.append([level, VerificationLevel.label(level), counts[level]])
    report.tables.append(summary)

    report.totals = {"transactions": len(table.rows), "by_level": counts}
    report.add_note(Provenance.FACT.value,
                    "Level 1 means the credit appears in an uploaded bank statement.")
    report.add_note(Provenance.USER_INPUT.value,
                    "Level 0 means the entry was recorded by hand and has not been "
                    "independently verified. Cash payments always start at Level 0.")
    return report


def deduction_report(db: Session, organization_id: int,
                     employee_ids: Optional[list[int]] = None) -> Report:
    report = Report(title="Deduction Report", subtitle="Every deduction and its authority")

    query = db.query(Deduction)
    if employee_ids is not None:
        query = query.filter(Deduction.employee_id.in_(employee_ids or [-1]))

    table = ReportTable(
        title="Deductions",
        columns=["Employee", "Month", "Category", "Amount", "Status", "Authority", "Evidence"],
    )
    unexplained = 0
    total = 0
    for d in sorted(query.all(), key=lambda d: (d.employee.full_name,
                                                d.period.sort_key if d.period else 0)):
        total += d.amount_paise
        if d.status == DeductionStatus.UNEXPLAINED.value:
            unexplained += d.amount_paise
        table.rows.append([
            d.employee.full_name, d.period.label if d.period else "—", d.category,
            format_money(d.amount_paise), d.status, d.authority or "—",
            "Yes" if d.evidence_document_id else "No",
        ])
    table.totals = ["TOTAL", "", "", format_money(total), "", "", ""]
    report.tables.append(table)

    report.totals = {"total_paise": total, "unexplained_paise": unexplained}
    if unexplained:
        report.add_note(Provenance.UNVERIFIED.value,
                        f"{format_money(unexplained)} of deductions have no authorising record. "
                        "A deduction is not treated as legitimate merely because the amount "
                        "received was lower.")
    return report


def benefit_report(db: Session, organization_id: int,
                   employee_ids: Optional[list[int]] = None) -> Report:
    report = Report(title="Benefit Distribution Report",
                    subtitle="In-kind items, recorded separately from cash")

    query = db.query(BenefitDistribution)
    if employee_ids is not None:
        query = query.filter(BenefitDistribution.employee_id.in_(employee_ids or [-1]))

    table = ReportTable(
        title="Distributions",
        columns=["Employee", "Month", "Item", "Expected", "Received", "Shortage",
                 "Date", "Status", "Verification"],
    )
    short_count = 0
    for d in sorted(query.all(), key=lambda d: (d.employee.full_name,
                                                d.period.sort_key if d.period else 0)):
        if d.shortage_quantity > 0:
            short_count += 1
        unit = d.item.unit if d.item else ""
        table.rows.append([
            d.employee.full_name, d.period.label if d.period else "—",
            d.item.name if d.item else "—",
            f"{d.expected_quantity:g} {unit}", f"{d.received_quantity:g} {unit}",
            f"{d.shortage_quantity:g} {unit}" if d.shortage_quantity else "—",
            d.distribution_date.isoformat() if d.distribution_date else "—",
            d.status, VerificationLevel.label(d.verification_level),
        ])
    report.tables.append(table)
    report.totals = {"rows": len(table.rows), "short_distributions": short_count}
    report.add_note(Provenance.UNVERIFIED.value,
                    "Quantities are as recorded by the employee or the distribution register. "
                    "A shortage indicates a difference in the records and requires evidence; "
                    "it is not an accusation.")
    return report


def government_payment_report(db: Session, organization_id: int, year: int,
                              employee_ids: Optional[list[int]] = None) -> Report:
    """Spec s30: the four-way view."""
    report = Report(title="Government Payment Report",
                    subtitle=f"Entitlement, sanction, disbursement and credit for {year}")

    query = (
        db.query(Reconciliation)
        .join(SalaryPeriod, SalaryPeriod.id == Reconciliation.period_id)
        .filter(Reconciliation.organization_id == organization_id, SalaryPeriod.year == year)
    )
    if employee_ids is not None:
        query = query.filter(Reconciliation.employee_id.in_(employee_ids or [-1]))

    table = ReportTable(
        title="Four-way reconciliation",
        columns=["Employee", "Month", "Entitlement", "Sanction", "Disbursement",
                 "Bank credit", "Outcome"],
    )
    totals = {"entitled": 0, "sanctioned": 0, "disbursed": 0, "credited": 0}
    for r in sorted(query.all(), key=lambda r: (r.employee.full_name, r.period.sort_key)):
        totals["entitled"] += r.entitled_paise
        totals["sanctioned"] += r.sanctioned_paise or 0
        totals["disbursed"] += r.disbursed_paise or 0
        totals["credited"] += r.credited_paise
        table.rows.append([
            r.employee.full_name, r.period.label, format_money(r.entitled_paise),
            format_money(r.sanctioned_paise) if r.sanctioned_paise is not None else "no record",
            format_money(r.disbursed_paise) if r.disbursed_paise is not None else "no record",
            format_money(r.credited_paise), r.outcome.replace("_", " ").title(),
        ])
    table.totals = ["TOTAL", "", format_money(totals["entitled"]),
                    format_money(totals["sanctioned"]), format_money(totals["disbursed"]),
                    format_money(totals["credited"]), ""]
    report.tables.append(table)
    report.totals = {f"{k}_paise": v for k, v in totals.items()}
    report.add_note(Provenance.OFFICIAL.value,
                    "Sanction and disbursement figures come only from documents uploaded to this "
                    "system. 'no record' means no such document has been supplied.")
    return report


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------
def to_csv(report: Report) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([report.title])
    if report.subtitle:
        writer.writerow([report.subtitle])
    writer.writerow([f"Generated: {report.generated_at.strftime('%d %b %Y %H:%M')}"])
    writer.writerow([])

    for key, value in report.meta.items():
        writer.writerow([key, value])
    if report.meta:
        writer.writerow([])

    for table in report.tables:
        writer.writerow([table.title])
        writer.writerow(table.columns)
        for row in table.rows:
            writer.writerow(row)
        if table.totals:
            writer.writerow(table.totals)
        if table.note:
            writer.writerow([table.note])
        writer.writerow([])

    if report.provenance_notes:
        writer.writerow(["EVIDENCE NOTES"])
        for provenance, text in report.provenance_notes:
            writer.writerow([provenance, text])

    # BOM so Excel opens the rupee sign and Hindi correctly.
    return buffer.getvalue().encode("utf-8-sig")


def to_excel(report: Report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    workbook.remove(workbook.active)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    title_font = Font(bold=True, size=13)

    for index, table in enumerate(report.tables):
        # Sheet names are limited to 31 characters and cannot contain []:*?/\
        safe = "".join(c for c in table.title if c not in "[]:*?/\\")[:28] or f"Table{index + 1}"
        sheet = workbook.create_sheet(safe if safe not in workbook.sheetnames
                                      else f"{safe[:26]}_{index}")
        sheet.append([report.title])
        sheet["A1"].font = title_font
        if report.subtitle:
            sheet.append([report.subtitle])
        sheet.append([f"Generated: {report.generated_at.strftime('%d %b %Y %H:%M')}"])
        sheet.append([])

        header_row = sheet.max_row + 1
        sheet.append(table.columns)
        for cell in sheet[header_row]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        for row in table.rows:
            sheet.append(row)
        if table.totals:
            sheet.append(table.totals)
            for cell in sheet[sheet.max_row]:
                cell.font = Font(bold=True)
        if table.note:
            sheet.append([])
            sheet.append([table.note])

        for column_index, column in enumerate(table.columns, start=1):
            width = max([len(str(column))]
                        + [len(str(r[column_index - 1])) for r in table.rows
                           if len(r) >= column_index]) + 3
            sheet.column_dimensions[sheet.cell(row=header_row, column=column_index)
                                    .column_letter].width = min(45, width)

    if report.provenance_notes:
        notes = workbook.create_sheet("Evidence notes")
        notes.append(["Standing", "Note"])
        for cell in notes[1]:
            cell.font = header_font
            cell.fill = header_fill
        for provenance, text in report.provenance_notes:
            notes.append([provenance, text])
        notes.column_dimensions["A"].width = 18
        notes.column_dimensions["B"].width = 100

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def to_pdf(report: Report) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), title=report.title,
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7.5, leading=9.5)
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=8.5)

    story: list = [Paragraph(report.title, styles["Title"])]
    if report.subtitle:
        story.append(Paragraph(report.subtitle, styles["Heading3"]))
    story.append(Paragraph(
        f"Generated {report.generated_at.strftime('%d %b %Y, %H:%M')}", small))
    story.append(Spacer(1, 6))

    if report.meta:
        meta_rows = [[Paragraph(f"<b>{k}</b>", cell), Paragraph(str(v), cell)]
                     for k, v in report.meta.items()]
        meta_table = Table(meta_rows, colWidths=[45 * mm, 110 * mm])
        meta_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.extend([meta_table, Spacer(1, 8)])

    for table in report.tables:
        story.append(Paragraph(table.title, styles["Heading3"]))
        data = [[Paragraph(f"<b>{c}</b>", cell) for c in table.columns]]
        for row in table.rows:
            data.append([Paragraph(str(v), cell) for v in row])
        if table.totals:
            data.append([Paragraph(f"<b>{v}</b>", cell) for v in table.totals])

        if len(data) == 1:
            story.append(Paragraph("No records.", small))
            story.append(Spacer(1, 8))
            continue

        available = doc.width
        widths = [available / len(table.columns)] * len(table.columns)
        pdf_table = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFC9D4")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if table.totals:
            style.append(("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1),
                          colors.HexColor("#E3E9F2")))
        pdf_table.setStyle(TableStyle(style))
        story.append(pdf_table)

        if table.note:
            story.append(Spacer(1, 3))
            story.append(Paragraph(f"<i>{table.note}</i>", small))
        story.append(Spacer(1, 10))

    if report.provenance_notes:
        story.append(PageBreak())
        story.append(Paragraph("How to read this report", styles["Heading2"]))
        story.append(Paragraph(
            "Each statement below is labelled with its standing, so it is clear what is "
            "evidenced, what is official, what the system has inferred, and what is "
            "not yet verified.", small))
        story.append(Spacer(1, 6))
        for provenance, text in report.provenance_notes:
            story.append(Paragraph(f"<b>{provenance}:</b> {text}", small))
            story.append(Spacer(1, 4))

    doc.build(story)
    return buffer.getvalue()


EXPORTERS = {
    "csv": (to_csv, "text/csv", "csv"),
    "excel": (to_excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pdf": (to_pdf, "application/pdf", "pdf"),
}


def export(report: Report, fmt: str) -> tuple[bytes, str, str]:
    """Render a report. Returns ``(content, mime_type, extension)``."""
    if fmt not in EXPORTERS:
        raise ValueError(f"Unsupported export format: {fmt}")
    builder, mime, extension = EXPORTERS[fmt]
    return builder(report), mime, extension


def record_generation(db: Session, *, organization_id: int, report: Report, report_type: str,
                      fmt: str, user: Optional[User] = None,
                      employee_id: Optional[int] = None,
                      filters: Optional[dict] = None) -> GeneratedReport:
    """Log the export with the totals it carried (spec s96)."""
    row = GeneratedReport(
        organization_id=organization_id,
        employee_id=employee_id,
        report_type=report_type,
        period_label=report.subtitle[:60] if report.subtitle else None,
        fmt=fmt,
        filters_json=filters or {},
        totals_json=report.totals,
        generated_by_id=user.id if user else None,
    )
    db.add(row)
    db.flush()
    return row
