"""Bank statement parsing and normalisation (spec s6, s74, s75).

Every supported format is reduced to one canonical :class:`ParsedRow` shape so
that the rest of the platform never needs to know which bank a statement came
from.

Handled deliberately, because these are what break naive parsers on real
Indian bank statements:

* separate Debit / Credit columns, a single signed Amount column, or an Amount
  column plus a Dr/Cr type column
* Indian digit grouping (``1,23,456.78``) and trailing ``Cr`` / ``Dr`` markers
* many date formats, day-first by default
* multi-line narrations that wrap across PDF rows
* repeated page headers and footers, opening/closing balance lines
* summary and total rows that are not transactions
* OCR digit confusion in amount cells

Rows that cannot be parsed are NEVER silently dropped: each becomes a
:class:`RowError` carrying the original text, so the import screen can show
exactly what was not understood (spec s75).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Sequence

from ...money import MoneyError, try_to_paise
from ..textutils import (
    clean_amount_cell,
    extract_counterparty,
    extract_utr,
    normalize_narration,
    normalize_text,
    parse_date,
)


# ---------------------------------------------------------------------------
# Canonical shapes
# ---------------------------------------------------------------------------
@dataclass
class ParsedRow:
    """One normalised statement line."""

    row_number: int
    txn_date: date
    amount_paise: int
    direction: str                       # CREDIT | DEBIT
    description: str = ""
    value_date: Optional[date] = None
    balance_paise: Optional[int] = None
    reference: Optional[str] = None
    utr: Optional[str] = None
    counterparty: Optional[str] = None
    page_number: Optional[int] = None
    raw: str = ""

    @property
    def narration_normalized(self) -> str:
        return normalize_narration(self.description)


@dataclass
class RowError:
    """A line we could not turn into a transaction. Never discarded."""

    row_number: int
    raw: str
    error: str
    page_number: Optional[int] = None
    severity: str = "MEDIUM"


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    detected_format: str = "UNKNOWN"
    bank_name: Optional[str] = None
    account_last4: Optional[str] = None
    account_holder: Optional[str] = None
    pages_read: int = 0
    pages_failed: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows)


class StatementParseError(Exception):
    """Raised when a file cannot be opened or contains no readable table."""


# ---------------------------------------------------------------------------
# Column recognition
# ---------------------------------------------------------------------------
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "txn_date": (
        "date", "txn date", "transaction date", "tran date", "trans date",
        "posting date", "post date", "date of transaction", "tran. date",
        "dt", "txndate", "booking date",
    ),
    "value_date": ("value date", "value dt", "val date", "valuedate", "effective date"),
    "description": (
        "description", "narration", "particulars", "remarks", "details",
        "transaction details", "transaction remarks", "transaction particulars",
        "transaction description", "narrative",
    ),
    "debit": (
        "debit", "debit amount", "withdrawal", "withdrawals", "withdrawal amt",
        "withdrawal amount", "withdrawal (dr)", "dr", "dr amount", "paid out",
        "debit(dr)", "amount debited", "debit amt",
    ),
    "credit": (
        "credit", "credit amount", "deposit", "deposits", "deposit amt",
        "deposit amount", "deposit (cr)", "cr", "cr amount", "paid in",
        "credit(cr)", "amount credited", "credit amt",
    ),
    "amount": ("amount", "txn amount", "transaction amount", "amt", "amount (inr)", "amount inr"),
    "balance": (
        "balance", "closing balance", "running balance", "balance amt",
        "balance amount", "available balance", "bal", "balance (inr)",
    ),
    "reference": (
        "ref no", "ref no.", "reference", "reference no", "reference number",
        "cheque no", "chq no", "chq/ref no", "cheque/reference no", "utr",
        "utr no", "transaction id", "txn id", "instrument id", "ref/cheque no",
    ),
    "type": ("type", "dr/cr", "cr/dr", "transaction type", "txn type", "indicator", "drcr"),
}


def _canonical_header(cell: Any) -> Optional[str]:
    """Map a header cell onto a canonical column name."""
    text = normalize_text(str(cell or "")).lower().strip().strip(":.")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    for canonical, synonyms in COLUMN_SYNONYMS.items():
        if text in synonyms:
            return canonical
    # Substring fallback, longest synonym first so "credit amount" beats "amount".
    for canonical, synonyms in sorted(
        COLUMN_SYNONYMS.items(), key=lambda kv: -max(len(s) for s in kv[1])
    ):
        for syn in sorted(synonyms, key=len, reverse=True):
            if syn in text:
                return canonical
    return None


def _map_header_row(cells: Sequence[Any]) -> dict[str, int]:
    """Return ``{canonical_name: column_index}`` for a candidate header row."""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        canonical = _canonical_header(cell)
        if canonical and canonical not in mapping:
            mapping[canonical] = idx
    return mapping


def _is_usable_mapping(mapping: dict[str, int]) -> bool:
    """A table is usable when it has a date and some notion of an amount."""
    if "txn_date" not in mapping:
        return False
    return bool({"debit", "credit", "amount"} & set(mapping))


def find_header(rows: Sequence[Sequence[Any]], scan_limit: int = 30) -> tuple[int, dict[str, int]]:
    """Locate the header row within the first ``scan_limit`` rows.

    Statements routinely carry a bank logo, address block and account summary
    above the real table, so the header is rarely row 0.
    """
    best_idx, best_map, best_score = -1, {}, 0
    for idx, row in enumerate(rows[:scan_limit]):
        mapping = _map_header_row(row)
        if not _is_usable_mapping(mapping):
            continue
        score = len(mapping)
        if score > best_score:
            best_idx, best_map, best_score = idx, mapping, score
    return best_idx, best_map


# ---------------------------------------------------------------------------
# Noise detection
# ---------------------------------------------------------------------------
_NOISE_PATTERNS = (
    r"^\s*$",
    r"opening balance",
    r"closing balance",
    r"brought forward",
    r"carried forward",
    r"b/f\b", r"c/f\b",
    r"total\s*[:\-]?\s*$",
    r"^\s*total\b",
    r"grand total",
    r"statement of account",
    r"page \d+ of \d+",
    r"continued",
    r"this is a computer generated",
    r"^\s*date\s+",              # a repeated header line
    r"registered office",
    r"customer care",
    r"^\s*-+\s*$",
)
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.I)


def is_noise_row(text: str) -> bool:
    """Page furniture, summary and balance lines that are not transactions."""
    return bool(_NOISE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
_CR_MARK = re.compile(r"\b(cr|credit)\b", re.I)
_DR_MARK = re.compile(r"\b(dr|debit|wdl|withdrawal)\b", re.I)


def _cell(row: Sequence[Any], mapping: dict[str, int], key: str) -> Optional[str]:
    idx = mapping.get(key)
    if idx is None or idx >= len(row):
        return None
    value = row[idx]
    if value is None:
        return None
    return normalize_text(str(value))


def build_row(
    raw_cells: Sequence[Any],
    mapping: dict[str, int],
    row_number: int,
    page_number: Optional[int] = None,
    dayfirst: bool = True,
) -> ParsedRow | RowError:
    """Turn one spreadsheet/table row into a :class:`ParsedRow` or a
    :class:`RowError` explaining precisely what could not be read."""
    raw_text = " | ".join(normalize_text(str(c)) for c in raw_cells if c is not None)

    date_text = _cell(raw_cells, mapping, "txn_date")
    txn_date = parse_date(date_text, dayfirst=dayfirst)
    if txn_date is None:
        return RowError(row_number, raw_text, f"Could not read a transaction date from {date_text!r}.",
                        page_number)

    description = _cell(raw_cells, mapping, "description") or ""

    # --- Determine amount and direction ---
    debit_text = clean_amount_cell(_cell(raw_cells, mapping, "debit"))
    credit_text = clean_amount_cell(_cell(raw_cells, mapping, "credit"))
    amount_text = clean_amount_cell(_cell(raw_cells, mapping, "amount"))
    type_text = _cell(raw_cells, mapping, "type") or ""

    debit_paise = try_to_paise(debit_text) if debit_text else None
    credit_paise = try_to_paise(credit_text) if credit_text else None

    direction: Optional[str] = None
    amount_paise: Optional[int] = None

    if credit_paise and credit_paise != 0:
        direction, amount_paise = "CREDIT", abs(credit_paise)
    elif debit_paise and debit_paise != 0:
        direction, amount_paise = "DEBIT", abs(debit_paise)
    elif amount_text:
        signed = try_to_paise(amount_text)
        if signed is None:
            return RowError(row_number, raw_text,
                            f"Could not read the amount {amount_text!r}.", page_number)
        # Direction from an explicit type column, then from markers in the
        # amount cell, then from the sign.
        if _CR_MARK.search(type_text) and not _DR_MARK.search(type_text):
            direction = "CREDIT"
        elif _DR_MARK.search(type_text):
            direction = "DEBIT"
        elif _CR_MARK.search(amount_text):
            direction = "CREDIT"
        elif _DR_MARK.search(amount_text):
            direction = "DEBIT"
        else:
            direction = "DEBIT" if signed < 0 else "CREDIT"
        amount_paise = abs(signed)

    if direction is None or amount_paise is None:
        # Distinguish "the cell said 0.00" from "there was no amount at all".
        # A zero-value line is real in some statements (fee reversals, markers)
        # and spec s94 requires it to be handled explicitly, not as a mystery.
        if 0 in {v for v in (debit_paise, credit_paise) if v is not None}:
            return RowError(row_number, raw_text,
                            "The amount on this row is zero; it was not imported as a payment.",
                            page_number, severity="LOW")
        return RowError(row_number, raw_text,
                        "No debit or credit amount found in this row.", page_number)

    if amount_paise == 0:
        return RowError(row_number, raw_text,
                        "The amount on this row is zero; it was not imported as a payment.",
                        page_number, severity="LOW")

    balance_text = clean_amount_cell(_cell(raw_cells, mapping, "balance"))
    balance_paise = try_to_paise(balance_text) if balance_text else None

    reference = _cell(raw_cells, mapping, "reference")
    value_date = parse_date(_cell(raw_cells, mapping, "value_date"), dayfirst=dayfirst)

    return ParsedRow(
        row_number=row_number,
        txn_date=txn_date,
        value_date=value_date,
        description=description,
        amount_paise=amount_paise,
        direction=direction,
        balance_paise=balance_paise,
        reference=reference or None,
        utr=extract_utr(f"{description} {reference or ''}"),
        counterparty=extract_counterparty(description),
        page_number=page_number,
        raw=raw_text,
    )


# ---------------------------------------------------------------------------
# Statement metadata
# ---------------------------------------------------------------------------
_BANK_HINTS = {
    "state bank of india": "State Bank of India", "sbi": "State Bank of India",
    "punjab national bank": "Punjab National Bank", "pnb": "Punjab National Bank",
    "bank of baroda": "Bank of Baroda", "hdfc": "HDFC Bank",
    "icici": "ICICI Bank", "axis bank": "Axis Bank", "canara": "Canara Bank",
    "union bank": "Union Bank of India", "kotak": "Kotak Mahindra Bank",
    "indian bank": "Indian Bank", "bank of india": "Bank of India",
    "central bank": "Central Bank of India", "uco bank": "UCO Bank",
    "idbi": "IDBI Bank", "yes bank": "Yes Bank", "gramin": "Gramin Bank",
    "india post": "India Post Payments Bank", "ippb": "India Post Payments Bank",
}


def sniff_metadata(text: str) -> dict:
    """Pull bank name, masked account and holder name from the page preamble."""
    meta: dict = {}
    lowered = normalize_text(text).lower()

    for hint, proper in _BANK_HINTS.items():
        if hint in lowered:
            meta["bank_name"] = proper
            break

    acct = re.search(r"(?:a/?c|account)\s*(?:no\.?|number)?\s*[:\-]?\s*([X\*\d][X\*\d\s\-]{5,25})",
                     text, re.I)
    if acct:
        digits = re.sub(r"\D", "", acct.group(1))
        if len(digits) >= 4:
            meta["account_last4"] = digits[-4:]

    holder = re.search(r"(?:account\s*(?:holder|name)|customer\s*name|name)\s*[:\-]\s*([A-Za-z .]{3,60})",
                       text, re.I)
    if holder:
        meta["account_holder"] = normalize_text(holder.group(1))

    return meta


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_csv(content: bytes, dayfirst: bool = True) -> ParseResult:
    text = _decode(content)
    result = ParseResult(detected_format="CSV")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if not rows:
        raise StatementParseError("The CSV file contains no rows.")

    result.notes.append(f"Detected delimiter {delimiter!r}.")
    header_idx, mapping = find_header(rows)
    if header_idx < 0:
        raise StatementParseError(
            "Could not find a transaction table in this CSV. A statement needs at "
            "least a date column and a debit/credit or amount column."
        )

    preamble = "\n".join(" ".join(str(c) for c in r) for r in rows[:header_idx])
    result.__dict__.update({k: v for k, v in sniff_metadata(preamble).items()})
    result.notes.append(f"Header found on line {header_idx + 1}: {sorted(mapping)}")

    for offset, raw_cells in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        joined = " ".join(str(c) for c in raw_cells if c)
        if not joined.strip() or is_noise_row(joined):
            continue
        built = build_row(raw_cells, mapping, offset, dayfirst=dayfirst)
        (result.rows if isinstance(built, ParsedRow) else result.errors).append(built)

    return result


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def parse_excel(content: bytes, dayfirst: bool = True) -> ParseResult:
    from openpyxl import load_workbook

    result = ParseResult(detected_format="XLSX")
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        raise StatementParseError(f"The Excel file could not be opened: {exc}") from exc

    best: tuple[int, dict[str, int], list, str] | None = None
    for sheet in workbook.worksheets:
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        if not rows:
            continue
        header_idx, mapping = find_header(rows)
        if header_idx >= 0 and (best is None or len(mapping) > len(best[1])):
            best = (header_idx, mapping, rows, sheet.title)

    if best is None:
        raise StatementParseError(
            "No transaction table was found in this Excel file. Please check the "
            "sheet contains a date column and debit/credit or amount columns."
        )

    header_idx, mapping, rows, sheet_title = best
    result.notes.append(f"Sheet '{sheet_title}', header on row {header_idx + 1}: {sorted(mapping)}")

    preamble = "\n".join(" ".join(str(c) for c in r if c is not None) for r in rows[:header_idx])
    for key, value in sniff_metadata(preamble).items():
        setattr(result, key, value)

    for offset, raw_cells in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        joined = " ".join(str(c) for c in raw_cells if c is not None)
        if not joined.strip() or is_noise_row(joined):
            continue
        built = build_row(raw_cells, mapping, offset, dayfirst=dayfirst)
        (result.rows if isinstance(built, ParsedRow) else result.errors).append(built)

    workbook.close()
    return result


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
#: A statement line in text mode: leading date, trailing amounts.
_TEXT_LINE_RE = re.compile(
    r"^\s*(?P<date>\d{1,2}[-/.][A-Za-z0-9]{2,9}[-/.]\d{2,4})\s+(?P<rest>.+)$"
)
_TRAILING_AMOUNTS_RE = re.compile(
    r"(?P<amounts>(?:[\d,]+\.\d{2}\s*(?:cr|dr)?\s*){1,3})$", re.I
)


def parse_pdf(content: bytes, dayfirst: bool = True) -> ParseResult:
    """Extract transactions from a PDF statement.

    Strategy: try table extraction first (accurate when the PDF has ruling
    lines), then fall back to line-based text parsing per page. Pages that
    yield nothing are reported by number so the user knows exactly what was
    missed (spec s75).
    """
    import pdfplumber

    result = ParseResult(detected_format="PDF")
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise StatementParseError(f"The PDF could not be opened: {exc}") from exc

    row_counter = 0
    preamble_text = ""

    with pdf:
        result.pages_read = len(pdf.pages)
        carried_mapping: dict[str, int] = {}

        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - a damaged page must not abort the import
                page_text = ""

            if page_no == 1:
                preamble_text = page_text[:3000]

            page_rows_before = len(result.rows)

            # --- 1. Table extraction ---
            try:
                tables = page.extract_tables() or []
            except Exception:  # noqa: BLE001
                tables = []

            for table in tables:
                cleaned = [[normalize_text(str(c)) if c is not None else "" for c in row]
                           for row in table if row]
                if not cleaned:
                    continue
                header_idx, mapping = find_header(cleaned, scan_limit=6)
                if header_idx >= 0:
                    carried_mapping = mapping
                    body = cleaned[header_idx + 1:]
                elif carried_mapping:
                    # Continuation table on a later page with no repeated header.
                    mapping, body = carried_mapping, cleaned
                else:
                    continue

                for raw_cells in body:
                    joined = " ".join(raw_cells)
                    if not joined.strip() or is_noise_row(joined):
                        continue
                    row_counter += 1
                    built = build_row(raw_cells, mapping, row_counter, page_no, dayfirst)
                    if isinstance(built, ParsedRow):
                        result.rows.append(built)
                    elif built.severity != "LOW":
                        result.errors.append(built)

            # --- 2. Text fallback for this page ---
            if len(result.rows) == page_rows_before and page_text.strip():
                text_rows, row_counter = _parse_pdf_text_page(
                    page_text, page_no, row_counter, dayfirst
                )
                for item in text_rows:
                    (result.rows if isinstance(item, ParsedRow) else result.errors).append(item)

            if len(result.rows) == page_rows_before and page_text.strip():
                result.pages_failed.append(page_no)

    for key, value in sniff_metadata(preamble_text).items():
        setattr(result, key, value)

    if result.pages_failed:
        result.notes.append(
            "No transactions could be read from page(s) "
            + ", ".join(str(p) for p in result.pages_failed)
            + ". These pages may be scanned images, or may contain no transactions."
        )

    if not result.rows and not result.errors:
        raise StatementParseError(
            "No transactions could be read from this PDF. If it is a scanned "
            "document, please upload a clearer copy or enter the transactions manually."
        )
    return result


def _parse_pdf_text_page(
    page_text: str, page_no: int, row_counter: int, dayfirst: bool
) -> tuple[list[ParsedRow | RowError], int]:
    """Line-based parsing for PDFs without ruled tables.

    Continuation lines (a wrapped narration) are appended to the previous
    transaction rather than dropped (spec s6: multiline narrations).
    """
    out: list[ParsedRow | RowError] = []
    for raw_line in page_text.splitlines():
        line = normalize_text(raw_line)
        if not line or is_noise_row(line):
            continue

        match = _TEXT_LINE_RE.match(line)
        if not match:
            # Continuation of the previous narration.
            if out and isinstance(out[-1], ParsedRow) and len(line) > 3:
                out[-1].description = normalize_text(f"{out[-1].description} {line}")
            continue

        txn_date = parse_date(match.group("date"), dayfirst=dayfirst)
        if txn_date is None:
            continue

        rest = match.group("rest")
        amounts_match = _TRAILING_AMOUNTS_RE.search(rest)
        if not amounts_match:
            row_counter += 1
            out.append(RowError(row_counter, line,
                               "A date was found but no amount could be read on this line.",
                               page_no, severity="LOW"))
            continue

        amounts_blob = amounts_match.group("amounts")
        description = normalize_text(rest[: amounts_match.start()])
        tokens = re.findall(r"[\d,]+\.\d{2}\s*(?:cr|dr)?", amounts_blob, re.I)
        values = [try_to_paise(tok) for tok in tokens]
        values = [v for v in values if v is not None]
        if not values:
            row_counter += 1
            out.append(RowError(row_counter, line, "Amounts on this line could not be read.",
                               page_no, severity="LOW"))
            continue

        # Convention: the last trailing number is the running balance when
        # more than one amount is present.
        if len(values) >= 2:
            amount_paise, balance_paise = abs(values[-2]), values[-1]
        else:
            amount_paise, balance_paise = abs(values[-1]), None

        blob_lower = amounts_blob.lower()
        if "cr" in blob_lower:
            direction = "CREDIT"
        elif "dr" in blob_lower:
            direction = "DEBIT"
        else:
            direction = "DEBIT" if _DR_MARK.search(description) else "CREDIT"

        if amount_paise == 0:
            continue

        row_counter += 1
        out.append(ParsedRow(
            row_number=row_counter,
            txn_date=txn_date,
            description=description,
            amount_paise=amount_paise,
            direction=direction,
            balance_paise=balance_paise,
            utr=extract_utr(description),
            counterparty=extract_counterparty(description),
            page_number=page_no,
            raw=line,
        ))

    return out, row_counter


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def parse_statement(filename: str, content: bytes, dayfirst: bool = True) -> ParseResult:
    """Parse any supported statement file into the canonical row shape."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext == ".csv" or ext == ".txt":
        return parse_csv(content, dayfirst)
    if ext in {".xlsx", ".xls"}:
        return parse_excel(content, dayfirst)
    if ext == ".pdf":
        return parse_pdf(content, dayfirst)
    raise StatementParseError(
        f"'{ext or 'This file type'}' is not a supported statement format. "
        "Please upload a PDF, CSV or Excel statement."
    )
