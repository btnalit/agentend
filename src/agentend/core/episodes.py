from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.db.models import Artifact, Episode, EpisodeArtifact, EpisodeTool, ReplanSuggestion, Run, SkillDraft, ToolCall


def summarize_run(home: Path, session: Session, run_id: str) -> Episode:
    run = session.get(Run, run_id)
    if run is None:
        raise ValueError(f"Unknown run: {run_id}")
    existing = session.execute(select(Episode).where(Episode.run_id == run_id)).scalars().first()
    episode = existing or Episode(id=str(uuid4()), run_id=run_id)
    if existing is not None:
        _clear_episode_children(session, episode.id)

    tool_calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
    artifacts = session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)).scalars().all()
    suggestion = (
        session.execute(select(ReplanSuggestion).where(ReplanSuggestion.run_id == run_id).order_by(ReplanSuggestion.created_at.desc()))
        .scalars()
        .first()
    )
    result = _load_json(run.result_json)
    goal = _load_json(run.input_json).get("input", "")
    episode.workflow_id = run.workflow_id
    episode.skill_id = _skill_id_from_workflow(run.workflow_id)
    episode.status = run.status
    episode.goal = str(goal)
    episode.title = f"{run.workflow_id or 'run'}: {str(goal)[:80]}"
    episode.error = run.error
    episode.replan_suggestion_json = suggestion.suggestion_json if suggestion is not None else "{}"
    episode.metrics_json = json.dumps(
        {
            "tool_calls": len(tool_calls),
            "artifacts": len(artifacts),
            "has_error": bool(run.error),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    episode.summary = _summary(run, result, tool_calls, artifacts, suggestion)
    if existing is None:
        session.add(episode)

    for call in tool_calls:
        session.add(
            EpisodeTool(
                id=str(uuid4()),
                episode_id=episode.id,
                tool_call_id=call.id,
                tool_name=call.tool_name,
                status=call.status,
                error=call.error,
            )
        )
    for artifact in artifacts:
        session.add(
            EpisodeArtifact(
                id=str(uuid4()),
                episode_id=episode.id,
                artifact_id=artifact.id,
                path=artifact.path,
                kind=artifact.kind,
            )
        )
    record_event(session, "episode.created", {"episode_id": episode.id, "run_id": run_id}, run_id=run_id)
    return episode


def episode_to_dict(session: Session, episode: Episode) -> dict:
    tools = session.execute(select(EpisodeTool).where(EpisodeTool.episode_id == episode.id).order_by(EpisodeTool.created_at)).scalars().all()
    artifacts = (
        session.execute(select(EpisodeArtifact).where(EpisodeArtifact.episode_id == episode.id).order_by(EpisodeArtifact.created_at))
        .scalars()
        .all()
    )
    return {
        "id": episode.id,
        "run_id": episode.run_id,
        "status": episode.status,
        "goal": episode.goal,
        "workflow_id": episode.workflow_id,
        "skill_id": episode.skill_id,
        "summary": episode.summary,
        "error": episode.error,
        "replan_suggestion": _load_json(episode.replan_suggestion_json),
        "tools": [{"name": row.tool_name, "status": row.status, "error": row.error} for row in tools],
        "artifacts": [{"path": row.path, "kind": row.kind} for row in artifacts],
        "metrics": _load_json(episode.metrics_json),
    }


def promote_episode_to_skill(home: Path, session: Session, episode_id: str, skill_id: str) -> SkillDraft:
    episode = session.get(Episode, episode_id)
    if episode is None:
        raise ValueError(f"Unknown episode: {episode_id}")
    if episode.status != "completed":
        raise ValueError("Only completed episodes can be promoted")
    if session.get(SkillDraft, skill_id) is not None:
        raise ValueError(f"Skill draft already exists: {skill_id}")

    tools = session.execute(select(EpisodeTool).where(EpisodeTool.episode_id == episode_id).order_by(EpisodeTool.created_at)).scalars().all()
    artifacts = (
        session.execute(select(EpisodeArtifact).where(EpisodeArtifact.episode_id == episode_id).order_by(EpisodeArtifact.created_at))
        .scalars()
        .all()
    )
    draft_dir = home / "data" / "skill_drafts" / skill_id
    (draft_dir / "examples").mkdir(parents=True, exist_ok=True)
    (draft_dir / "evals").mkdir(parents=True, exist_ok=True)
    required_tools = sorted({tool.tool_name for tool in tools})
    manifest = {
        "id": skill_id,
        "version": "0.1.0",
        "description": f"Draft promoted from episode {episode_id}: {episode.goal or episode.summary[:120]}",
        "triggers": _trigger_terms(episode.goal),
        "workflow": "workflow.yaml",
        "required_tools": required_tools,
        "required_mcp": [],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "enabled": False,
        "source": {"type": "episode", "episode_id": episode_id},
    }
    workflow = {
        "id": f"skill.{skill_id}",
        "name": skill_id,
        "description": f"Draft workflow generated from episode {episode_id}.",
        "nodes": _draft_workflow_nodes(required_tools),
    }
    metadata = {
        "source_episode_id": episode_id,
        "source_run_id": episode.run_id,
        "workflow_id": episode.workflow_id,
        "tools": required_tools,
        "artifacts": [artifact.path for artifact in artifacts],
        "risks": ["Review this draft before enabling. It was generated from one successful episode."],
    }
    (draft_dir / "skill.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (draft_dir / "workflow.yaml").write_text(yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (draft_dir / "README.md").write_text(_draft_readme(skill_id, episode, required_tools, artifacts), encoding="utf-8")
    (draft_dir / "examples" / "input.json").write_text(
        json.dumps({"task": episode.goal or "Run this promoted skill."}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (draft_dir / "evals" / "smoke.json").write_text(
        json.dumps(
            {
                "suite": "skills-smoke",
                "skill_id": skill_id,
                "source_episode_id": episode_id,
                "input": {"task": episode.goal or "Run this promoted skill."},
                "assertions": [{"name": "skill workflow returns output", "type": "non_empty_output"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    row = SkillDraft(
        id=skill_id,
        source_episode_id=episode_id,
        path=str(draft_dir),
        status="draft",
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )
    session.add(row)
    record_event(session, "episode.promoted_to_skill_draft", {"episode_id": episode_id, "skill_id": skill_id, "path": str(draft_dir)}, run_id=episode.run_id)
    return row


def _clear_episode_children(session: Session, episode_id: str) -> None:
    for row in session.execute(select(EpisodeTool).where(EpisodeTool.episode_id == episode_id)).scalars().all():
        session.delete(row)
    for row in session.execute(select(EpisodeArtifact).where(EpisodeArtifact.episode_id == episode_id)).scalars().all():
        session.delete(row)


def _summary(
    run: Run,
    result: dict,
    tool_calls: list[ToolCall],
    artifacts: list[Artifact],
    suggestion: ReplanSuggestion | None,
) -> str:
    if run.status == "completed":
        content = str(result.get("content", ""))[:240]
        return f"Run completed with {len(tool_calls)} tool calls and {len(artifacts)} artifacts. {content}".strip()
    if suggestion is not None:
        payload = _load_json(suggestion.suggestion_json)
        return f"Run failed: {run.error}. Replanner suggests {payload.get('action')}: {payload.get('reason')}"
    return f"Run {run.status}: {run.error or result.get('error', '')}"


def _skill_id_from_workflow(workflow_id: str | None) -> str | None:
    if workflow_id and workflow_id.startswith("skill."):
        return workflow_id.removeprefix("skill.")
    return None


def _load_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _trigger_terms(goal: str) -> list[str]:
    terms = [item.strip() for item in goal.replace(",", " ").split() if item.strip()]
    return terms[:5] or ["promoted", "episode"]


def _draft_readme(skill_id: str, episode: Episode, tools: list[str], artifacts: list[EpisodeArtifact]) -> str:
    artifact_lines = "\n".join(f"- {artifact.path}" for artifact in artifacts) or "- None"
    tool_lines = "\n".join(f"- {tool}" for tool in tools) or "- None"
    return f"""# {skill_id}

Draft skill generated from episode `{episode.id}`.

## Goal

{episode.goal or "No goal captured."}

## Summary

{episode.summary}

## Tools

{tool_lines}

## Artifacts

{artifact_lines}

## Review Notes

- This draft is not enabled automatically.
- Review `skill.yaml`, `workflow.yaml`, examples, and evals before installing.
"""


def _draft_workflow_nodes(required_tools: list[str]) -> list[dict]:
    nodes: list[dict] = []
    previous_ids: list[str] = []
    for index, tool_name in enumerate(required_tools):
        node_id = f"tool_{index + 1}"
        node = {
            "id": node_id,
            "type": "tool",
            "tool": tool_name,
            "input": _draft_tool_input(tool_name),
        }
        if previous_ids:
            node["depends_on"] = previous_ids[-1:]
        nodes.append(node)
        previous_ids.append(node_id)

    answer = {
        "id": "answer",
        "type": "llm",
        "prompt": "Reuse the promoted episode pattern for this task: {input}",
    }
    if previous_ids:
        answer["depends_on"] = previous_ids[-1:]
    nodes.append(answer)
    nodes.append({"id": "final", "type": "final", "depends_on": ["answer"]})
    return nodes


def _draft_tool_input(tool_name: str) -> dict:
    if tool_name in {"fs.write_text", "file.write_text"}:
        return {"path": "promoted-output.txt", "content": "Promoted skill input: {input}"}
    if tool_name == "fs.list":
        return {"path": "."}
    if tool_name == "fs.glob":
        return {"pattern": "*"}
    if tool_name in {"fs.read_text", "file.read_text"}:
        return {"path": "agent.md"}
    if tool_name == "shell.run":
        return {"command": "echo {input}"}
    if tool_name == "git.status":
        return {"cwd": "."}
    if tool_name == "web.search":
        return {"query": "{input}", "provider": "fake", "limit": 1}
    if tool_name == "web.fetch":
        return {"url": "https://example.com/search/1"}
    if tool_name == "python.exec":
        return {"code": "print('promoted skill')"}
    if tool_name == "tools.discover":
        return {"query": "{input}"}
    return {}
