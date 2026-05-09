import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from agentend.db.models import Base


def database_path(home: Path) -> Path:
    return home.expanduser().resolve() / "data" / "agentend.sqlite"


def create_sqlite_engine(home: Path) -> Engine:
    path = database_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30}, future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def init_database(home: Path) -> Path:
    resolved_home = home.expanduser().resolve()
    resolved_home.mkdir(parents=True, exist_ok=True)
    with _init_file_lock(resolved_home):
        engine = create_sqlite_engine(resolved_home)
        _with_sqlite_retry(lambda: Base.metadata.create_all(engine))
        _with_sqlite_retry(lambda: _ensure_incremental_columns(engine))
    return database_path(home)


@contextmanager
def _init_file_lock(home: Path, *, timeout_seconds: float = 30.0, stale_seconds: float = 120.0) -> Iterator[None]:
    lock_path = home / ".agentend-init.lock"
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} created_at={time.time()}\n".encode("utf-8"))
        except FileExistsError:
            if _lock_is_stale(lock_path, stale_seconds):
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for AgentEnd init lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _lock_is_stale(lock_path: Path, stale_seconds: float) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age > stale_seconds


def _with_sqlite_retry(operation):
    delay = 0.05
    last_error: Exception | None = None
    for _ in range(6):
        try:
            return operation()
        except Exception as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "database table is locked" not in message:
                raise
            last_error = exc
            time.sleep(delay)
            delay = min(delay * 2, 1.0)
    if last_error is not None:
        raise last_error


def _ensure_incremental_columns(engine: Engine) -> None:
    additions = {
        "tasks": {
            "agent_run_id": "VARCHAR(36)",
            "source_hash": "VARCHAR(64)",
            "batch_id": "VARCHAR(36)",
            "run_mode": "VARCHAR(32) NOT NULL DEFAULT 'normal'",
            "retry_after_at": "DATETIME",
            "heartbeat_at": "DATETIME",
            "progress_artifact_id": "VARCHAR(36)",
            "claimed_at": "DATETIME",
            "worker_id": "VARCHAR(64)",
            "resume_cursor_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "schedules": {
            "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            "max_consecutive_failures": "INTEGER NOT NULL DEFAULT 3",
            "paused_reason": "TEXT",
            "last_error": "TEXT",
        },
        "storage_cleanup_runs": {
            "plan_id": "VARCHAR(36)",
            "source_plan_id": "VARCHAR(36)",
            "status": "VARCHAR(32) NOT NULL DEFAULT 'completed'",
            "rules_json": "TEXT NOT NULL DEFAULT '[]'",
            "total_bytes": "INTEGER NOT NULL DEFAULT 0",
            "deleted_count": "INTEGER NOT NULL DEFAULT 0",
            "error": "TEXT",
        },
        "context_pack_items": {
            "source_type": "VARCHAR(64) NOT NULL DEFAULT 'generated'",
            "trust_level": "VARCHAR(64) NOT NULL DEFAULT 'generated'",
            "allowed_use_json": "TEXT NOT NULL DEFAULT '[]'",
            "can_override_policy": "VARCHAR(8) NOT NULL DEFAULT 'false'",
        },
        "context_dropped_items": {
            "source_type": "VARCHAR(64) NOT NULL DEFAULT 'generated'",
            "trust_level": "VARCHAR(64) NOT NULL DEFAULT 'generated'",
            "allowed_use_json": "TEXT NOT NULL DEFAULT '[]'",
            "can_override_policy": "VARCHAR(8) NOT NULL DEFAULT 'false'",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {
                str(row._mapping["name"])
                for row in connection.execute(text(f"PRAGMA table_info({table})")).all()
            }
            for name, definition in columns.items():
                if name in existing:
                    continue
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def create_session_factory(home: Path) -> sessionmaker[Session]:
    engine = create_sqlite_engine(home)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


@contextmanager
def session_scope(home: Path) -> Iterator[Session]:
    factory = create_session_factory(home)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
