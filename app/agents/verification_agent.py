"""AGENT 7b - Government Payment Verification framework (spec s28, s84, s108).

The evidence hierarchy is strict and, crucially, *multi-dimensional*: a record
is verified separately for "the money arrived" and "the payment was
authorised", because one never implies the other.

There is no fake government API here (spec s108). Levels 3-5 exist in the
model and can be recorded when a real integration or an official portal record
is supplied, but the system never claims a portal was consulted when it was
not. Where no integration exists, the honest path offered to the user is
"upload the official record for verification".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..audit import AuditSource, record as audit_record
from ..db import utcnow
from ..enums import VerificationDimension, VerificationLevel
from ..models import Document, User, VerificationRecord

AGENT_NAME = "verification_agent"


@dataclass
class VerificationState:
    level: int
    dimension: str
    source: str
    verified_on: Optional[date] = None
    document_id: Optional[int] = None
    note: Optional[str] = None

    @property
    def label(self) -> str:
        return VerificationLevel.label(self.level)


#: Which levels a human may assert through the UI. Level 4 (authorised API)
#: cannot be claimed by hand -- only a real integration may write it, so a
#: person cannot mark something "API verified" when no API was called.
HUMAN_ASSERTABLE_LEVELS = {
    VerificationLevel.USER_CLAIM,
    VerificationLevel.BANK_STATEMENT,
    VerificationLevel.OFFICIAL_DOCUMENT,
    VerificationLevel.GOVERNMENT_PORTAL,
}


class VerificationRefused(Exception):
    """Raised when a verification claim is not supported by evidence."""


def record_verification(
    db: Session,
    *,
    organization_id: int,
    entity_type: str,
    entity_id: int,
    dimension: str,
    level: int,
    source: str,
    verifier: Optional[User] = None,
    evidence_document: Optional[Document] = None,
    confidence: float = 1.0,
    note: Optional[str] = None,
    verified_on: Optional[date] = None,
    allow_api_level: bool = False,
) -> VerificationRecord:
    """Assert a verification level for one dimension of one record.

    Guard rails, enforced here rather than in the UI so they cannot be
    bypassed:

    * Level 2 and above REQUIRE a linked evidence document. Marking something
      "officially verified" with nothing attached is exactly the fabrication
      the specification forbids (spec s52).
    * Level 4 is reserved for genuine integrations.
    """
    level = int(level)

    if level >= VerificationLevel.OFFICIAL_DOCUMENT and evidence_document is None:
        raise VerificationRefused(
            "Marking a record as officially verified requires an attached document. "
            "Please upload the payment order, sanction order or official statement first."
        )

    if level == VerificationLevel.AUTHORISED_API and not allow_api_level:
        raise VerificationRefused(
            "Level 4 (authorised API) can only be set by a real government integration. "
            "No such integration is configured on this system. Upload the official "
            "record instead to verify at Level 2 or 3."
        )

    if not allow_api_level and level not in HUMAN_ASSERTABLE_LEVELS and level != VerificationLevel.CROSS_CONFIRMED:
        raise VerificationRefused(f"Verification level {level} cannot be set manually.")

    if level == VerificationLevel.CROSS_CONFIRMED:
        # Cross-confirmation genuinely means two independent official sources.
        existing_official = (
            db.query(VerificationRecord)
            .filter(VerificationRecord.entity_type == entity_type,
                    VerificationRecord.entity_id == entity_id,
                    VerificationRecord.dimension == dimension,
                    VerificationRecord.level >= VerificationLevel.OFFICIAL_DOCUMENT)
            .all()
        )
        sources = {r.evidence_document_id for r in existing_official if r.evidence_document_id}
        if evidence_document is not None:
            sources.add(evidence_document.id)
        if len(sources) < 2:
            raise VerificationRefused(
                "Level 5 requires two independent official sources that agree. "
                f"Only {len(sources)} official source(s) are currently attached."
            )

    record = VerificationRecord(
        organization_id=organization_id,
        entity_type=entity_type,
        entity_id=entity_id,
        dimension=dimension,
        level=level,
        source=source[:200],
        evidence_document_id=evidence_document.id if evidence_document else None,
        confidence=confidence,
        verified_on=verified_on or date.today(),
        verifier_user_id=verifier.id if verifier else None,
        note=note,
    )
    db.add(record)
    db.flush()

    audit_record(
        db,
        action="RECORD_VERIFICATION",
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
        actor=verifier,
        new_value={"dimension": dimension, "level": level, "source": source,
                   "document": evidence_document.uid if evidence_document else None},
        reason=note or "Verification recorded",
        source=AuditSource.HUMAN if verifier else AuditSource.agent(AGENT_NAME),
    )
    return record


def current_level(db: Session, entity_type: str, entity_id: int, dimension: str) -> int:
    """Highest recorded level for one dimension of a record."""
    rows = (
        db.query(VerificationRecord)
        .filter(VerificationRecord.entity_type == entity_type,
                VerificationRecord.entity_id == entity_id,
                VerificationRecord.dimension == dimension)
        .all()
    )
    return max((r.level for r in rows), default=VerificationLevel.USER_CLAIM)


def summary(db: Session, entity_type: str, entity_id: int) -> dict[str, VerificationState]:
    """Verification state across every dimension of a record."""
    out: dict[str, VerificationState] = {}
    for dimension in VerificationDimension.values():
        rows = (
            db.query(VerificationRecord)
            .filter(VerificationRecord.entity_type == entity_type,
                    VerificationRecord.entity_id == entity_id,
                    VerificationRecord.dimension == dimension)
            .order_by(VerificationRecord.level.desc())
            .all()
        )
        if rows:
            best = rows[0]
            out[dimension] = VerificationState(
                level=best.level, dimension=dimension, source=best.source,
                verified_on=best.verified_on, document_id=best.evidence_document_id,
                note=best.note,
            )
        else:
            out[dimension] = VerificationState(
                level=VerificationLevel.USER_CLAIM, dimension=dimension,
                source="No verification recorded",
            )
    return out


def describe_gap(credit_level: int, sanction_level: int, locale: str = "en") -> str:
    """Plain-language statement of what is and is not yet proven.

    This is the wording that keeps the two dimensions from being conflated in
    the user's mind (spec s28).
    """
    if locale == "hi":
        if credit_level >= VerificationLevel.BANK_STATEMENT and sanction_level >= VerificationLevel.OFFICIAL_DOCUMENT:
            return ("बैंक रिकॉर्ड से पुष्टि है कि राशि प्राप्त हुई, और आधिकारिक दस्तावेज़ से "
                    "पुष्टि है कि भुगतान स्वीकृत था।")
        if credit_level >= VerificationLevel.BANK_STATEMENT:
            return ("बैंक रिकॉर्ड से पुष्टि है कि राशि प्राप्त हुई। यह अभी प्रमाणित नहीं है कि "
                    "भुगतान आधिकारिक रूप से स्वीकृत था — इसके लिए स्वीकृति आदेश अपलोड करें।")
        if sanction_level >= VerificationLevel.OFFICIAL_DOCUMENT:
            return ("आधिकारिक दस्तावेज़ से पुष्टि है कि भुगतान स्वीकृत था, परंतु बैंक में राशि "
                    "आने की पुष्टि नहीं है — बैंक स्टेटमेंट अपलोड करें।")
        return "इस रिकॉर्ड के लिए कोई स्वतंत्र प्रमाण उपलब्ध नहीं है।"

    if credit_level >= VerificationLevel.BANK_STATEMENT and sanction_level >= VerificationLevel.OFFICIAL_DOCUMENT:
        return ("Bank records confirm the money arrived, and an official document confirms "
                "the payment was sanctioned.")
    if credit_level >= VerificationLevel.BANK_STATEMENT:
        return ("Bank records confirm the money arrived. It is NOT yet confirmed that the "
                "payment was officially sanctioned - upload the sanction or payment order "
                "to verify that separately.")
    if sanction_level >= VerificationLevel.OFFICIAL_DOCUMENT:
        return ("An official document confirms the payment was sanctioned, but there is no "
                "bank evidence that the money arrived - upload the bank statement covering "
                "the expected credit date.")
    return "No independent evidence is available for this record yet."


def integrations_available() -> list[dict]:
    """Government integrations configured on this deployment.

    Returns an empty list unless a real integration is configured. The UI uses
    this to offer document upload instead of a fictional "verify online"
    button (spec s108).
    """
    return []
