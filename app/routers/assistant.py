"""AI assistant: the chat page and its JSON endpoint (spec s34, s105)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..agents import llm, query_agent
from ..db import get_db
from ..deps import base_context, get_current_user, verify_csrf
from ..models import ChatMessage, Employee, User
from ..security import PermissionDenied, can_access_employee, is_admin

router = APIRouter()


def _templates():
    from ..main import templates

    return templates


def _target_employee(db: Session, user: User, employee_uid: Optional[str]) -> Optional[Employee]:
    if employee_uid:
        employee = db.query(Employee).filter(Employee.uid == employee_uid).first()
        if employee is None or not can_access_employee(db, user, employee):
            raise PermissionDenied("You do not have access to that employee's records.")
        return employee
    if user.employee_id:
        return db.get(Employee, user.employee_id)
    return None


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   employee_uid: Optional[str] = None):
    context = base_context(request, db, user)
    employee = _target_employee(db, user, employee_uid)

    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(40)
        .all()
    )
    suggestions = (query_agent.SUGGESTED_QUESTIONS_HI if context["locale"] == "hi"
                   else query_agent.SUGGESTED_QUESTIONS_EN)

    from ..security import visible_employees

    context.update({
        "employee": employee,
        "messages": list(reversed(history)),
        "suggestions": suggestions,
        "llm_status": llm.status_note(context["locale"]),
        "llm_active": llm.available(),
        "employees": (visible_employees(db, user).order_by(Employee.full_name).all()
                      if is_admin(user) else []),
    })
    return _templates().TemplateResponse("assistant.html", context)


@router.post("/assistant")
def assistant_ask(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  question: str = Form(...), employee_uid: str = Form(""),
                  csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    context = base_context(request, db, user)
    employee = _target_employee(db, user, employee_uid or None)

    if employee is None:
        answer_text = ("Your account is not linked to an employee record, so I have no "
                       "payment history to answer from."
                       if context["locale"] != "hi" else
                       "आपका खाता किसी कर्मचारी रिकॉर्ड से नहीं जुड़ा है, इसलिए उत्तर देने "
                       "के लिए कोई भुगतान इतिहास उपलब्ध नहीं है।")
        db.add(ChatMessage(organization_id=user.organization_id, user_id=user.id,
                           role="user", content=question[:4000]))
        db.add(ChatMessage(organization_id=user.organization_id, user_id=user.id,
                           role="assistant", content=answer_text, intent="no_employee"))
        db.commit()
        return RedirectResponse(url="/assistant", status_code=status.HTTP_303_SEE_OTHER)

    db.add(ChatMessage(organization_id=user.organization_id, user_id=user.id,
                       employee_id=employee.id, role="user", content=question[:4000]))

    answer = query_agent.ask(db, employee, question, locale=context["locale"])

    db.add(ChatMessage(
        organization_id=user.organization_id, user_id=user.id, employee_id=employee.id,
        role="assistant", content=answer.text, intent=answer.intent,
        evidence_json={"evidence": answer.evidence}, used_llm=answer.used_llm,
    ))
    db.commit()

    target = "/assistant" + (f"?employee_uid={employee.uid}" if employee_uid else "")
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/assistant")
def assistant_api(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  question: str = Form(...), employee_uid: str = Form("")):
    """JSON endpoint used by the in-page assistant widget."""
    context = base_context(request, db, user)
    employee = _target_employee(db, user, employee_uid or None)
    if employee is None:
        return JSONResponse({
            "text": "Your account is not linked to an employee record.",
            "intent": "no_employee", "evidence": [], "used_llm": False, "followups": [],
        })

    answer = query_agent.ask(db, employee, question, locale=context["locale"])

    db.add(ChatMessage(organization_id=user.organization_id, user_id=user.id,
                       employee_id=employee.id, role="user", content=question[:4000]))
    db.add(ChatMessage(
        organization_id=user.organization_id, user_id=user.id, employee_id=employee.id,
        role="assistant", content=answer.text, intent=answer.intent,
        evidence_json={"evidence": answer.evidence}, used_llm=answer.used_llm,
    ))
    db.commit()
    return JSONResponse(answer.as_dict())


@router.post("/assistant/clear")
def clear_history(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user),
                  csrf_field: str = Form("", alias="csrf")):
    verify_csrf(request, csrf_field)
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    db.commit()
    return RedirectResponse(url="/assistant", status_code=status.HTTP_303_SEE_OTHER)
