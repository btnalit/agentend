from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import (
    Checkpoint,
    EpisodeArtifact,
    ExtensionRecord,
    Skill,
    SkillDraft,
    StorageCleanupRun,
    StorageRetentionRule,
)
from agentend.db.session import database_path


RETENTION_CATEGORIES = (
    "artifacts",
    "sandboxes",
    "exports",
    "cache",
    "skill_drafts",
    "checkpoints",
)


def storage_usage(home: Path) -> list[dict[str, object]]:
    resolved_home = home.expanduser().resolve()
    roots = [
        database_path(resolved_home),
        resolved_home / "data" / "artifacts",
        resolved_home / "data" / "sandboxes",
        resolved_home / "data" / "eval_exports",
        resolved_home / "data" / "exports",
        resolved_home / "data" / "cache",
        resolved_home / "data" / "skill_drafts",
        resolved_home / "skills" / "market-cache",
    ]
    return [{"path": str(path), "name": path.name, "size_bytes": _path_size(path)} for path in roots]


def build_cleanup_plan(home: Path, session: Session, *, older_than: str) -> dict[str, object]:
    resolved_home = home.expanduser().resolve()
    age = parse_age_threshold(older_than)
    now = datetime.now(timezone.utc)
    cutoff = now - age
    rules = _retention_rules(session, older_than)
    protected_paths = _protected_paths(session, resolved_home)

    items: list[dict[str, object]] = []
    items.extend(
        _old_files(
            resolved_home / "data" / "artifacts",
            category="artifacts",
            rule_id="artifacts-old",
            reason=f"artifact older than {older_than}",
            cutoff=cutoff,
            protected_paths=protected_paths,
        )
    )
    items.extend(
        _old_children(
            resolved_home / "data" / "sandboxes",
            category="sandboxes",
            rule_id="sandboxes-old",
            reason=f"sandbox older than {older_than}",
            cutoff=cutoff,
            protected_paths=protected_paths,
        )
    )
    for export_root in (resolved_home / "data" / "eval_exports", resolved_home / "data" / "exports"):
        items.extend(
            _old_children(
                export_root,
                category="exports",
                rule_id="exports-old",
                reason=f"export older than {older_than}",
                cutoff=cutoff,
                protected_paths=protected_paths,
            )
        )
    items.extend(
        _old_children(
            resolved_home / "data" / "cache",
            category="cache",
            rule_id="cache-old",
            reason=f"cache entry older than {older_than}",
            cutoff=cutoff,
            protected_paths=protected_paths,
        )
    )
    items.extend(
        _old_children(
            resolved_home / "skills" / "market-cache",
            category="cache",
            rule_id="market-cache-old",
            reason=f"market cache entry older than {older_than}",
            cutoff=cutoff,
            protected_paths=protected_paths,
        )
    )
    items.extend(
        _old_children(
            resolved_home / "data" / "skill_drafts",
            category="skill_drafts",
            rule_id="skill-drafts-old",
            reason=f"skill draft older than {older_than}",
            cutoff=cutoff,
            protected_paths=protected_paths,
        )
    )
    items.extend(_old_checkpoints(session, cutoff=cutoff, older_than=older_than))

    items = _dedupe_items(items)
    return {
        "plan_id": str(uuid4()),
        "older_than": older_than,
        "created_at": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "rules": rules,
        "items": items,
        "total_bytes": sum(int(item.get("size_bytes", 0)) for item in items),
    }


def execute_cleanup_plan(home: Path, session: Session, items: list[dict[str, object]]) -> list[dict[str, object]]:
    resolved_home = home.expanduser().resolve()
    protected_paths = _protected_paths(session, resolved_home)
    results: list[dict[str, object]] = []
    for item in items:
        result = dict(item)
        try:
            kind = str(item.get("kind", ""))
            if kind == "file":
                path = Path(str(item["path"]))
                if _is_protected(path, protected_paths):
                    result["status"] = "protected"
                    results.append(result)
                    continue
                _delete_file(path, resolved_home)
                result["status"] = "deleted"
            elif kind == "directory":
                path = Path(str(item["path"]))
                if _is_protected(path, protected_paths):
                    result["status"] = "protected"
                    results.append(result)
                    continue
                _delete_directory(path, resolved_home)
                result["status"] = "deleted"
            elif kind == "db_row":
                _delete_db_row(session, item)
                result["status"] = "deleted"
            else:
                raise ValueError(f"Unsupported cleanup item kind: {kind}")
        except FileNotFoundError:
            result["status"] = "missing"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
        results.append(result)
    _prune_empty_directories(resolved_home / "data" / "artifacts", resolved_home)
    return results


