"""Background job runner (spec s85, s86).

A small in-process worker pool backed by the ``ai_jobs`` table. Long work --
OCR, statement processing, org-wide reconciliation, annual reports -- runs off
the request thread so a large upload never freezes the interface.

Deliberately dependency-free: no Redis or Celery to install and operate. Job
state lives in the database, so progress survives a page reload and the user
can watch it. For a multi-process deployment, swap :func:`enqueue` for a real
broker; the job records and UI stay the same.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from ..db import SessionLocal, session_scope, utcnow
from ..enums import JobStatus, JobType
from ..models import AIJob

logger = logging.getLogger(__name__)

_queue: "queue.Queue[int]" = queue.Queue()
_workers: list[threading.Thread] = []
_started = threading.Event()

#: job_type -> handler(db, job)
HANDLERS: dict[str, Callable] = {}


def register(job_type: str):
    """Decorator registering a handler for a job type."""
    def decorator(func: Callable) -> Callable:
        HANDLERS[job_type] = func
        return func
    return decorator


def enqueue(db, *, organization_id: int, job_type: str, payload: Optional[dict] = None,
            created_by_id: Optional[int] = None, message: str = "") -> AIJob:
    """Create a job row and hand it to a worker."""
    job = AIJob(
        organization_id=organization_id,
        job_type=job_type,
        status=JobStatus.QUEUED.value,
        payload_json=payload or {},
        created_by_id=created_by_id,
        message=message or "Waiting to start",
    )
    db.add(job)
    db.flush()
    _queue.put(job.id)
    return job


#: A worker may pick up a job before the enqueuing request has committed, so
#: the row is not yet visible in another session. Rather than coordinate
#: transactions, the worker simply waits briefly for it to appear.
_VISIBILITY_RETRIES = 20
_VISIBILITY_DELAY = 0.1


def _load_job(db, job_id: int) -> Optional[AIJob]:
    import time

    for attempt in range(_VISIBILITY_RETRIES):
        job = db.get(AIJob, job_id)
        if job is not None:
            return job
        db.rollback()   # end the snapshot so the next read sees new commits
        time.sleep(_VISIBILITY_DELAY * (1 + attempt * 0.1))
    return None


def _run_job(job_id: int) -> None:
    with session_scope() as db:
        job = _load_job(db, job_id)
        if job is None:
            logger.warning("Job %s never became visible; skipping.", job_id)
            return
        if job.status not in {JobStatus.QUEUED.value, JobStatus.PROCESSING.value}:
            return
        handler = HANDLERS.get(job.job_type)
        if handler is None:
            job.status = JobStatus.FAILED.value
            job.error = f"No handler is registered for job type '{job.job_type}'."
            job.finished_at = utcnow()
            return

        job.status = JobStatus.PROCESSING.value
        job.started_at = utcnow()
        job.message = "Processing"
        db.flush()

        try:
            result = handler(db, job) or {}
            job.result_json = result
            # A handler may ask for human review rather than claiming success.
            if job.status == JobStatus.PROCESSING.value:
                job.status = JobStatus.COMPLETED.value
            job.message = result.get("message", "Completed")
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the worker
            logger.exception("Job %s (%s) failed", job_id, job.job_type)
            job.status = JobStatus.FAILED.value
            job.error = str(exc)
            job.message = "Failed"
        finally:
            job.finished_at = utcnow()


def _worker_loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            if job_id is None:
                return
            _run_job(job_id)
        except Exception:  # noqa: BLE001
            logger.exception("Worker crashed handling job %s", job_id)
        finally:
            _queue.task_done()


def start_workers() -> None:
    """Start the worker threads once per process."""
    if _started.is_set():
        return
    _started.set()

    for index in range(max(1, settings_worker_count())):
        thread = threading.Thread(target=_worker_loop, name=f"srs-worker-{index}", daemon=True)
        thread.start()
        _workers.append(thread)
    logger.info("Started %d background worker(s)", len(_workers))

    _requeue_orphans()


def settings_worker_count() -> int:
    from ..config import settings

    return settings.worker_threads


def _requeue_orphans() -> None:
    """Recover jobs left PROCESSING by a previous crash or restart."""
    db = SessionLocal()
    try:
        stuck = (
            db.query(AIJob)
            .filter(AIJob.status.in_([JobStatus.QUEUED.value, JobStatus.PROCESSING.value]))
            .all()
        )
        for job in stuck:
            job.status = JobStatus.QUEUED.value
            job.message = "Requeued after a restart"
            _queue.put(job.id)
        if stuck:
            db.commit()
            logger.info("Requeued %d interrupted job(s)", len(stuck))
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


def wait_for_idle(timeout: float = 30.0) -> bool:
    """Block until the queue drains. Used by tests and CLI commands."""
    done = threading.Event()

    def waiter() -> None:
        _queue.join()
        done.set()

    threading.Thread(target=waiter, daemon=True).start()
    return done.wait(timeout)


def run_now(job_id: int) -> None:
    """Execute a job synchronously (tests, single-shot CLI)."""
    _run_job(job_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
@register(JobType.RECONCILE_ORG.value)
def _handle_reconcile_org(db, job: AIJob) -> dict:
    from ..agents import orchestrator

    summary = orchestrator.process_organization(db, job.organization_id, job=job,
                                                actor_id=job.created_by_id)
    if summary.review_required:
        job.status = JobStatus.REQUIRES_REVIEW.value
    return {
        "processed": summary.processed,
        "reconciled": summary.reconciled,
        "review_required": summary.review_required,
        "short_payments": summary.short_payments,
        "excess_payments": summary.excess_payments,
        "duplicates_suspected": summary.duplicates_suspected,
        "unallocated_transactions": summary.unallocated_transactions,
        "anomalies": summary.anomalies_created,
        "message": summary.messages[0] if summary.messages else "Completed",
    }


@register(JobType.RECONCILE_PERIOD.value)
def _handle_reconcile_employee(db, job: AIJob) -> dict:
    from ..agents import orchestrator
    from ..models import Employee

    employee_id = (job.payload_json or {}).get("employee_id")
    employee = db.get(Employee, employee_id) if employee_id else None
    if employee is None:
        raise ValueError("The employee for this job no longer exists.")

    summary = orchestrator.process_employee(db, employee, actor_id=job.created_by_id)
    if summary.review_required:
        job.status = JobStatus.REQUIRES_REVIEW.value
    return {
        "processed": summary.processed,
        "reconciled": summary.reconciled,
        "review_required": summary.review_required,
        "message": (f"{summary.processed} transaction(s) processed for "
                    f"{employee.full_name}; {summary.review_required} need review."),
    }


@register(JobType.DOCUMENT_EXTRACTION.value)
def _handle_document(db, job: AIJob) -> dict:
    from ..agents import document_agent
    from ..models import Document

    document_id = (job.payload_json or {}).get("document_id")
    document = db.get(Document, document_id) if document_id else None
    if document is None:
        raise ValueError("The document for this job no longer exists.")

    extraction = document_agent.process(db, document)
    if extraction.status in {"UNAVAILABLE", "FAILED", "PARTIAL"}:
        job.status = JobStatus.REQUIRES_REVIEW.value
    return {
        "status": extraction.status,
        "confidence": extraction.confidence,
        "fields_found": len(extraction.fields),
        "guidance": extraction.guidance,
        "message": extraction.guidance or f"Document read ({extraction.status}).",
    }


@register(JobType.ANOMALY_SCAN.value)
def _handle_anomaly_scan(db, job: AIJob) -> dict:
    from ..agents import anomaly_agent

    found = anomaly_agent.scan_organization(db, job.organization_id)
    return {"anomalies": len(found), "message": f"{len(found)} finding(s) reviewed."}


@register(JobType.AGENTIC_MONITOR.value)
def _handle_monitor(db, job: AIJob) -> dict:
    from ..agents import anomaly_agent, monthly_close_agent

    anomaly_agent.scan_organization(db, job.organization_id)
    result = monthly_close_agent.monitor(db, job.organization_id)
    notified = monthly_close_agent.notify_from_monitor(db, job.organization_id)
    return {
        "critical": len(result.critical),
        "review": len(result.review),
        "reconciled": result.reconciled_count,
        "notifications": notified,
        "questions_checked": result.questions_checked,
        "message": (f"{len(result.critical)} critical and {len(result.review)} review "
                    f"item(s) need attention."),
    }


@register(JobType.RULE_SIMULATION.value)
def _handle_rule_simulation(db, job: AIJob) -> dict:
    from ..agents import arrear_agent
    from ..models import SalaryRule

    rule_id = (job.payload_json or {}).get("rule_id")
    rule = db.get(SalaryRule, rule_id) if rule_id else None
    if rule is None:
        raise ValueError("The rule for this simulation no longer exists.")

    result = arrear_agent.simulate_retroactive_change(db, rule)
    return {
        "employees_affected": result.employees_affected,
        "periods_affected": len(result.impacts),
        "total_delta_paise": result.total_delta_paise,
        "impacts": [
            {"period": i.period.label, "old_paise": i.old_paise,
             "new_paise": i.new_paise, "delta_paise": i.delta_paise}
            for i in result.impacts[:200]
        ],
        "message": (f"{result.employees_affected} employee(s) and {len(result.impacts)} "
                    f"month(s) would change."),
    }
