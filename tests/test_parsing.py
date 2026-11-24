"""Document and statement parsing (spec s3, s6, s23, s74, s75)."""

from __future__ import annotations

import io
from datetime import date

import pytest

from app.money import format_money
from app.services.parsers.statement import (
    ParsedRow,
    StatementParseError,
    parse_csv,
    parse_excel,
    parse_pdf,
    parse_statement,
)
from app.services.textutils import (
    extract_counterparty,
    extract_month_references,
    extract_utr,
    is_ifsc,
    name_similarity,
    normalize_name,
    parse_date,
    repair_numeric_ocr,
)

CSV_STATEMENT = """State Bank of India
Statement of Account
Account Name : SUNITA DEVI
Account Number : XXXXXX4321

Txn Date,Value Date,Description,Ref No.,Debit,Credit,Balance
01/03/2026,01/03/2026,OPENING BALANCE,,,,"1,240.00"
18/04/2026,18/04/2026,NEFT-SBIN0001234-ICDS UP-MARCH HONORARIUM-UTRN123456789012,UTRN123456789012,,"10,000.00","11,240.00"
20/04/2026,20/04/2026,ATM WDL LUCKNOW,,"2,000.00",,"9,240.00"
05/05/2026,05/05/2026,NEFT GOVT CREDIT,REF9988,,"1,23,456.78","1,32,696.78"
06/05/2026,,ROW WITH NO AMOUNT,,,,
notadate,,GARBAGE ROW,,,,
Total,,,,"2,000.00","1,33,456.78",
"""


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def test_csv_statement_parsed_with_metadata():
    result = parse_csv(CSV_STATEMENT.encode())

    assert result.bank_name == "State Bank of India"
    assert result.account_last4 == "4321"
    assert result.account_holder == "SUNITA DEVI"
    assert len(result.rows) == 3


def test_csv_amounts_and_direction():
    rows = parse_csv(CSV_STATEMENT.encode()).rows
    by_date = {r.txn_date: r for r in rows}

    credit = by_date[date(2026, 4, 18)]
    assert credit.direction == "CREDIT"
    assert credit.amount_paise == 1000000
    assert credit.utr == "UTRN123456789012"
    assert credit.balance_paise == 1124000

    debit = by_date[date(2026, 4, 20)]
    assert debit.direction == "DEBIT"
    assert debit.amount_paise == 200000

    lakh = by_date[date(2026, 5, 5)]
    assert format_money(lakh.amount_paise) == "₹1,23,456.78"


def test_noise_rows_skipped_but_errors_kept():
    """Spec s75: nothing is silently discarded."""
    result = parse_csv(CSV_STATEMENT.encode())

    descriptions = " ".join(r.description for r in result.rows)
    assert "OPENING BALANCE" not in descriptions
    assert "Total" not in descriptions

    errors = " ".join(e.raw for e in result.errors)
    assert "GARBAGE ROW" in errors
    assert "ROW WITH NO AMOUNT" in errors
    assert all(e.error for e in result.errors), "each error must explain itself"


def test_csv_without_a_table_raises_a_clear_error():
    with pytest.raises(StatementParseError) as exc:
        parse_csv(b"just some text\nwith no table at all\n")
    assert "date column" in str(exc.value)


def test_semicolon_delimiter_detected():
    data = "Date;Description;Credit;Balance\n01/02/2026;SALARY;1000.00;1000.00\n"
    result = parse_csv(data.encode())
    assert len(result.rows) == 1
    assert result.rows[0].amount_paise == 100000


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def test_excel_with_signed_amount_and_type_column():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in [["HDFC Bank"], ["Account No : 998877661234"], [],
                ["Date", "Narration", "Type", "Amount", "Closing Balance"],
                ["07/03/2026", "SALARY MAR 2026", "CR", "12,000.00", "15,000.00"],
                ["09/03/2026", "UPI SHOP", "DR", "500.00", "14,500.00"]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    result = parse_excel(buf.getvalue())
    assert result.bank_name == "HDFC Bank"
    assert result.account_last4 == "1234"
    assert [r.direction for r in result.rows] == ["CREDIT", "DEBIT"]
    assert result.rows[0].amount_paise == 1200000


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _build_pdf() -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    data = [["Txn Date", "Description", "Debit", "Credit", "Balance"],
            ["01/03/2026", "OPENING BALANCE", "", "", "1,240.00"],
            ["18/04/2026", "NEFT ICDS MARCH HONORARIUM UTRN12345678", "", "10,000.00", "11,240.00"],
            ["20/04/2026", "ATM WITHDRAWAL", "2,000.00", "", "9,240.00"]]
    table = Table(data, colWidths=[60, 200, 60, 60, 60])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("FONTSIZE", (0, 0), (-1, -1), 7)]))
    doc.build([Paragraph("Punjab National Bank", styles["Title"]),
               Paragraph("Account Number : XXXXXX4321", styles["Normal"]),
               Spacer(1, 10), table])
    return buf.getvalue()


