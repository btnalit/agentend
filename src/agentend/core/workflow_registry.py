from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agentend.config import AppConfig
from agentend.core.workflow_schema import WorkflowDefinition, load_workflow_yaml


@dataclass(frozen=True)
class WorkflowLoadError:
    path: Path
    message: str


class WorkflowRegistry:
    def __init__(self, config: AppConfig):
        self.config = config
        self.workflow_dir = config.resolve_home_path(config.data.workflow_dir)

    def list_workflows(self) -> tuple[list[WorkflowDefinition], list[WorkflowLoadError]]:
        workflows: list[WorkflowDefinition] = []
        errors: list[WorkflowLoadError] = []
        for path in sorted(self.workflow_dir.glob("*.yaml")):
            try:
                workflows.append(load_workflow_yaml(path.read_text(encoding="utf-8")))
            except (ValidationError, ValueError) as exc:
                errors.append(WorkflowLoadError(path=path, message=str(exc)))
        return workflows, errors

    def get(self, workflow_id: str) -> WorkflowDefinition:
        workflows, errors = self.list_workflows()
        for workflow in workflows:
            if workflow.id == workflow_id:
                return workflow
        if errors:
            details = "; ".join(f"{error.path.name}: {error.message}" for error in errors)
            raise ValueError(f"Workflow {workflow_id!r} not found. Validation errors: {details}")
        raise ValueError(f"Workflow {workflow_id!r} not found")
