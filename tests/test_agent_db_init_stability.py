from pathlib import Path

from sqlalchemy import select, text

from agentend.core.skills import ensure_builtin_skills
from agentend.db.models import Skill
from agentend.db.session import create_session_factory, create_sqlite_engine, init_database


def test_sqlite_engine_uses_busy_timeout(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "agentend-home")
    with engine.connect() as connection:
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert busy_timeout >= 30000


def test_builtin_skill_registration_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    factory = create_session_factory(home)
    with factory() as session:
        ensure_builtin_skills(home, session)
        ensure_builtin_skills(home, session)
        session.commit()

    with factory() as session:
        rows = session.execute(select(Skill)).scalars().all()
        ids = [row.id for row in rows]

    assert len(ids) == len(set(ids))
    assert "code.local_task" in ids