def test_pdf_statement_parsed():
    result = parse_pdf(_build_pdf())

    assert result.bank_name == "Punjab National Bank"
    assert result.account_last4 == "4321"
    assert len(result.rows) == 2

    credit = next(r for r in result.rows if r.direction == "CREDIT")
    assert credit.amount_paise == 1000000
    assert credit.txn_date == date(2026, 4, 18)


def test_dispatch_by_extension():
    assert parse_statement("x.csv", CSV_STATEMENT.encode()).detected_format == "CSV"
    assert parse_statement("x.pdf", _build_pdf()).detected_format == "PDF"
    with pytest.raises(StatementParseError):
        parse_statement("x.docx", b"whatever")


# ---------------------------------------------------------------------------
# Dates (spec s73)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("18/04/2026", date(2026, 4, 18)),
    ("18-04-2026", date(2026, 4, 18)),
    ("2026-04-18", date(2026, 4, 18)),
    ("18-Apr-2026", date(2026, 4, 18)),
    ("18 April 2026", date(2026, 4, 18)),
    ("1.4.26", date(2026, 4, 1)),
    ("04/18/2026", date(2026, 4, 18)),   # unambiguous: 18 cannot be a month
])
def test_date_formats(text, expected):
    assert parse_date(text) == expected


def test_impossible_date_returns_none():
    assert parse_date("31/02/2026") is None
    assert parse_date("nonsense") is None
    assert parse_date("") is None


def test_day_first_is_the_default():
    """Indian statements are day-first: 03/04 is 3 April, not 4 March."""
    assert parse_date("03/04/2026") == date(2026, 4, 3)


# ---------------------------------------------------------------------------
# Narration mining (spec s8)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("narration,month,year", [
    ("NEFT MARCH HONORARIUM 2026", 3, 2026),
    ("SAL MAR26 CR", 3, 2026),
    ("SALARY MAR-26", 3, 2026),
    ("salary 03/2026", 3, 2026),
    ("मार्च वेतन 2026", 3, 2026),
])
def test_salary_month_extracted_from_narration(narration, month, year):
    refs = extract_month_references(narration)
    assert refs, f"no month found in {narration!r}"
    assert (refs[0][0], refs[0][1]) == (month, year)


def test_no_month_reference_returns_empty():
    assert extract_month_references("NEFT GOVT 10000") == []


def test_multiple_months_detected():
    refs = extract_month_references("ARREAR JAN FEB MAR 2026")
    assert [r[0] for r in refs] == [1, 2, 3]


def test_utr_extraction_excludes_ifsc_and_words():
    assert extract_utr("NEFT-SBIN0001234-ICDS-UTRN123456789012") == "UTRN123456789012"
    assert extract_utr("UTR: HDFC123456789012") == "HDFC123456789012"
    assert extract_utr("NEFT SBIN0001234") is None, "an IFSC is not a UTR"
    assert extract_utr("ATM WITHDRAWAL") is None, "a plain word is not a UTR"
    assert is_ifsc("SBIN0001234") is True


def test_counterparty_extraction():
    party = extract_counterparty("NEFT-SBIN0001234-DIRECTORATE OF ICDS UP-UTRN123")
    assert party == "DIRECTORATE OF ICDS UP"


