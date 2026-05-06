from __future__ import annotations

from pathlib import Path


def resolve_home_child(home: Path, value: object, *, label: str = "path") -> Path:
    """Resolve a user path that must stay inside AgentEnd home."""
    root = home.expanduser().resolve()
    raw = Path(str(value))
    if raw.is_absolute():
        raise ValueError(f"{label} must be relative to AgentEnd home")
    if ".." in raw.parts:
        raise ValueError(f"{label} must not contain '..'")
    resolved = (root / raw).resolve()
    if not _is_relative_to(resolved, root):
        raise ValueError(f"{label} must stay inside AgentEnd home")
    return resolved


def safe_artifact_path(home: Path, run_id: str, requested: object, *, label: str = "path") -> Path:
    raw = Path(str(requested))
    if raw.is_absolute():
        raise ValueError(f"{label} must be relative to the run artifact directory")
    if ".." in raw.parts:
        raise ValueError(f"{label} must not contain '..'")
    safe_name = raw.name or "artifact"
    root = (home.expanduser().resolve() / "data" / "artifacts" / run_id).resolve()
    resolved = (root / safe_name).resolve()
    if not _is_relative_to(resolved, root):
        raise ValueError(f"{label} must stay inside the run artifact directory")
    return resolved


def ensure_not_root(path: Path, root: Path, *, label: str = "path") -> None:
    if path.resolve() == root.expanduser().resolve():
        raise ValueError(f"{label} must not be the AgentEnd home root")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
