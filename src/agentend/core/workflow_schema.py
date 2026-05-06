from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


NodeType = Literal["llm", "tool", "condition", "parallel", "human_input", "workflow_call", "final"]


class WorkflowNode(BaseModel):
    id: str
    type: NodeType
    prompt: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    tool: str | None = None
    workflow: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinition(BaseModel):
    id: str
    name: str
    description: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    nodes: list[WorkflowNode]

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        if not self.nodes:
            raise ValueError("nodes must contain at least one node")
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"duplicate node id: {node.id}")
            seen.add(node.id)
        for node in self.nodes:
            missing = [dep for dep in node.depends_on if dep not in seen]
            if missing:
                raise ValueError(f"node {node.id} depends on unknown nodes: {missing}")
        return self


def load_workflow_yaml(content: str) -> WorkflowDefinition:
    raw = yaml.safe_load(content) or {}
    return WorkflowDefinition.model_validate(raw)
