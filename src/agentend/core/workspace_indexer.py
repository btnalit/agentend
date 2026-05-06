from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agentend.db.models import WorkspaceIndex

INDEX_FILES = ["AGENTS.md", "README.md", "CONTEXT.md", "pyproject.toml", "package.json"]


def index_workspace(home: Path, session: Session) -> list[WorkspaceIndex]:
    session.execute(delete(WorkspaceIndex))
    rows: list[WorkspaceIndex] = []
    for name in INDEX_FILES:
        path = home / name
        if not path.exists() or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        row = WorkspaceIndex(
            id=str(uuid4()),
            source_path=name,
            kind=path.suffix.lstrip(".") or "file",
            summary=content[:2000],
            content_hash=sha256(content.encode("utf-8")).hexdigest(),
        )
        session.add(row)
        rows.append(row)
    docs = home / "docs"
    if docs.exists():
        for path in sorted(docs.rglob("*.md"))[:20]:
            content = path.read_text(encoding="utf-8", errors="ignore")
            rel = str(path.relative_to(home))
            row = WorkspaceIndex(
                id=str(uuid4()),
                source_path=rel,
                kind="markdown",
                summary=content[:1000],
                content_hash=sha256(content.encode("utf-8")).hexdigest(),
            )
            session.add(row)
            rows.append(row)
    return rows


def workspace_summary(session: Session) -> list[WorkspaceIndex]:
    return session.execute(select(WorkspaceIndex).order_by(WorkspaceIndex.source_path)).scalars().all()
