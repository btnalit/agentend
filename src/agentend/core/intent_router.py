from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from agentend.core.capabilities import refresh_capabilities
from agentend.core.skills import ensure_builtin_skills


INTENT_TYPES = {
    "chat",
    "answer",
    "task",
    "tool_action",
    "workflow_action",
    "skill_action",
    "clarification",
    "blocked",
}
ACTION_TYPES = {"skill_run", "tool_call", "workflow_run"}
RISK_LEVELS = {"low", "medium", "high"}
SOURCES = {"rule", "model", "fallback"}
HIGH_SIDE_EFFECTS = {"local_execute", "network_write", "external_write"}


@dataclass
class IntentCandidateAction:
    type: str
    name: str
    score: float = 0.0
    input_data: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.type not in ACTION_TYPES:
            raise ValueError(f"invalid candidate action type: {self.type}")
        if not self.name:
            raise ValueError("candidate action name must not be empty")
        if not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("candidate action score must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "score": float(self.score),
        }
        if self.input_data:
            payload["input_data"] = self.input_data
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass
class IntentDecision:
    intent_type: str
    goal: str
    confidence: float = 0.0
    slots: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    candidate_actions: list[IntentCandidateAction | dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    risk_level: str = "low"
    risk_notes: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    routing_reason: str = ""
    source: str = "rule"
    schema_version: str = "1"
    model_provider: str | None = None
    model_model: str | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.intent_type not in INTENT_TYPES:
            raise ValueError(f"invalid intent_type: {self.intent_type}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {self.risk_level}")
        if self.source not in SOURCES:
            raise ValueError(f"invalid source: {self.source}")
        normalized: list[IntentCandidateAction] = []
        for action in self.candidate_actions:
            normalized.append(action if isinstance(action, IntentCandidateAction) else IntentCandidateAction(**action))
        self.candidate_actions = normalized

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent_type": self.intent_type,
            "goal": self.goal,
            "confidence": float(self.confidence),
            "slots": dict(self.slots),
            "constraints": list(self.constraints),
            "missing_inputs": list(self.missing_inputs),
            "candidate_actions": [action.to_dict() for action in self.candidate_actions],
            "allowed_tools": list(self.allowed_tools),
            "risk_level": self.risk_level,
            "risk_notes": list(self.risk_notes),
            "clarification_question": self.clarification_question,
            "routing_reason": self.routing_reason,
            "source": self.source,
            "model_provider": self.model_provider,
            "model_model": self.model_model,
            "model_usage": dict(self.model_usage),
        }


