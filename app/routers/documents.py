"""Evidence vault: upload, browse, view, download and version documents."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import document_agent, verification_agent
from ..audit import AuditSource, record as audit_record
from ..db import get_db
from ..deps import base_context, get_current_user, verify_csrf
from ..enums import DocumentCategory, JobType, VerificationDimension, VerificationLevel
from ..models import Document, Employee, Organization, User
from ..security import (
    PermissionDenied,
    UploadRejected,
    can_access_employee,
    is_admin,
    require_write,
    validate_upload,
    visible_employees,
)
from ..services import jobs

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


def _get_document(db: Session, user: User, uid: str) -> Document:
    document = db.query(Document).filter(Document.uid == uid).first()
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.organization_id != user.organization_id:
        raise PermissionDenied("You do not have access to this document.")
    # A document tied to an employee follows that employee's access rules.
    if document.employee_id:
        employee = db.get(Employee, document.employee_id)
        if not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to this document.")
    return document


@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   category: Optional[str] = None, employee_uid: Optional[str] = None,
                   error: Optional[str] = None, q: Optional[str] = None):
    context = base_context(request, db, user)

    query = db.query(Document).filter(Document.organization_id == user.organization_id,
                                      Document.is_current.is_(True))
    if not is_admin(user) and user.role not in {"AUDITOR", "READ_ONLY"}:
        ids = [e.id for e in visible_employees(db, user).all()]
        query = query.filter(Document.employee_id.in_(ids or [-1])
                             | Document.employee_id.is_(None))
    if category:
        query = query.filter(Document.category == category)
    if employee_uid:
        employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if employee and can_access_employee(db, user, employee):
            query = query.filter(Document.employee_id == employee.id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(Document.title.ilike(like)
                             | Document.original_filename.ilike(like)
                             | Document.reference_number.ilike(like))

    documents = query.order_by(Document.created_at.desc()).limit(300).all()

    counts: dict[str, int] = {}
    for cat in DocumentCategory.values():
        counts[cat] = (
            db.query(Document)
            .filter(Document.organization_id == user.organization_id,
                    Document.is_current.is_(True), Document.category == cat)
            .count()
        )

    context.update({
        "documents": documents,
        "categories": DocumentCategory.values(),
        "category_counts": counts,
        "selected_category": category,
        "employees": visible_employees(db, user).order_by(Employee.full_name).all(),
        "error": error,
        "q": q,
    })
    return _templates().TemplateResponse("documents.html", context)


@router.post("/documents/upload")
async def upload_document(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user),
                          document: UploadFile = File(...),
                          title: str = Form(""), category: str = Form("OTHER"),
                          employee_id: Optional[int] = Form(None),
                          entity_type: Optional[str] = Form(None),
                          entity_id: Optional[int] = Form(None),
                          supersedes_uid: Optional[str] = Form(None),
                          replacement_reason: str = Form(""),
                          csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)

    content = await document.read()
    filename = document.filename or "document"

    try:
        mime = validate_upload(filename, content)
    except UploadRejected as exc:
        return RedirectResponse(url=f"/documents?error={str(exc)[:300]}",
                                status_code=status.HTTP_303_SEE_OTHER)

    employee = None
    if employee_id:
        employee = db.get(Employee, employee_id)
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee.")

    supersedes = None
    if supersedes_uid:
        supersedes = _get_document(db, user, supersedes_uid)
        if not replacement_reason.strip():
            return RedirectResponse(
                url="/documents?error=A reason is required when replacing a document.",
                status_code=status.HTTP_303_SEE_OTHER)

    organization = db.get(Organization, user.organization_id)
    if category not in DocumentCategory.values():
        category = document_agent.suggest_category(filename)

    stored = document_agent.store(
        db, organization=organization, content=content, filename=filename,
        title=title.strip() or filename, category=category, mime_type=mime,
        uploaded_by=user, employee_id=employee.id if employee else None,
        supersedes=supersedes, replacement_reason=replacement_reason.strip() or None,
    )

    if entity_type and entity_id:
        document_agent.link_evidence(db, stored, entity_type, int(entity_id),
                                     role="PRIMARY", user=user)

    jobs.enqueue(db, organization_id=organization.id,
                 job_type=JobType.DOCUMENT_EXTRACTION.value,
                 payload={"document_id": stored.id}, created_by_id=user.id,
                 message=f"Reading {stored.title}")
    uid = stored.uid
    db.commit()

    return RedirectResponse(url=f"/documents/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/documents/{uid}", response_class=HTMLResponse)
def document_detail(uid: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    context = base_context(request, db, user)
    document = _get_document(db, user, uid)

    versions = []
    node = document
    seen = set()
    while node is not None and node.id not in seen:
        seen.add(node.id)
        versions.append(node)
        node = db.get(Document, node.supersedes_id) if node.supersedes_id else None
    newer = db.query(Document).filter(Document.supersedes_id == document.id).all()

    from ..audit import history
    from ..models import EvidenceLink

    links = db.query(EvidenceLink).filter(EvidenceLink.document_id == document.id).all()

    context.update({
        "document": document,
        "versions": versions,
        "newer_versions": newer,
        "links": links,
        "audit_entries": history(db, "Document", document.id, limit=30),
        "extraction": document.extraction_json or {},
        "verification": verification_agent.summary(db, "Document", document.id),
        "levels": [(i, VerificationLevel.label(i, context["locale"])) for i in range(4)],
        "VerificationDimension": VerificationDimension,
    })
    return _templates().TemplateResponse("document_detail.html", context)


@router.get("/documents/{uid}/download")
def download_document(uid: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user), inline: int = 0):
    document = _get_document(db, user, uid)
    path = Path(document.stored_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The stored file is missing from the vault.")

    audit_record(db, action="DOWNLOAD_DOCUMENT", entity_type="Document",
                 entity_id=document.id, entity_uid=document.uid,
                 organization_id=user.organization_id, actor=user,
                 reason="Document accessed", source=AuditSource.HUMAN,
                 ip_address=request.client.host if request.client else None, commit=True)

    disposition = "inline" if inline else "attachment"
    return FileResponse(
        path, media_type=document.mime_type or "application/octet-stream",
        filename=document.original_filename,
        headers={"Content-Disposition": f'{disposition}; filename="{document.original_filename}"'},
    )


@router.post("/documents/{uid}/verify")
def verify_document(uid: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user),
                    dimension: str = Form(...), level: int = Form(...),
                    source: str = Form(...), note: str = Form(""),
                    csrf_field: str = Form("", alias="csrf")):
    """Spec s84: marking something officially verified needs a human decision."""
    verify_csrf(request, csrf_field)
    require_write(user)
    document = _get_document(db, user, uid)

    if dimension not in VerificationDimension.values():
        raise HTTPException(status_code=400, detail="Unknown verification dimension")

    try:
        verification_agent.record_verification(
            db, organization_id=user.organization_id, entity_type="Document",
            entity_id=document.id, dimension=dimension, level=int(level),
            source=source, verifier=user, evidence_document=document, note=note or None,
        )
    except verification_agent.VerificationRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return RedirectResponse(url=f"/documents/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/documents/{uid}/reprocess")
def reprocess_document(uid: str, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user),
                       csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    document = _get_document(db, user, uid)

    jobs.enqueue(db, organization_id=user.organization_id,
                 job_type=JobType.DOCUMENT_EXTRACTION.value,
                 payload={"document_id": document.id}, created_by_id=user.id,
                 message=f"Re-reading {document.title}")
    db.commit()
    return RedirectResponse(url=f"/documents/{uid}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/documents/{uid}/link")
def link_document(uid: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  entity_type: str = Form(...), entity_id: int = Form(...),
                  role: str = Form("SUPPORTING"), note: str = Form(""),
                  csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    require_write(user)
    document = _get_document(db, user, uid)

    document_agent.link_evidence(db, document, entity_type, int(entity_id),
                                 role=role, note=note or None, user=user)
    audit_record(db, action="LINK_EVIDENCE", entity_type=entity_type, entity_id=int(entity_id),
                 organization_id=user.organization_id, actor=user,
                 new_value={"document": document.uid, "role": role},
                 reason=note or "Evidence linked", source=AuditSource.HUMAN)
    db.commit()

    back = request.headers.get("referer") or f"/documents/{uid}"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
