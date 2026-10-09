from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared_runtime.sqlite_compat import wal_reset_fix_available_for


def wal_reset_fix_status(version: str) -> tuple[bool, str]:
    fixed = wal_reset_fix_available_for(version)
    if fixed:
        return True, "SQLite runtime includes the upstream WAL-reset fix or an official backport."
    return False, (
        "SQLite runtime may be affected by the WAL-reset corruption bug; "
        "this project will avoid WAL for the shared state DB, and upgrading to a current SQLite runtime is still recommended."
    )


def sidecars(path: Path) -> list[Path]:
    return [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]


def inspect_db(path: Path) -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"SQLite runtime: {sqlite3.sqlite_version}")
    fixed, message = wal_reset_fix_status(sqlite3.sqlite_version)
    print(f"WAL-reset fix: {'yes' if fixed else 'NO'} - {message}")
    print(f"Database: {path}")
    for item in sidecars(path):
        if item.exists():
            print(f"  {item.name}: {item.stat().st_size} bytes")
        else:
            print(f"  {item.name}: missing")

    if not path.exists():
        print("STATUS: database file does not exist; the application can create a fresh one on next start.")
        return 0

    try:
        connection = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only=ON")
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
            print("quick_check:")
            for row in quick[:20]:
                print(f"  {row}")
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
            print("integrity_check:")
            for row in integrity[:50]:
                print(f"  {row}")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        print(f"STATUS: CORRUPT/UNREADABLE - {type(exc).__name__}: {exc}")
        return 2

    if quick == ["ok"] and integrity == ["ok"]:
        print("STATUS: OK")
        return 0
    print("STATUS: INTEGRITY ERRORS DETECTED")
    return 2


def quarantine(path: Path) -> int:
    existing = [item for item in sidecars(path) if item.exists()]
    if not existing:
        print("Nothing to quarantine.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = path.parent / f"corrupt_state_backup_{stamp}"
    destination.mkdir(parents=True, exist_ok=False)
    print("IMPORTANT: stop the Agent and MCP processes before quarantining a WAL database.")
    for item in existing:
        target = destination / item.name
        shutil.move(str(item), str(target))
        print(f"moved: {item} -> {target}")
    print(f"Quarantine complete: {destination}")
    print("Restart the services to let the project create a fresh state database.")
    return 0


def copy_snapshot(path: Path, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    print("IMPORTANT: stop all processes using this database before taking a filesystem snapshot.")
    copied = 0
    for item in sidecars(path):
        if item.exists():
            shutil.copy2(item, destination / item.name)
            copied += 1
            print(f"copied: {item.name}")
    if copied == 0:
        print("No database files found.")
        return 1
    print(f"Snapshot copied to: {destination}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose and safely quarantine the Hytera SQLite state database.")
    parser.add_argument("--path", default=os.environ.get("STATE_DB_PATH", "data/state.sqlite3"))
    parser.add_argument("--quarantine", action="store_true", help="Move DB/WAL/SHM into a timestamped backup directory.")
    parser.add_argument("--snapshot-dir", help="Copy DB/WAL/SHM into this directory. Stop services first.")
    args = parser.parse_args()
    path = Path(args.path).expanduser().resolve(strict=False)
    if args.snapshot_dir:
        return copy_snapshot(path, Path(args.snapshot_dir).expanduser().resolve(strict=False))
    if args.quarantine:
        return quarantine(path)
    return inspect_db(path)


if __name__ == "__main__":
    raise SystemExit(main())
