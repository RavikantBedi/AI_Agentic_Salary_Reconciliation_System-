"""Backup and restore, on any platform.

    python backup.py create  --to /backups          take a backup
    python backup.py verify  /backups/db-<stamp>.db check one is readable
    python backup.py list    --in /backups          show what exists
    python backup.py prune   --in /backups --keep 30

Backs up BOTH halves of the system:

  * the database  — using SQLite's online backup API, which is safe to run
    while the application is serving traffic and is WAL-aware
  * the evidence vault — the original uploaded documents

Backing up only the database produces a restore whose evidence links all point
at missing files, so both are taken together and named with the same stamp.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - platform dependent
        pass

from app.config import settings  # noqa: E402


def _database_path() -> Path | None:
    """The SQLite file behind DATABASE_URL, or None for other engines."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    return Path(url.split("///")[-1]).resolve()


def cmd_create(args) -> int:
    destination = Path(args.to).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    source = _database_path()
    if source is None:
        print("DATABASE_URL is not SQLite. Use your database's own dump tool "
              "(for PostgreSQL: pg_dump), then back up the documents folder below.")
    elif not source.exists():
        print(f"Database not found: {source}")
        return 1
    else:
        target = destination / f"db-{stamp}.db"
        live = sqlite3.connect(str(source))
        copy = sqlite3.connect(str(target))
        try:
            # Online backup: consistent even while the application is writing.
            with copy:
                live.backup(copy)
        finally:
            live.close()
            copy.close()
        print(f"Database  -> {target}  ({target.stat().st_size / 1_048_576:.1f} MB)")

    vault = Path(settings.storage_dir)
    if vault.exists() and any(vault.rglob("*")):
        archive = destination / f"documents-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(vault, arcname="documents")
        print(f"Documents -> {archive}  ({archive.stat().st_size / 1_048_576:.1f} MB)")
    else:
        print(f"Documents -> nothing to back up ({vault} is empty)")

    print(f"\nStamp: {stamp}")
    print("Restore both halves together, using the same stamp.")
    return 0


def cmd_verify(args) -> int:
    path = Path(args.path).resolve()
    if not path.exists():
        print(f"Not found: {path}")
        return 1

    connection = sqlite3.connect(str(path))
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        tables = connection.execute(
            "select count(*) from sqlite_master where type='table'").fetchone()[0]
        counts = {}
        for table in ("employees", "bank_transactions", "reconciliations", "audit_logs"):
            try:
                counts[table] = connection.execute(
                    f"select count(*) from {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = "missing"
    finally:
        connection.close()

    print(f"File      : {path}")
    print(f"Integrity : {integrity}")
    print(f"Tables    : {tables}")
    for table, count in counts.items():
        print(f"  {table:20} {count}")

    ok = integrity == "ok" and tables > 40
    print("\n" + ("Backup is readable and complete." if ok
                  else "PROBLEM: this backup does not look usable."))
    return 0 if ok else 1


def cmd_list(args) -> int:
    folder = Path(args.in_dir).resolve()
    if not folder.exists():
        print(f"No such folder: {folder}")
        return 1

    rows = sorted(list(folder.glob("db-*.db")) + list(folder.glob("documents-*.tar.gz")))
    if not rows:
        print(f"No backups in {folder}")
        return 0

    print(f"{'File':44} {'Size':>10}  Taken")
    for item in rows:
        stat = item.stat()
        print(f"{item.name:44} {stat.st_size / 1_048_576:9.1f}M  "
              f"{datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M}")
    return 0


def cmd_prune(args) -> int:
    folder = Path(args.in_dir).resolve()
    cutoff = datetime.now().timestamp() - args.keep * 86400

    removed = 0
    for pattern in ("db-*.db", "documents-*.tar.gz"):
        for item in folder.glob(pattern):
            if item.stat().st_mtime < cutoff:
                item.unlink()
                print(f"removed {item.name}")
                removed += 1

    print(f"{removed} backup file(s) older than {args.keep} days removed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="backup.py", description="Backup and restore helper",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command")

    create = sub.add_parser("create", help="take a backup")
    create.add_argument("--to", default="backups", help="destination folder")

    verify = sub.add_parser("verify", help="check a backup is readable")
    verify.add_argument("path", help="path to a db-*.db backup")

    listing = sub.add_parser("list", help="show existing backups")
    listing.add_argument("--in", dest="in_dir", default="backups")

    prune = sub.add_parser("prune", help="delete old backups")
    prune.add_argument("--in", dest="in_dir", default="backups")
    prune.add_argument("--keep", type=int, default=30, help="days to keep")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    return {"create": cmd_create, "verify": cmd_verify,
            "list": cmd_list, "prune": cmd_prune}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
