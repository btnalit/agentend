import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_selector import select_next_action_with_trace
from agentend.core.effectiveness import record_effectiveness_event
from agentend.db.models import AgentIteration
from agentend.db.session import session_scope


def test_selector_trace_records_top_candidates_and_score_breakdown(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["agent", "run", "--home", str(home), "Read README and identify pytest command."])

    assert result.exit_code == 0, result.output
    with session_scope(home) as session:
        iteration = session.execute(select(AgentIteration)).scalar_one()
        plan = json.loads(iteration.plan_json)
        trace = plan["selector_trace"]
        assert trace["selected"]["name"]
        assert trace["goal_type"] in {"code", "workspace"}
        assert len(trace["candidates"]) >= 2
        top = trace["candidates"][0]
        assert top["score_breakdown"]
        assert "base" in top["score_breakdown"]
        assert "input_fit" in top["score_breakdown"]
        assert "effectiveness" in top["score_breakdown"]
        assert "rejected_reasons" in top


def test_selector_recent_failures_can_override_old_success_aggregate(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with session_scope(home) as session:
        for _ in range(6):
            record_effectiveness_event(
                session,
                capability_type="skill",
                capability_id="file.workspace_ops",
                goal_type="workspace",
                status="success",
            )
        for _ in range(4):
            record_effectiveness_event(
                session,
                capability_type="skill",
                capability_id="file.workspace_ops",
                goal_type="workspace",
                status="failure",
                error_code="recent_failure",
            )
        record_effectiveness_event(
            session,
            capability_type="skill",
            capability_id="code.local_task",
            goal_type="workspace",
            status="success",
        )

        result = select_next_action_with_trace(
            home,
            session,
            "Read project files and identify pytest command.",
            {"candidate_skills": ["file.workspace_ops", "code.local_task"]},
            [],
        )

    assert result.selected.name == "code.local_task"
    file_candidate = next(item for item in result.trace["candidates"] if item["name"] == "file.workspace_ops")
    assert file_candidate["score_breakdown"]["recent_failure_penalty"] < 0
    assert "recent failures" in " ".join(file_candidate["rejected_reasons"])