# ---------------------------------------------------------------------------
# Name matching (spec s23) - cautious by design
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,minimum", [
    ("Raj Kumar", "RAJ KUMAR", 0.99),
    ("Raj Kumar", "Rajkumar", 0.95),
    ("Raj Kumar", "R. Kumar", 0.80),
    ("Smt. Sunita Devi", "Sunita Devi", 0.95),
])
def test_similar_names_score_high(a, b, minimum):
    assert name_similarity(a, b) >= minimum


@pytest.mark.parametrize("a,b", [
    ("Raj Kumar", "Sunita Devi"),
    ("Anil Singh", "Sunil Yadav"),
])
def test_different_names_score_low(a, b):
    assert name_similarity(a, b) < 0.6


def test_similar_but_distinct_names_do_not_reach_certainty():
    """Raj Kumar and Raj Kumari are different people."""
    assert name_similarity("Raj Kumar", "Raj Kumari") < 1.0


def test_name_normalisation():
    assert normalize_name("  Dr. Raj   Kumar!  ") == "DR RAJ KUMAR"
    assert normalize_name(None) == ""


# ---------------------------------------------------------------------------
# OCR repair (spec s94)
# ---------------------------------------------------------------------------
def test_ocr_digit_repair_only_when_result_is_numeric():
    assert repair_numeric_ocr("1O,50O.OO") == "10,500.00"
    assert repair_numeric_ocr("l2000") == "12000"
    # Real words must never be mangled into digits.
    assert repair_numeric_ocr("MARCH") == "MARCH"
    assert repair_numeric_ocr("SBIN0001234") == "SBIN0001234"


# ---------------------------------------------------------------------------
# Document extraction (spec s3, s43)
# ---------------------------------------------------------------------------
def test_pdf_text_extraction_and_field_mining():
    from app.agents.document_agent import extract_text, mine_fields

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    doc.build([
        Paragraph("Sanction Order", styles["Title"]),
        Paragraph("Sanction No : ICDS/LKO/2026/0012", styles["Normal"]),
        Spacer(1, 6),
        Paragraph("Dated : 28/03/2026", styles["Normal"]),
        Spacer(1, 6),
        Paragraph("Net Amount : Rs. 10,500.00", styles["Normal"]),
    ])

    extraction = extract_text(buf.getvalue(), "application/pdf")
    assert extraction.status == "COMPLETED"
    assert "ICDS/LKO/2026/0012" in extraction.text

    fields, confidence = mine_fields(extraction.text)
    assert fields.get("sanction_number") == "ICDS/LKO/2026/0012"
    assert fields.get("net_amount") == 1050000
    assert 0 < confidence <= 0.9, "extraction confidence is capped: it is not proof"


def test_scanned_pdf_reports_honestly_rather_than_faking_ocr():
    """Spec s108: never pretend an extraction happened."""
    from app.agents.document_agent import extract_text

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(100, 100, 200, 200, fill=0)   # a page with shapes but no text
    c.showPage()
    c.save()

    extraction = extract_text(buf.getvalue(), "application/pdf")
    assert extraction.status in {"UNAVAILABLE", "PARTIAL"}
    assert extraction.guidance, "the user must be told what to do instead"
    assert not extraction.fields


def test_image_without_ocr_engine_is_reported_not_invented():
    from app.agents.document_agent import extract_text

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 30), "white").save(buf, format="PNG")

    extraction = extract_text(buf.getvalue(), "image/png")
    # Either OCR ran, or it honestly reported being unavailable -- never faked.
    assert extraction.status in {"COMPLETED", "PARTIAL", "UNAVAILABLE", "FAILED"}
    if extraction.status == "UNAVAILABLE":
        assert "not available" in (extraction.guidance or "").lower()
        assert extraction.text == ""


def test_document_category_suggestion():
    from app.agents.document_agent import suggest_category

    assert suggest_category("sbi_statement.pdf", "Statement of Account") == "BANK_STATEMENT"
    assert suggest_category("go_1147.pdf", "Government Order") == "GOVERNMENT_ORDER"
    assert suggest_category("random.pdf", "hello") == "OTHER"