def decide_intent(home: Path, session: Session, text: str) -> IntentDecision:
    normalized = text.strip()
    resolved_home = home.expanduser().resolve()
    ensure_builtin_skills(resolved_home, session)
    from agentend.core.tool_contracts import sync_tool_manifests
    from agentend.core.tool_registry import ToolRegistry

    sync_tool_manifests(session, ToolRegistry(resolved_home).manifests())
    refresh_capabilities(session)

    if not normalized:
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="clarification",
            goal=normalized,
            confidence=1.0,
            missing_inputs=["goal"],
            clarification_question="What goal should AgentEnd work on?",
            routing_reason="empty input",
            ),
        )

    lowered = normalized.lower()
    if _looks_like_service_availability_check(normalized, lowered):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
                intent_type="chat",
                goal=normalized,
                confidence=0.94,
                candidate_actions=[IntentCandidateAction("workflow_run", "simple_chat", 0.9, {"input": normalized})],
                routing_reason="short service availability check should stay in chat",
            ),
        )

    if _looks_like_prompt_injection(lowered) or _contains_any(lowered, ["删除整个", "delete everything", "rm -rf"]):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="blocked",
            goal=normalized,
            confidence=0.95,
            risk_level="high",
            risk_notes=["destructive or policy-bypass request"],
            routing_reason="high-risk destructive or instruction-bypass terms matched",
            ),
        )

    path = _extract_path(normalized)
    if _contains_any(lowered, ["写入", "写进", "write file", "write to file"]) and not path:
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="clarification",
            goal=normalized,
            confidence=0.86,
            slots={},
            missing_inputs=["path"],
            risk_level="medium",
            risk_notes=["file write requires an explicit target path"],
            clarification_question="请提供要写入的文件路径。",
            routing_reason="file write intent lacks path slot",
            ),
        )

    if path and _contains_any(lowered, ["读取", "读", "read", "打开", "查看"]):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="tool_action",
            goal=normalized,
            confidence=0.92,
            slots={"path": path},
            candidate_actions=[
                IntentCandidateAction("tool_call", "fs.read_text", 0.95, {"path": path}, "explicit path read")
            ],
            allowed_tools=["fs.read_text"],
            routing_reason="file read terms and path slot matched",
            ),
        )

    if _contains_any(lowered, ["调研", "研究", "搜索", "查找", "报告", "research", "search", "report"]):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="task",
            goal=normalized,
            confidence=0.9,
            slots={"topic": normalized},
            candidate_actions=[
                IntentCandidateAction("skill_run", "research.report", 0.95, {"task": normalized, "goal": normalized}),
                IntentCandidateAction("tool_call", "web.search", 0.85, {"query": normalized, "provider": "fake", "limit": 3}),
                IntentCandidateAction("tool_call", "web.fetch", 0.65),
            ],
            allowed_tools=["web.search", "web.fetch"],
            routing_reason="research/search/report terms matched",
            ),
        )

    if _contains_any(lowered, ["测试", "修复", "代码", "pytest", "test", "code", "bug"]):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="task",
            goal=normalized,
            confidence=0.88,
            candidate_actions=[
                IntentCandidateAction("skill_run", "code.local_task", 0.92, {"task": normalized, "goal": normalized}),
                IntentCandidateAction("tool_call", "git.status", 0.75, {"cwd": "."}),
                IntentCandidateAction("tool_call", "shell.run", 0.68),
            ],
            allowed_tools=["git.status", "shell.run", "fs.read_text"],
            risk_level="medium",
            risk_notes=["shell.run can execute local commands; keep commands explicit."],
            routing_reason="code/test terms matched",
            ),
        )

    if _contains_any(lowered, ["文件", "目录", "整理", "读取", "workspace", "file", "read", "list"]):
        return _finalize_intent_decision(
            resolved_home,
            session,
            IntentDecision(
            intent_type="task",
            goal=normalized,
            confidence=0.82,
            candidate_actions=[
                IntentCandidateAction("skill_run", "file.workspace_ops", 0.9, {"task": normalized, "goal": normalized}),
                IntentCandidateAction("tool_call", "fs.list", 0.7, {"path": "."}),
                IntentCandidateAction("tool_call", "fs.read_text", 0.62),
            ],
            allowed_tools=["fs.list", "fs.read_text"],
            routing_reason="file/workspace terms matched",
            ),
        )

    return _finalize_intent_decision(
        resolved_home,
        session,
        IntentDecision(
        intent_type="chat",
        goal=normalized,
        confidence=0.78,
        candidate_actions=[IntentCandidateAction("workflow_run", "simple_chat", 0.8, {"input": normalized})],
        routing_reason="no action terms matched",
        ),
    )


@dataclass(frozen=True)
class _ModelClassifierResult:
    decision: IntentDecision | None
    provider: str | None
    model: str | None
    usage: dict[str, Any]
    fallback_reason: str | None = None


def _finalize_intent_decision(home: Path, session: Session, decision: IntentDecision) -> IntentDecision:
    if not _should_use_model_classifier(session, decision.goal, decision):
        return constrain_intent_decision(home, session, decision)

    result = _classify_intent_with_model(home, session, decision.goal, decision)
    if result.decision is not None:
        return constrain_intent_decision(home, session, result.decision)

    if result.fallback_reason:
        _append_once(decision.risk_notes, f"model classifier fallback: {result.fallback_reason}")
    decision.model_provider = result.provider
    decision.model_model = result.model
    decision.model_usage = result.usage
    return constrain_intent_decision(home, session, decision)


def _should_use_model_classifier(session: Session, text: str, decision: IntentDecision) -> bool:
    if decision.intent_type in {"blocked", "clarification"}:
        return False
    if session.get(_model_route_cls(), "intent_classify") is None:
        return False
    if _is_complex_or_multi_intent(text):
        return True
    if _looks_like_tool_confusion(text.lower()):
        return True
    return float(decision.confidence) < 0.72


