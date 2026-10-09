# -*- coding: utf-8 -*-
"""Version-aware SQLite migration runner shared by call repositories."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _migration_version(migration: Path) -> int:
    prefix, separator, _ = migration.stem.partition("_")
    if not separator or not prefix.isdigit():
        raise RuntimeError(f"invalid CallStore migration filename: {migration.name}")
    return int(prefix)


def run_call_store_migrations(connection: sqlite3.Connection, migration_dir: Path) -> None:
    """Apply each call-store migration at most once for the current database.

    Migration SQL remains the source of truth for recording ``migration_version``.
    The runner only skips versions already recorded, so repository construction is
    safe to repeat against an existing database without replaying ``ALTER TABLE``.
    """

    migrations = sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise RuntimeError(f"no CallStore migrations found: {migration_dir}")

    has_version_table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='migration_version'"
    ).fetchone() is not None
    applied_versions = (
        {
            int(row["version"] if isinstance(row, sqlite3.Row) else row[0])
            for row in connection.execute("SELECT version FROM migration_version")
        }
        if has_version_table
        else set()
    )

    for migration in migrations:
        version = _migration_version(migration)
        if version in applied_versions:
            continue
        connection.executescript(migration.read_text(encoding="utf-8"))
        applied_versions.add(version)


__all__ = ["run_call_store_migrations"]
