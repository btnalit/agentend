from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from agentend.db.models import Base


def database_path(home: Path) -> Path:
    return home.expanduser().resolve() / "data" / "agentend.sqlite"


def create_sqlite_engine(home: Path) -> Engine:
    path = database_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", future=True)


def init_database(home: Path) -> Path:
    engine = create_sqlite_engine(home)
    Base.metadata.create_all(engine)
    _ensure_incremental_columns(engine)
    return database_path(home)


def _ensure_incremental_columns(engine: Engine) -> None:
    additions = {
        "tasks": {
            "source_hash": "VARCHAR(64)",
            "batch_id": "VARCHAR(36)",
            "run_mode": "VARCHAR(32) NOT NULL DEFAULT 'normal'",
            "retry_after_at": "DATETIME",
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