def record_cleanup_run(
    session: Session,
    *,
    mode: str,
    plan: dict[str, object],
    results: list[dict[str, object]] | None = None,
    source_plan_id: str | None = None,
    status: str = "completed",
) -> StorageCleanupRun:
    items = results if results is not None else list(plan.get("items", []))
    deleted_count = sum(1 for item in items if item.get("status", "planned") == "deleted")
    total_bytes = sum(int(item.get("size_bytes", 0)) for item in items)
    row = StorageCleanupRun(
        id=str(uuid4()),
        mode=mode,
        plan_id=str(plan["plan_id"]),
        source_plan_id=source_plan_id,
        status=status,
        deleted_json=json.dumps(items, ensure_ascii=False, sort_keys=True),
        rules_json=json.dumps(plan.get("rules", []), ensure_ascii=False, sort_keys=True),
        total_bytes=total_bytes,
        deleted_count=deleted_count,
    )
    session.add(row)
    return row


def load_cleanup_plan(session: Session, plan_id: str) -> dict[str, object]:
    row = (
        session.execute(
            select(StorageCleanupRun)
            .where(StorageCleanupRun.plan_id == plan_id)
            .where(StorageCleanupRun.mode == "dry-run")
            .order_by(StorageCleanupRun.created_at.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        raise ValueError(f"Unknown dry-run cleanup plan: {plan_id}")
    return {
        "plan_id": plan_id,
        "older_than": "",
        "created_at": row.created_at.isoformat(),
        "cutoff": "",
        "rules": json.loads(row.rules_json or "[]"),
        "items": json.loads(row.deleted_json or "[]"),
        "total_bytes": row.total_bytes,
    }


def parse_age_threshold(value: str) -> timedelta:
    stripped = value.strip().lower()
    if len(stripped) < 2:
        raise ValueError("Age threshold must look like 30d, 12h, or 60m")
    unit = stripped[-1]
    amount_text = stripped[:-1]
    if not amount_text.isdigit():
        raise ValueError("Age threshold amount must be an integer")
    amount = int(amount_text)
    if amount < 0:
        raise ValueError("Age threshold must be zero or greater")
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    raise ValueError("Age threshold unit must be one of d, h, or m")


def _retention_rules(session: Session, older_than: str) -> list[dict[str, str]]:
    rows = session.execute(select(StorageRetentionRule)).scalars().all()
    existing = {row.id for row in rows}
    for category in RETENTION_CATEGORIES:
        rule_id = f"default:{category}"
        if rule_id not in existing:
            row = StorageRetentionRule(
                id=rule_id,
                category=category,
                older_than=older_than,
                enabled="true",
                action="delete",
                reason=f"default {category} retention",
            )
            session.add(row)
            rows.append(row)
    return [
        {
            "id": row.id,
            "category": row.category,
            "older_than": older_than,
            "enabled": row.enabled,
            "action": row.action,
            "reason": row.reason,
        }
        for row in rows
        if row.enabled == "true"
    ]


def _old_files(
    root: Path,
    *,
    category: str,
    rule_id: str,
    reason: str,
    cutoff: datetime,
    protected_paths: set[Path],
) -> list[dict[str, object]]:
    if not root.exists():
        return []
    items: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_protected(path, protected_paths):
            continue
        mtime = _mtime(path)
        if mtime >= cutoff:
            continue
        items.append(_path_item(path, kind="file", category=category, rule_id=rule_id, reason=reason, mtime=mtime))
    return items


def _old_children(
    root: Path,
    *,
    category: str,
    rule_id: str,
    reason: str,
    cutoff: datetime,
    protected_paths: set[Path],
) -> list[dict[str, object]]:
    if not root.exists():
        return []
    items: list[dict[str, object]] = []
    for path in sorted(root.iterdir()):
        if _is_protected(path, protected_paths):
            continue
        mtime = _mtime(path)
        if mtime >= cutoff:
            continue
        kind = "directory" if path.is_dir() else "file"
        items.append(_path_item(path, kind=kind, category=category, rule_id=rule_id, reason=reason, mtime=mtime))
    return items


def _old_checkpoints(session: Session, *, cutoff: datetime, older_than: str) -> list[dict[str, object]]:
    rows = list(session.execute(select(Checkpoint).order_by(Checkpoint.run_id, Checkpoint.created_at.desc())).scalars().all())
    latest_by_run: set[str] = set()
    latest_ids: set[str] = set()
    for row in rows:
        if row.run_id in latest_by_run:
            continue
        latest_by_run.add(row.run_id)
        latest_ids.add(row.id)

    items: list[dict[str, object]] = []
    for row in rows:
        if row.id in latest_ids:
            continue
        created_at = _as_aware(row.created_at)
        if created_at >= cutoff:
            continue
        items.append(
            {
                "kind": "db_row",
                "category": "checkpoints",
                "table": "checkpoints",
                "row_id": row.id,
                "path": None,
                "size_bytes": 0,
                "reason": f"checkpoint older than {older_than}; latest checkpoint per run retained",
                "rule_id": "checkpoints-old",
                "created_at": created_at.isoformat(),
            }
        )
    return items


def _path_item(
    path: Path,
    *,
    kind: str,
    category: str,
    rule_id: str,
    reason: str,
    mtime: datetime,
) -> dict[str, object]:
    return {
        "kind": kind,
        "category": category,
        "path": str(path),
        "table": None,
        "row_id": None,
        "size_bytes": _path_size(path),
        "reason": reason,
        "rule_id": rule_id,
        "mtime": mtime.isoformat(),
    }


def _dedupe_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("kind", "")), str(item.get("path") or item.get("row_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _protected_paths(session: Session, home: Path) -> set[Path]:
    protected: set[Path] = set()
    for row in session.execute(select(EpisodeArtifact)).scalars().all():
        protected.add(_resolve_home_path(home, row.path))
    for row in session.execute(select(Skill).where(Skill.enabled == "true")).scalars().all():
        for raw in (row.source_location, row.workflow_path):
            if raw:
                path = _resolve_home_path(home, raw)
                protected.add(path if path.is_dir() else path.parent)
    for row in session.execute(select(ExtensionRecord).where(ExtensionRecord.status == "enabled")).scalars().all():
        if row.source:
            path = _resolve_home_path(home, row.source)
            protected.add(path if path.is_dir() else path.parent)
    return protected


def _resolve_home_path(home: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def _is_protected(path: Path, protected_paths: set[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == protected or _is_relative_to(resolved, protected) for protected in protected_paths)


def _delete_file(path: Path, home: Path) -> None:
    resolved = _validate_cleanup_path(path, home)
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    if not resolved.is_file():
        raise ValueError(f"Cleanup item is not a file: {resolved}")
    resolved.unlink()


def _delete_directory(path: Path, home: Path) -> None:
    resolved = _validate_cleanup_path(path, home)
    if not resolved.exists():
        raise FileNotFoundError(str(resolved))
    if not resolved.is_dir():
        raise ValueError(f"Cleanup item is not a directory: {resolved}")
    shutil.rmtree(resolved)


def _delete_db_row(session: Session, item: dict[str, object]) -> None:
    table = str(item.get("table", ""))
    row_id = str(item.get("row_id", ""))
    if table != "checkpoints":
        raise ValueError(f"Unsupported cleanup DB table: {table}")
    row = session.get(Checkpoint, row_id)
    if row is not None:
        session.delete(row)


def _validate_cleanup_path(path: Path, home: Path) -> Path:
    resolved = path.expanduser().resolve()
    allowed_roots = _allowed_cleanup_roots(home)
    if resolved == home or not _is_relative_to(resolved, home):
        raise ValueError(f"Cleanup path must stay inside AgentEnd home: {resolved}")
    if not any(_is_relative_to(resolved, root) and resolved != root for root in allowed_roots):
        raise ValueError(f"Cleanup path is outside managed cleanup roots: {resolved}")
    return resolved


def _allowed_cleanup_roots(home: Path) -> tuple[Path, ...]:
    return (
        home / "data" / "artifacts",
        home / "data" / "sandboxes",
        home / "data" / "eval_exports",
        home / "data" / "exports",
        home / "data" / "cache",
        home / "data" / "skill_drafts",
        home / "skills" / "market-cache",
    )


def _prune_empty_directories(root: Path, home: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda item: len(item.parts), reverse=True):
        if path == root or not _is_relative_to(path.resolve(), home):
            continue
        try:
            path.rmdir()
        except OSError:
            continue


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    return 0


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
