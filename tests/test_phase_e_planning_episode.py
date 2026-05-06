import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Episode, EpisodeArtifact, EpisodeTool, ReplanSuggestion, Run, Skill, SkillDraft
from agentend.db.session import session_scope


def _run_id_from_output(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_goal_analyzer_cli_and_tool_recommend_from_capability_map(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    (tmp_path / "README.md").write_text("# Demo Project\n\nUse pytest for tests.\n", encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    research = runner.invoke(app, ["goal", "analyze", "帮我调研浏览器自动化工具", "--home", str(home)])
    code = runner.invoke(
        app,
        ["tools", "test", "goal.analyze", "--home", str(home), "--input", '{"text":"帮我跑测试并修复代码"}'],
    )
    chatted = runner.invoke(app, ["chat", "--home", str(home), "--message", "帮我调研浏览器自动化工具"])

    assert research.exit_code == 0
    research_payload = json.loads(research.output)
    assert research_payload["goal"] == "帮我调研浏览器自动化工具"
    assert "research.report" in research_payload["candidate_skills"]
    assert "web.search" in research_payload["candidate_tools"]
    assert code.exit_code == 0
    code_payload = json.loads(code.output)
    assert "code.local_task" in code_payload["candidate_skills"]
    assert "shell.run" in code_payload["candidate_tools"]
    assert chatted.exit_code == 0
    chat_run_id = _run_id_from_output(chatted.output)
    with session_scope(home) as session:
        run = session.get(Run, chat_run_id)
        result = json.loads(run.result_json)
        assert "research.report" in result["goal_analysis"]["candidate_skills"]


def test_replanner_tool_and_workflow_failure_records_suggestion(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow_dir = home / "workflows" / "definitions"
    (workflow_dir / "bad_tool.yaml").write_text(
        """id: bad_tool
name: Bad Tool
nodes:
  - id: call_missing
    type: tool
    tool: missing.tool
  - id: final
    type: final
    depends_on: [call_missing]
""",
        encoding="utf-8",
    )

    direct = runner.invoke(
        app,
        [
            "tools",
            "test",
            "plan.replan",
            "--home",
            str(home),
            "--input",
            '{"failed_step":"web.search","error_code":"missing_config","error":"provider missing"}',
        ],
    )
    failed = runner.invoke(app, ["workflows", "run", "bad_tool", "--home", str(home), "--input", "x"])

    assert direct.exit_code == 0
    direct_payload = json.loads(direct.output)
    assert direct_payload["action"] in {"ask_user", "alternative_tool"}
    assert "provider" in direct_payload["reason"]
    assert failed.exit_code == 1
    run_id = _run_id_from_output(failed.output)
    with session_scope(home) as session:
        suggestion = session.execute(
            select(ReplanSuggestion).where(ReplanSuggestion.run_id == run_id)
        ).scalar_one()
        assert suggestion.failed_step == "call_missing"
        assert suggestion.error_code == "tool_not_found"
        payload = json.loads(suggestion.suggestion_json)
        assert payload["action"] == "alternative_tool"
        assert payload["alternative_tool"] == "tools.discover"

    summarized = runner.invoke(app, ["episodes", "summarize", run_id, "--home", str(home)])
    assert summarized.exit_code == 0
    episode_id = re.search(r"Episode:\s+([0-9a-f-]+)", summarized.output).group(1)
    shown = runner.invoke(app, ["episodes", "show", episode_id, "--home", str(home)])
    assert shown.exit_code == 0
    failed_episode = json.loads(shown.output)
    assert failed_episode["status"] == "failed"
    assert failed_episode["replan_suggestion"]["alternative_tool"] == "tools.discover"


def test_episode_logger_summarizes_tool_artifacts(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    wrote = runner.invoke(
        app,
        ["tools", "test", "fs.write_text", "--home", str(home), "--input", '{"path":"out.txt","content":"hello"}'],
    )
    assert wrote.exit_code == 0
    with session_scope(home) as session:
        tool_run = session.execute(select(Run).where(Run.workflow_id == "tools.test")).scalar_one()

    summarized = runner.invoke(app, ["episodes", "summarize", tool_run.id, "--home", str(home)])
    assert summarized.exit_code == 0
    episode_id = re.search(r"Episode:\s+([0-9a-f-]+)", summarized.output).group(1)
    listed = runner.invoke(app, ["episodes", "list", "--home", str(home)])
    shown = runner.invoke(app, ["episodes", "show", episode_id, "--home", str(home)])

    assert tool_run.id in listed.output
    assert "fs.write_text" in shown.output
    assert "out.txt" in shown.output
    with session_scope(home) as session:
        episode = session.get(Episode, episode_id)
        assert episode is not None
        assert episode.status == "completed"
        assert session.execute(select(EpisodeTool).where(EpisodeTool.episode_id == episode_id)).first() is not None
        assert session.execute(select(EpisodeArtifact).where(EpisodeArtifact.episode_id == episode_id)).first() is not None


def test_episode_to_skill_promotes_successful_episode_as_disabled_draft(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    wrote = runner.invoke(
        app,
        ["tools", "test", "fs.write_text", "--home", str(home), "--input", '{"path":"out.txt","content":"hello"}'],
    )
    assert wrote.exit_code == 0
    with session_scope(home) as session:
        run = session.execute(select(Run).where(Run.workflow_id == "tools.test")).scalar_one()
    summarized = runner.invoke(app, ["episodes", "summarize", run.id, "--home", str(home)])
    episode_id = re.search(r"Episode:\s+([0-9a-f-]+)", summarized.output).group(1)

    promoted = runner.invoke(app, ["episodes", "promote", episode_id, "--home", str(home), "--skill-id", "demo.promoted"])
    draft_dir = home / "data" / "skill_drafts" / "demo.promoted"
    validated = runner.invoke(app, ["skills", "validate", "--home", str(home), "--path", str(draft_dir)])

    assert promoted.exit_code == 0
    assert str(draft_dir) in promoted.output
    assert (draft_dir / "skill.yaml").exists()
    assert (draft_dir / "workflow.yaml").exists()
    assert (draft_dir / "README.md").exists()
    assert (draft_dir / "examples" / "input.json").exists()
    assert (draft_dir / "evals" / "smoke.json").exists()
    assert validated.exit_code == 0
    assert "OK demo.promoted" in validated.output
    with session_scope(home) as session:
        draft = session.get(SkillDraft, "demo.promoted")
        skill = session.get(Skill, "demo.promoted")
        assert draft is not None
        assert draft.status == "draft"
        assert draft.source_episode_id == episode_id
        assert skill is None


def test_episode_to_skill_rejects_failed_episode(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow_dir = home / "workflows" / "definitions"
    (workflow_dir / "bad_tool.yaml").write_text(
        """id: bad_tool
name: Bad Tool
nodes:
  - id: call_missing
    type: tool
    tool: missing.tool
  - id: final
    type: final
    depends_on: [call_missing]
""",
        encoding="utf-8",
    )
    failed = runner.invoke(app, ["workflows", "run", "bad_tool", "--home", str(home), "--input", "x"])
    run_id = _run_id_from_output(failed.output)
    summarized = runner.invoke(app, ["episodes", "summarize", run_id, "--home", str(home)])
    episode_id = re.search(r"Episode:\s+([0-9a-f-]+)", summarized.output).group(1)

    promoted = runner.invoke(app, ["episodes", "promote", episode_id, "--home", str(home), "--skill-id", "demo.failed"])

    assert promoted.exit_code == 1
    assert "Only completed episodes can be promoted" in promoted.output
