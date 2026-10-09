from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
_warn_lock = threading.Lock()
_warned_paths: set[str] = set()


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the OS handle.

    ``sqlite3.Connection.__exit__`` only commits/rolls back; it does not close
    the connection.  Repositories in this project use short-lived ``with``
    blocks, so closing on exit prevents leaked handles and Windows lock buildup.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _state_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return path, Path(str(path) + "-wal"), Path(str(path) + "-shm")


def _acquire_repair_lock(lock_path: Path, *, timeout_seconds: float) -> int:
    """Acquire a small cross-process startup lock using atomic file creation."""

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()}\ncreated={time.time()}\n".encode("ascii"))
            return fd
        except FileExistsError:
            try:
                # A crashed startup must not leave the repair path blocked forever.
                if time.time() - lock_path.stat().st_mtime > 120.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for SQLite state repair lock: {lock_path}")
            time.sleep(0.05)


def _release_repair_lock(lock_path: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_healthy_state_db(
    path: str | Path | None = None,
    *,
    lock_timeout_seconds: float = 10.0,
) -> Path | None:
    """Validate the shared state DB and quarantine it when corruption is detected.

    This is intended for process startup, before repositories keep database
    connections open.  A corrupt database is never deleted: the database plus
    any WAL/SHM sidecars are moved into a timestamped backup directory, after
    which normal repository migrations can create a fresh state database.

    Returns the quarantine directory when recovery was required, otherwise
    ``None``.
    """

    db_path = Path(path).expanduser().resolve(strict=False) if path is not None else configured_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(db_path) + ".repair.lock")
    lock_fd = _acquire_repair_lock(lock_path, timeout_seconds=lock_timeout_seconds)
    try:
        if not db_path.exists():
            orphan_sidecars = [item for item in _state_sidecars(db_path)[1:] if item.exists()]
            if orphan_sidecars:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                quarantine_dir = db_path.parent / f"corrupt_state_backup_{stamp}"
                quarantine_dir.mkdir(parents=True, exist_ok=False)
                for item in orphan_sidecars:
                    os.replace(str(item), str(quarantine_dir / item.name))
                logger.warning(
                    "sqlite_shared_state_orphan_sidecars_quarantined path=%s backup=%s files=%s",
                    db_path,
                    quarantine_dir,
                    ",".join(item.name for item in orphan_sidecars),
                )
                return quarantine_dir
            return None

        corruption: str | None = None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(db_path),
                timeout=min(2.0, max(0.1, float(lock_timeout_seconds))),
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only=ON")
            rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
            if rows != ["ok"]:
                corruption = "; ".join(rows[:8]) or "quick_check returned no result"
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                logger.info(
                    "sqlite_shared_state_health_check_busy path=%s error=%s",
                    db_path,
                    exc,
                )
                return None
            raise RuntimeError(
                f"SQLite state health check failed without a corruption signature: "
                f"path={db_path} error={type(exc).__name__}: {exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            corruption = f"{type(exc).__name__}: {exc}"
        finally:
            if connection is not None:
                connection.close()

        if corruption is None:
            return None

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        quarantine_dir = db_path.parent / f"corrupt_state_backup_{stamp}"
        quarantine_dir.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            database, wal, shm = _state_sidecars(db_path)
            # Move sidecars first and the main DB last. If anything fails, roll
            # back what was already moved so startup never leaves a half-moved
            # database layout behind.
            for item in (wal, shm, database):
                if not item.exists():
                    continue
                target = quarantine_dir / item.name
                os.replace(str(item), str(target))
                moved.append((item, target))
        except OSError as exc:
            for original, target in reversed(moved):
                try:
                    if target.exists() and not original.exists():
                        os.replace(str(target), str(original))
                except OSError:
                    pass
            raise RuntimeError(
                "SQLite state database is corrupt but could not be quarantined. "
                "Stop all Agent/MCP processes and start again. "
                f"path={db_path} error={exc}"
            ) from exc

        logger.warning(
            "sqlite_shared_state_corrupt_quarantined path=%s backup=%s files=%s reason=%s",
            db_path,
            quarantine_dir,
            ",".join(original.name for original, _ in moved),
            corruption,
        )
        return quarantine_dir
    finally:
        _release_repair_lock(lock_path, lock_fd)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(value).split('.'):
        try:
            parts.append(int(token))
        except ValueError:
            break
    return tuple(parts)


def wal_reset_fix_available_for(version: str) -> bool:
    """Return whether a SQLite version is known to contain the WAL-reset fix.

    Upstream fixed the bug in 3.51.3 and later, with official backports in
    3.50.7 and 3.44.6.
    """
    v = _version_tuple(version)
    if v >= (3, 51, 3):
        return True
    if len(v) >= 3 and v[:2] == (3, 50) and v[2] >= 7:
        return True
    if len(v) >= 3 and v[:2] == (3, 44) and v[2] >= 6:
        return True
    return False


def wal_reset_fix_available() -> bool:
    return wal_reset_fix_available_for(sqlite3.sqlite_version)


def configured_state_db_path() -> Path:
    return Path(os.environ.get("STATE_DB_PATH", str(Path("data") / "state.sqlite3"))).expanduser().resolve(strict=False)


def is_shared_state_db(path: str | Path) -> bool:
    return Path(path).expanduser().resolve(strict=False) == configured_state_db_path()


def preferred_journal_mode(path: str | Path) -> str:
    # The shared state DB is opened by several repositories and by both the
    # Agent and MCP processes.  On SQLite runtimes affected by the upstream
    # WAL-reset bug, avoid the vulnerable WAL condition while preserving WAL
    # for independent per-feature databases.
    if is_shared_state_db(path) and not wal_reset_fix_available():
        return "DELETE"
    return "WAL"


def configure_durable_journal(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    synchronous: str = "FULL",
) -> str:
    mode = preferred_journal_mode(path)
    row = connection.execute(f"PRAGMA journal_mode={mode}").fetchone()
    connection.execute(f"PRAGMA synchronous={synchronous}")
    observed = str(row[0] if row else mode).upper()
    if mode == "DELETE":
        key = str(Path(path).expanduser().resolve(strict=False))
        with _warn_lock:
            first = key not in _warned_paths
            if first:
                _warned_paths.add(key)
        if first:
            logger.info(
                "sqlite_shared_state_wal_disabled path=%s sqlite_version=%s "
                "reason=wal_reset_bug_runtime_not_fixed journal_mode=%s",
                key,
                sqlite3.sqlite_version,
                observed,
            )
    return observed


__all__ = [
    "ClosingConnection",
    "configure_durable_journal",
    "configured_state_db_path",
    "ensure_healthy_state_db",
    "is_shared_state_db",
    "preferred_journal_mode",
    "wal_reset_fix_available",
    "wal_reset_fix_available_for",
]
