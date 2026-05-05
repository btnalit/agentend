from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine
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
    return database_path(home)


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
