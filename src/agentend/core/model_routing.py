from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.db.models import CostBudget, ModelRoute

DEFAULT_STAGES = ["goal_analyze", "context_compact", "workflow_step", "replan", "vision", "final_evaluate"]


@dataclass(frozen=True)
class RouteView:
    stage: str
    provider: str
    model: str


def list_routes(home: Path, session: Session) -> list[RouteView]:
    config = load_config(home)
    rows = {row.stage: row for row in session.query(ModelRoute).all()}
    routes = []
    for stage in DEFAULT_STAGES:
        row = rows.get(stage)
        if row is None:
            routes.append(RouteView(stage, config.llm.provider, config.llm.model))
        else:
            routes.append(RouteView(stage, row.provider, row.model))
    return routes


def set_route(session: Session, stage: str, provider: str, model: str) -> ModelRoute:
    row = session.get(ModelRoute, stage)
    if row is None:
        row = ModelRoute(stage=stage, provider=provider, model=model)
        session.add(row)
    else:
        row.provider = provider
        row.model = model
    return row


def set_budget(
    session: Session,
    workflow_id: str,
    *,
    max_llm_calls: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> CostBudget:
    row = session.get(CostBudget, workflow_id)
    if row is None:
        row = CostBudget(workflow_id=workflow_id)
        session.add(row)
    row.max_llm_calls = max_llm_calls
    row.max_input_tokens = max_input_tokens
    row.max_output_tokens = max_output_tokens
    return row