def _classify_intent_with_model(
    home: Path,
    session: Session,
    text: str,
    rule_decision: IntentDecision,
) -> _ModelClassifierResult:
    from agentend.config import load_config
    from agentend.core.events import record_event
    from agentend.core.llm_router import LLMRouter
    from agentend.core.model_routing import resolve_model_route
    from agentend.core.secrets import redact_text

    config = load_config(home)
    route = resolve_model_route(config, session, "intent_classify")
    provider_config = config.llm.providers.get(route.provider)
    if provider_config is None and route.provider == config.llm.provider:
        provider_config = config.llm.provider_config
    if provider_config is None:
        reason = f"missing provider config: {route.provider}"
        record_event(
            session,
            "intent.model_classify",
            {"provider": route.provider, "model": route.model, "status": "fallback", "reason": reason},
        )
        return _ModelClassifierResult(None, route.provider, route.model, {}, reason)

    prompt = _intent_classifier_prompt(session, text, rule_decision)
    try:
        if route.provider == "fake":
            content = _fake_intent_classifier_content(route.model, text, rule_decision)
            usage = _usage_payload(
                prompt,
                content,
                provider=route.provider,
                model=route.model,
                usage_source="estimated",
            )
        else:
            llm_config = replace(
                config.llm,
                provider=route.provider,
                model=route.model,
                provider_config=provider_config,
            )
            response = LLMRouter(replace(config, llm=llm_config)).complete_response(
                prompt,
                messages=[
                    {
                        "role": "system",
                        "content": "Classify user intent. Return only one JSON object matching the requested schema.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            content = response.content
            usage = {
                "provider": response.provider,
                "model": response.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
                "usage_source": "provider",
            }
    except Exception as exc:
        reason = f"model request failed: {redact_text(home, str(exc))}"
        record_event(
            session,
            "intent.model_classify",
            {"provider": route.provider, "model": route.model, "status": "fallback", "reason": reason},
        )
        return _ModelClassifierResult(None, route.provider, route.model, {}, reason)

    parsed = _parse_model_intent_payload(content, text)
    if isinstance(parsed, str):
        record_event(
            session,
            "intent.model_classify",
            {"provider": route.provider, "model": route.model, "status": "fallback", "reason": parsed},
        )
        return _ModelClassifierResult(None, route.provider, route.model, usage, parsed)

    parsed.model_provider = route.provider
    parsed.model_model = route.model
    parsed.model_usage = usage
    record_event(
        session,
        "intent.model_classify",
        {"provider": route.provider, "model": route.model, "status": "applied", "intent_type": parsed.intent_type},
    )
    return _ModelClassifierResult(parsed, route.provider, route.model, usage)


def _parse_model_intent_payload(content: str, text: str) -> IntentDecision | str:
    try:
        payload = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return "invalid JSON"
    if not isinstance(payload, dict):
        return "schema validation failed: model output is not an object"
    payload.setdefault("goal", text)
    payload.setdefault("source", "model")
    try:
        return IntentDecision(**payload)
    except (TypeError, ValueError) as exc:
        return f"schema validation failed: {exc}"


def _intent_classifier_prompt(session: Session, text: str, rule_decision: IntentDecision) -> str:
    from sqlalchemy import select

    from agentend.db.models import Skill, ToolManifest

    tools = []
    for tool in session.execute(select(ToolManifest).order_by(ToolManifest.name).limit(20)).scalars().all():
        tools.append(
            {
                "name": tool.name,
                "enabled": tool.enabled,
                "side_effect": tool.side_effect,
                "description": tool.description[:160],
            }
        )
    skills = [
        {"id": skill.id, "enabled": skill.enabled, "description": skill.description[:160]}
        for skill in session.execute(select(Skill).order_by(Skill.id).limit(20)).scalars().all()
    ]
    payload = {
        "task": "Return structured IntentDecision JSON.",
        "schema": {
            "intent_type": sorted(INTENT_TYPES),
            "confidence": "0.0..1.0",
            "slots": "object",
            "missing_inputs": "array of strings",
            "candidate_actions": [{"type": sorted(ACTION_TYPES), "name": "string", "score": "0.0..1.0"}],
            "allowed_tools": "array of tool names; do not include disabled, generated, or high side-effect tools",
            "risk_level": sorted(RISK_LEVELS),
            "clarification_question": "string or null",
        },
        "user_text": text,
        "rule_decision": rule_decision.to_dict(),
        "tools": tools,
        "skills": skills,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _fake_intent_classifier_content(model: str, text: str, rule_decision: IntentDecision) -> str:
    if model == "invalid-json":
        return "not-json"
    if model == "invalid-schema":
        return json.dumps({"intent_type": "unknown", "confidence": 2.0}, ensure_ascii=False)

    lowered = text.lower()
    path = _extract_path(text)
    if path and _contains_any(lowered, ["test", "pytest", "测试", "命令", "command"]):
        payload = {
            "schema_version": "1",
            "intent_type": "task",
            "goal": text,
            "confidence": 0.86,
            "slots": {"path": path},
            "constraints": ["ask_if_unclear"] if _contains_any(lowered, ["不明确", "unclear", "不确定"]) else [],
            "missing_inputs": [],
            "candidate_actions": [
                {"type": "tool_call", "name": "fs.read_text", "score": 0.9, "input_data": {"path": path}},
                {"type": "skill_run", "name": "code.local_task", "score": 0.82, "input_data": {"task": text, "goal": text}},
                {"type": "tool_call", "name": "git.status", "score": 0.72, "input_data": {"cwd": "."}},
                {"type": "tool_call", "name": "shell.run", "score": 0.58},
            ],
            "allowed_tools": ["fs.read_text", "git.status", "shell.run"],
            "risk_level": "medium",
            "risk_notes": ["model identified a multi-step read and test-command task"],
            "clarification_question": None,
            "routing_reason": "fake model structured classification for complex multi-intent input",
            "source": "model",
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    payload = rule_decision.to_dict()
    payload["source"] = "model"
    payload["confidence"] = max(float(payload.get("confidence") or 0.0), 0.8)
    payload["routing_reason"] = "fake model accepted rule decision for complex input"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _usage_payload(prompt: str, content: str, *, provider: str, model: str, usage_source: str) -> dict[str, Any]:
    input_tokens = _estimate_tokens(prompt)
    output_tokens = _estimate_tokens(content)
    return {
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_source": usage_source,
    }


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    return stripped.strip()


def _model_route_cls():
    from agentend.db.models import ModelRoute

    return ModelRoute


def _is_complex_or_multi_intent(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(r"(先.+再|first.+then|and then|;)", lowered)
        or _contains_any(lowered, ["，再", "然后", "同时", "并且", "如果", "if unclear", "if not clear"])
    )


def _looks_like_tool_confusion(lowered: str) -> bool:
    read_like = _contains_any(lowered, ["读取", "read", "打开", "查看"])
    search_like = _contains_any(lowered, ["搜索", "search", "调研", "research"])
    code_like = _contains_any(lowered, ["测试", "pytest", "code", "bug"])
    return sum([read_like, search_like, code_like]) >= 2


def _looks_like_service_availability_check(normalized: str, lowered: str) -> bool:
    compact = re.sub(r"\s+", "", normalized)
    if len(compact) > 24:
        return False
    if _contains_any(lowered, ["pytest", "代码", "项目", "文件", "跑测试", "运行测试", "test command", "run tests"]):
        return False
    return _contains_any(
        lowered,
        [
            "测试是否可用",
            "测试可用",
            "是否可用",
            "能用吗",
            "可用吗",
            "能不能用",
            "在吗",
            "ping",
            "hello",
        ],
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def constrain_intent_decision(home: Path, session: Session, decision: IntentDecision) -> IntentDecision:
    resolved_home = home.expanduser().resolve()
    from sqlalchemy import select

    from agentend.core.tool_contracts import sync_tool_manifests
    from agentend.core.tool_registry import ToolRegistry
    from agentend.db.models import GeneratedTool, ToolManifest

    sync_tool_manifests(session, ToolRegistry(resolved_home).manifests())
    refresh_capabilities(session)

    generated_drafts = {
        row.id
        for row in session.execute(select(GeneratedTool).where(GeneratedTool.status == "draft")).scalars().all()
    }
    allowed: list[str] = []
    risk_notes = list(decision.risk_notes)
    for tool_name in decision.allowed_tools:
        if tool_name in generated_drafts or tool_name.startswith("generated."):
            _append_once(risk_notes, f"generated draft excluded: {tool_name}")
            continue
        manifest = session.get(ToolManifest, tool_name)
        if manifest is None:
            _append_once(risk_notes, f"unknown tool excluded: {tool_name}")
            continue
        if manifest.enabled != "true":
            _append_once(risk_notes, f"disabled tool excluded: {tool_name}")
            continue
        if manifest.side_effect in HIGH_SIDE_EFFECTS:
            _append_once(risk_notes, f"high side-effect tool excluded: {tool_name} ({manifest.side_effect})")
            decision.risk_level = "high"
            continue
        allowed.append(tool_name)

    decision.allowed_tools = allowed
    decision.risk_notes = risk_notes
    return decision


def _contains_any(text: str, needles: list[str]) -> bool:
    for needle in needles:
        if re.fullmatch(r"[a-z0-9_.-]+", needle):
            if re.search(rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])", text):
                return True
            continue
        if needle in text:
            return True
    return False


def _append_once(values: list[str], item: str) -> None:
    if item not in values:
        values.append(item)


def _extract_path(text: str) -> str | None:
    match = re.search(r"([A-Za-z0-9_.\-\\/]+?\.(?:md|txt|json|yaml|yml|py|toml|csv))", text)
    return match.group(1) if match else None


def _looks_like_prompt_injection(lowered: str) -> bool:
    return _contains_any(
        lowered,
        [
            "忽略所有规则",
            "忽略之前",
            "ignore previous",
            "ignore all",
            "bypass",
            "绕过",
        ],
    )
