"""Command-line entry point.

    python manage.py init                 create the database
    python manage.py seed                 load the demonstration organisation
    python manage.py createadmin          create an administrator account
    python manage.py process              reconcile every employee
    python manage.py monitor              run the periodic inspection
    python manage.py testllm              check the AI provider responds
    python manage.py serve                start the web application
    python manage.py status               show a summary of the ledger
    python manage.py reset --yes          delete all data and start again
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows terminals default to cp1252 and cannot print Devanagari or the rupee
# sign; force UTF-8 so command output never crashes on a Hindi label.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - platform dependent
        pass

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.money import format_money  # noqa: E402


def cmd_init(args) -> int:
    init_db()
    print(f"Database ready: {settings.database_url}")
    print(f"Document vault: {settings.storage_dir}")
    return 0


def cmd_seed(args) -> int:
    from app.services.seed import seed_demo

    init_db()
    with session_scope() as db:
        info = seed_demo(db)

    if not info["created"]:
        print(info["message"])
        return 0

    print("Demonstration data created.\n")
    print("Sign in with any of these accounts:")
    for email, password in info["users"].items():
        print(f"  {email:24} {password}")
    print("\nNow run:  python manage.py process")
    return 0


def cmd_createadmin(args) -> int:
    from app.models import Organization, User
    from app.security import hash_password, password_problems

    init_db()
    with session_scope() as db:
        organization = db.query(Organization).first()
        if organization is None:
            organization = Organization(
                name=args.org or input("Organisation name: ").strip() or "My Organisation",
                currency="INR", country="India",
            )
            db.add(organization)
            db.flush()
            print(f"Created organisation: {organization.name}")

        email = (args.email or input("Email: ")).strip().lower()
        if db.query(User).filter(User.email == email).first():
            print(f"A user with the email {email} already exists.")
            return 1

        name = args.name or input("Full name: ").strip() or email
        password = args.password or getpass.getpass("Password: ")

        problems = password_problems(password)
        if problems:
            for problem in problems:
                print(f"  {problem}")
            return 1

        db.add(User(
            organization_id=organization.id, email=email, full_name=name,
            password_hash=hash_password(password), role="SUPER_ADMIN", is_active=True,
        ))
    print(f"Administrator created: {email}")
    return 0


def cmd_process(args) -> int:
    from app.agents import orchestrator
    from app.models import Organization

    with session_scope() as db:
        organizations = db.query(Organization).all()
        if not organizations:
            print("No organisation exists yet. Run: python manage.py seed")
            return 1

        for organization in organizations:
            print(f"Processing {organization.name}...")
            summary = orchestrator.process_organization(db, organization.id)
            print(f"  {summary.messages[0] if summary.messages else 'done'}")
    return 0


def cmd_monitor(args) -> int:
    from app.agents import anomaly_agent, monthly_close_agent
    from app.models import Organization

    with session_scope() as db:
        for organization in db.query(Organization).all():
            anomaly_agent.scan_organization(db, organization.id)
            result = monthly_close_agent.monitor(db, organization.id)
            sent = monthly_close_agent.notify_from_monitor(db, organization.id)
            print(f"{organization.name}: {len(result.critical)} critical, "
                  f"{len(result.review)} to review, {result.reconciled_count} reconciled, "
                  f"{sent} notification(s) sent.")
    return 0


def cmd_status(args) -> int:
    from app.models import (
        Anomaly,
        BankTransaction,
        Document,
        Employee,
        Organization,
        Reconciliation,
    )

    with session_scope() as db:
        organizations = db.query(Organization).all()
        if not organizations:
            print("No data yet. Run: python manage.py seed")
            return 0

        for organization in organizations:
            employees = db.query(Employee).filter(
                Employee.organization_id == organization.id).count()
            transactions = db.query(BankTransaction).filter(
                BankTransaction.organization_id == organization.id).count()
            documents = db.query(Document).filter(
                Document.organization_id == organization.id).count()
            recons = db.query(Reconciliation).filter(
                Reconciliation.organization_id == organization.id).all()
            open_findings = db.query(Anomaly).filter(
                Anomaly.organization_id == organization.id,
                Anomaly.status == "OPEN").count()

            entitled = sum(r.entitled_paise for r in recons)
            credited = sum(r.credited_paise for r in recons)
            pending = sum(max(0, r.difference_paise) for r in recons)
            reconciled = sum(1 for r in recons if r.outcome == "FULLY_RECONCILED")

            print(f"\n{organization.name}")
            print(f"  Employees          : {employees}")
            print(f"  Transactions       : {transactions}")
            print(f"  Documents          : {documents}")
            print(f"  Periods reconciled : {reconciled} of {len(recons)}")
            print(f"  Total entitled     : {format_money(entitled, organization.currency)}")
            print(f"  Total received     : {format_money(credited, organization.currency)}")
            print(f"  Still pending      : {format_money(pending, organization.currency)}")
            print(f"  Open findings      : {open_findings}")
    return 0


def cmd_testllm(args) -> int:
    """Check the configured AI provider actually answers."""
    from app.agents import llm

    print("AI narrative layer\n" + "-" * 60)

    chosen = llm.provider()
    if chosen is None:
        print("Status   : OFF (no provider configured)")
        print(f"Reason   : {llm.configuration_problem()}")
    else:
        key = settings.llm_key_for(chosen.key)
        shown = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("set" if key else "not set")
        print(f"Provider : {chosen.label} ({chosen.key})")
        print(f"Model    : {llm.model_name()}")
        print(f"Key      : {shown if chosen.needs_key else 'not required (local)'}")
        if chosen.key == "ollama":
            print(f"Host     : {settings.ollama_host}")

    if getattr(args, "models", False) and chosen is not None:
        print("-" * 60)
        models = llm.list_models()
        if models:
            print(f"Models available to this account ({len(models)}):")
            for name in models:
                marker = "  <- in use" if name == llm.model_name() else ""
                print(f"  {name}{marker}")
            print()
            print("Set LLM_MODEL in .env to choose one.")
        else:
            print("Could not list models for this provider.")
        return 0

    print("-" * 60)
    ok, message = llm.test_connection()
    print(("PASS  " if ok else "FAIL  ") + message)

    if not ok:
        print("\nFree providers you can use:")
        for p in llm.PROVIDERS.values():
            if p.free_tier:
                need = "no key needed" if not p.needs_key else f"{p.key.upper()}_API_KEY"
                print(f"  {p.label:22} {need:22} {p.signup_url}")
        print("\nPut the key in your .env file, then run this command again.")
        print("The application works fully without it -- every figure is still")
        print("produced by the rule-based engines either way.")
    return 0 if ok else 1


def cmd_reset(args) -> int:
    if not args.yes:
        print("This deletes ALL data. Re-run with --yes to confirm.")
        return 1

    from app.db import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("All data deleted. The database is empty.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    init_db()
    host, port = args.host, args.port
    print(f"\n  {settings.app_name}")
    print(f"  Open your browser at:  http://{'localhost' if host == '0.0.0.0' else host}:{port}\n")
    print("  Press Ctrl+C to stop.\n")

    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload,
                log_level="info", access_log=not args.quiet)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="Salary & Payment Reconciliation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="create the database")
    sub.add_parser("seed", help="load the demonstration organisation")

    admin = sub.add_parser("createadmin", help="create an administrator account")
    admin.add_argument("--email")
    admin.add_argument("--name")
    admin.add_argument("--password")
    admin.add_argument("--org")

    sub.add_parser("process", help="reconcile every employee")
    sub.add_parser("monitor", help="run the periodic inspection")
    sub.add_parser("status", help="show a summary of the ledger")
    testllm = sub.add_parser("testllm", help="check the AI provider responds")
    testllm.add_argument("--models", action="store_true",
                         help="list the models this account can use")

    reset = sub.add_parser("reset", help="delete all data")
    reset.add_argument("--yes", action="store_true")

    serve = sub.add_parser("serve", help="start the web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--quiet", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "init": cmd_init, "seed": cmd_seed, "createadmin": cmd_createadmin,
        "process": cmd_process, "monitor": cmd_monitor, "status": cmd_status,
        "testllm": cmd_testllm,
        "reset": cmd_reset, "serve": cmd_serve,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
