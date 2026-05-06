from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.config import AppConfig, load_config
from agentend.db.models import CostBudget, CostUsage, ModelRoute

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


def resolve_model_route(config: AppConfig, session: Session, stage: str) -> RouteView:
    row = session.get(ModelRoute, stage)
    if row is None:
        return RouteView(stage=stage, provider=config.llm.provider, model=config.llm.model)
    return RouteView(stage=stage, provider=row.provider, model=row.model)


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


def record_cost_usage(
    session: Session,
    *,
    run_id: str,
    step_id: str | None,
    workflow_id: str | None,
    model_stage: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    usage_source: str,
) -> CostUsage:
    row = CostUsage(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        workflow_id=workflow_id,
        model_stage=model_stage,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        usage_source=usage_source,
    )
    session.add(row)
    return row
