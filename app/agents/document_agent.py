"""AGENT 1 - Document Intelligence (spec s3, s41, s42, s43).

Stores evidence immutably, extracts text, and mines structured fields from it.

Honesty about OCR (spec s108): this host may or may not have an OCR engine.
Rather than pretend, the agent reports ``UNAVAILABLE`` and tells the user
exactly what to do instead (upload a text PDF, or enter the values manually).
No fake extraction is ever produced.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..config import settings
from ..db import utcnow
from ..enums import DocumentCategory, OcrStatus, confidence_band
from ..models import Document, EvidenceLink, Organization, User
from ..money import try_to_paise
from ..security import safe_filename, sha256_bytes
from ..services.textutils import (
    extract_month_references,
    extract_utr,
    normalize_text,
    parse_date,
)

AGENT_NAME = "document_agent"

IMAGE_TYPES = {"image/png", "image/jpeg", "image/tiff", "image/webp"}


@dataclass
class Extraction:
    text: str = ""
    confidence: float = 0.0
    fields: dict = field(default_factory=dict)
    status: str = OcrStatus.PENDING.value
    error: Optional[str] = None
    page_count: Optional[int] = None
    guidance: Optional[str] = None


# ---------------------------------------------------------------------------
# Storage - originals are never destroyed (spec s42)
# ---------------------------------------------------------------------------
def store(
    db: Session,
    *,
    organization: Organization,
    content: bytes,
    filename: str,
    title: str,
    category: str,
    mime_type: str,
    uploaded_by: Optional[User] = None,
    employee_id: Optional[int] = None,
    supersedes: Optional[Document] = None,
    replacement_reason: Optional[str] = None,
    notes: Optional[str] = None,
) -> Document:
    """Persist a document to the evidence vault.

    Replacing a document creates a NEW version; the previous file stays on
    disk and in the database with ``is_current = False``.
    """
    digest = sha256_bytes(content)
    clean_name = safe_filename(filename)

    folder = settings.storage_dir / str(organization.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:16]}_{clean_name}"
    if not path.exists():
        path.write_bytes(content)

    version = 1
    if supersedes is not None:
        version = supersedes.version + 1

    document = Document(
        organization_id=organization.id,
        employee_id=employee_id,
        category=category,
        title=title or clean_name,
        original_filename=clean_name,
        stored_path=str(path),
        mime_type=mime_type,
        size_bytes=len(content),
        sha256=digest,
        version=version,
        supersedes_id=supersedes.id if supersedes else None,
        is_current=True,
        replacement_reason=replacement_reason,
        ocr_status=OcrStatus.PENDING.value,
        uploaded_by_id=uploaded_by.id if uploaded_by else None,
        notes=notes,
    )
    db.add(document)
    db.flush()

    if supersedes is not None:
        supersedes.is_current = False
        audit_record(
            db,
            action="REPLACE_DOCUMENT",
            entity_type="Document",
            entity_id=supersedes.id,
            entity_uid=supersedes.uid,
            organization_id=organization.id,
            actor=uploaded_by,
            old_value={"version": supersedes.version, "is_current": True},
            new_value={"superseded_by": document.uid, "version": document.version},
            reason=replacement_reason or "Document replaced with a newer version",
            source=AuditSource.HUMAN,
        )

    audit_record(
        db,
        action="UPLOAD_DOCUMENT",
        entity_type="Document",
        entity_id=document.id,
        entity_uid=document.uid,
        organization_id=organization.id,
        actor=uploaded_by,
        new_value={"title": document.title, "category": category,
                   "sha256": digest, "size_bytes": len(content)},
        reason="Evidence uploaded",
        source=AuditSource.HUMAN if uploaded_by else AuditSource.SYSTEM,
    )
    return document


def link_evidence(db: Session, document: Document, entity_type: str, entity_id: int,
                  role: str = "SUPPORTING", note: Optional[str] = None,
                  user: Optional[User] = None) -> EvidenceLink:
    """Attach a document to a financial record, ignoring duplicate links."""
    existing = (
        db.query(EvidenceLink)
        .filter(EvidenceLink.document_id == document.id,
                EvidenceLink.entity_type == entity_type,
                EvidenceLink.entity_id == entity_id)
        .first()
    )
    if existing:
        return existing

    link = EvidenceLink(
        document_id=document.id, entity_type=entity_type, entity_id=entity_id,
        role=role, note=note, linked_by_id=user.id if user else None,
    )
    db.add(link)
    db.flush()
    return link


def evidence_for(db: Session, entity_type: str, entity_id: int) -> list[Document]:
    links = (
        db.query(EvidenceLink)
        .filter(EvidenceLink.entity_type == entity_type, EvidenceLink.entity_id == entity_id)
        .all()
    )
    return [link.document for link in links if link.document]


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def extract_text(content: bytes, mime_type: str) -> Extraction:
    """Pull text out of a document, honestly reporting what was possible."""
    result = Extraction()

    if mime_type == "application/pdf":
        return _extract_pdf(content)

    if mime_type in IMAGE_TYPES:
        return _extract_image(content)

    if mime_type in {"text/csv", "text/plain"}:
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                result.text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        result.status = OcrStatus.COMPLETED.value
        result.confidence = 1.0
        return result

    if mime_type and "spreadsheet" in mime_type:
        return _extract_excel(content)

    result.status = OcrStatus.NOT_REQUIRED.value
    return result


def _extract_pdf(content: bytes) -> Extraction:
    result = Extraction()
    try:
        import pdfplumber

        chunks, empty_pages = [], []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            result.page_count = len(pdf.pages)
            for index, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - one bad page must not abort
                    text = ""
                if text.strip():
                    chunks.append(text)
                else:
                    empty_pages.append(index)

        result.text = "\n".join(chunks)

        if not result.text.strip():
            # A PDF with no text layer is a scan. Be explicit about it.
            result.status = OcrStatus.UNAVAILABLE.value
            result.confidence = 0.0
            result.guidance = (
                "This PDF contains no readable text, which usually means it is a scanned "
                "image. No text could be extracted. Please upload a text-based PDF, a "
                "clearer scan, or enter the key values manually."
            )
        elif empty_pages:
            result.status = OcrStatus.PARTIAL.value
            result.confidence = round(
                (result.page_count - len(empty_pages)) / max(1, result.page_count), 3
            )
            result.guidance = (
                "No text could be read from page(s) "
                + ", ".join(str(p) for p in empty_pages)
                + ". Those pages may be scanned images."
            )
        else:
            result.status = OcrStatus.COMPLETED.value
            result.confidence = 0.95
    except Exception as exc:  # noqa: BLE001
        result.status = OcrStatus.FAILED.value
        result.error = str(exc)
        result.guidance = f"The PDF could not be read: {exc}"
    return result


def _extract_image(content: bytes) -> Extraction:
    """Attempt OCR on an image.

    OCR needs a Tesseract binary, which most machines do not have. Rather than
    fabricate a result, we say so and offer the manual path (spec s108).
    """
    result = Extraction(page_count=1)
    try:
        import pytesseract  # type: ignore
        from PIL import Image

        image = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(image)
        if text.strip():
            result.text = text
            result.status = OcrStatus.COMPLETED.value
            # OCR text is inherently less certain than an embedded text layer.
            result.confidence = 0.72
        else:
            result.status = OcrStatus.PARTIAL.value
            result.confidence = 0.0
            result.guidance = ("No readable text was found in this image. "
                               "Please upload a clearer photo or enter the values manually.")
    except ImportError:
        result.status = OcrStatus.UNAVAILABLE.value
        result.guidance = (
            "Automatic reading of images is not available on this system "
            "(no OCR engine is installed). The image has been stored safely as evidence. "
            "Please enter the key values manually, or upload a PDF instead."
        )
    except Exception as exc:  # noqa: BLE001
        result.status = OcrStatus.FAILED.value
        result.error = str(exc)
        result.guidance = f"The image could not be read: {exc}"
    return result


def _extract_excel(content: bytes) -> Extraction:
    result = Extraction()
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"--- {sheet.title} ---")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    lines.append(" | ".join(cells))
        workbook.close()
        result.text = "\n".join(lines)
        result.status = OcrStatus.COMPLETED.value
        result.confidence = 1.0
    except Exception as exc:  # noqa: BLE001
        result.status = OcrStatus.FAILED.value
        result.error = str(exc)
    return result


# ---------------------------------------------------------------------------
# Field mining
# ---------------------------------------------------------------------------
_LABELLED_PATTERNS: dict[str, list[str]] = {
    "order_number": [r"(?:order|g\.?o\.?|letter|memo)\s*(?:no\.?|number)\s*[:\-]\s*([A-Za-z0-9/\-\.]{3,60})"],
    "sanction_number": [r"sanction\s*(?:no\.?|number|order)\s*[:\-]\s*([A-Za-z0-9/\-\.]{3,60})"],
    "employee_code": [r"(?:employee|worker|staff)\s*(?:id|code|no\.?)\s*[:\-]\s*([A-Za-z0-9/\-]{3,40})"],
    "employee_name": [r"(?:name of (?:the )?(?:employee|worker)|employee name|name)\s*[:\-]\s*([A-Za-z .]{3,60})"],
    "designation": [r"designation\s*[:\-]\s*([A-Za-z .]{3,60})"],
    "issuing_authority": [r"(?:issued by|authority|office of(?: the)?)\s*[:\-]?\s*([A-Za-z ,.\-]{5,120})"],
    "account_number": [r"(?:a/?c|account)\s*(?:no\.?|number)\s*[:\-]\s*([X\*\d\- ]{6,30})"],
    "ifsc": [r"ifsc\s*(?:code)?\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})"],
}

_AMOUNT_LABELS = {
    "gross_amount": ("gross", "total earnings", "gross salary", "gross honorarium"),
    "net_amount": ("net", "net pay", "net amount", "net payable", "amount payable"),
    "deduction_amount": ("total deduction", "deductions", "less"),
    "sanctioned_amount": ("sanctioned amount", "amount sanctioned", "sanction amount"),
}


def mine_fields(text: str) -> tuple[dict, float]:
    """Extract structured fields from document text.

    Returns ``(fields, confidence)``. Only fields actually found are included;
    a missing field is absent, never guessed.
    """
    if not text or not text.strip():
        return {}, 0.0

    flat = normalize_text(text)
    fields: dict = {}

    for key, patterns in _LABELLED_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, flat, re.I)
            if match:
                fields[key] = normalize_text(match.group(1)).strip(" .-")
                break

    # Amounts by label.
    for key, labels in _AMOUNT_LABELS.items():
        for label in labels:
            match = re.search(
                rf"{re.escape(label)}\s*[:\-]?\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d{{1,2}})?)",
                flat, re.I,
            )
            if match:
                paise = try_to_paise(match.group(1))
                if paise is not None:
                    fields[key] = paise
                    break

    # Dates by label.
    for key, labels in {
        "document_date": ("dated", "date of issue", "date"),
        "effective_date": ("effective from", "with effect from", "w.e.f"),
        "sanction_date": ("sanction date", "date of sanction"),
    }.items():
        for label in labels:
            match = re.search(
                rf"{re.escape(label)}\s*[:\-]?\s*"
                r"(\d{1,2}[-/. ][A-Za-z0-9]{2,9}[-/. ]\d{2,4}|\d{4}-\d{2}-\d{2})",
                flat, re.I,
            )
            if match:
                parsed = parse_date(match.group(1))
                if parsed:
                    fields[key] = parsed.isoformat()
                    break

    utr = extract_utr(flat)
    if utr:
        fields["utr"] = utr

    months = extract_month_references(flat)
    if months:
        fields["month_references"] = [
            {"month": m, "year": y, "matched": t.strip()} for m, y, t in months[:6]
        ]

    # Confidence reflects how much of the document we understood, and is
    # capped: extraction confidence is never proof (spec s43).
    confidence = min(0.9, 0.25 + 0.1 * len(fields)) if fields else 0.0
    return fields, round(confidence, 3)


def process(db: Session, document: Document, content: Optional[bytes] = None) -> Extraction:
    """Extract text and fields from a stored document and record the result."""
    if content is None:
        try:
            from pathlib import Path

            content = Path(document.stored_path).read_bytes()
        except OSError as exc:
            document.ocr_status = OcrStatus.FAILED.value
            document.extraction_error = f"The stored file could not be read: {exc}"
            db.flush()
            return Extraction(status=OcrStatus.FAILED.value, error=str(exc))

    extraction = extract_text(content, document.mime_type or "")
    if extraction.text:
        fields, confidence = mine_fields(extraction.text)
        extraction.fields = fields
        extraction.confidence = max(extraction.confidence * 0.5, confidence)

    document.ocr_status = extraction.status
    # Cap stored text so a huge document cannot bloat the row.
    document.extracted_text = extraction.text[:200_000] if extraction.text else None
    document.extraction_confidence = extraction.confidence
    document.extraction_json = {
        "fields": extraction.fields,
        "confidence_band": confidence_band(extraction.confidence),
        "guidance": extraction.guidance,
        "extracted_at": utcnow().isoformat(),
    }
    document.extraction_error = extraction.error
    if extraction.page_count:
        document.page_count = extraction.page_count

    # Promote recognised fields onto the document header.
    fields = extraction.fields
    if not document.reference_number:
        document.reference_number = fields.get("order_number") or fields.get("sanction_number")
    if not document.issuing_authority and fields.get("issuing_authority"):
        document.issuing_authority = fields["issuing_authority"][:200]
    if not document.document_date and fields.get("document_date"):
        try:
            document.document_date = date.fromisoformat(fields["document_date"])
        except ValueError:
            pass

    db.flush()
    return extraction


def suggest_category(filename: str, text: str = "") -> str:
    """Guess a document category from its name and content."""
    blob = f"{filename} {text[:2000]}".lower()
    rules = [
        (DocumentCategory.BANK_STATEMENT.value, ("statement of account", "bank statement", "passbook", "account statement")),
        (DocumentCategory.GOVERNMENT_ORDER.value, ("government order", "g.o.", "notification", "shasanadesh", "शासनादेश")),
        (DocumentCategory.SANCTION_ORDER.value, ("sanction", "स्वीकृति")),
        (DocumentCategory.SALARY_SLIP.value, ("salary slip", "pay slip", "payslip", "वेतन पर्ची")),
        (DocumentCategory.PAYMENT_RECORD.value, ("pfms", "treasury", "payment advice", "payment order")),
        (DocumentCategory.ATTENDANCE.value, ("attendance", "उपस्थिति", "muster")),
        (DocumentCategory.BENEFIT_RECORD.value, ("distribution register", "ration", "supply register")),
        (DocumentCategory.RECEIPT.value, ("receipt", "रसीद", "voucher")),
    ]
    for category, keywords in rules:
        if any(word in blob for word in keywords):
            return category
    return DocumentCategory.OTHER.value
