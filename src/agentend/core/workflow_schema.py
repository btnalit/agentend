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
    then: list[str] = Field(default_factory=list)
    else_: list[str] = Field(default_factory=list, alias="else")
    branches: dict[str, list[str]] = Field(default_factory=dict)


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
            branch_targets = [*node.then, *node.else_]
            for branch in node.branches.values():
                branch_targets.extend(branch)
            missing_branches = [target for target in branch_targets if target not in seen]
            if missing_branches:
                raise ValueError(f"node {node.id} branches to unknown nodes: {missing_branches}")
        final_nodes = [node for node in self.nodes if node.type == "final"]
        if len(final_nodes) != 1:
            raise ValueError("workflow must contain exactly one final node")
        return self


def load_workflow_yaml(content: str) -> WorkflowDefinition:
    raw = yaml.safe_load(content) or {}
    return WorkflowDefinition.model_validate(raw)
