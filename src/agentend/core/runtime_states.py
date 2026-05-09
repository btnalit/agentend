from __future__ import annotations

TERMINAL_AGENT_RUN_STATUSES = {"completed", "failed", "cancelled", "blocked", "expired"}
ACTIVE_AGENT_RUN_STATUSES = {"pending", "planning", "running", "waiting_input"}

TERMINAL_AGENT_ITERATION_STATUSES = {"completed", "failed", "skipped", "blocked"}
ACTIVE_AGENT_ITERATION_STATUSES = {"created", "action_selected", "policy_checked", "executing", "observed", "evaluated", "checkpointed", "running"}

PENDING_CLARIFICATION_STATUSES = {"pending"}
