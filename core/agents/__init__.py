"""Agent Coordinator (SPEC §5.1, §6.3, Phase 3)."""

from core.agents.coordinator import AgentCoordinator
from core.agents.prompts import SYSTEM_PROMPT
from core.agents.roles import DELEGATE_TOOL, ROLES, Role
from core.agents.run import AgentRun, RunOutcome

__all__ = [
    "DELEGATE_TOOL",
    "ROLES",
    "SYSTEM_PROMPT",
    "AgentCoordinator",
    "AgentRun",
    "Role",
    "RunOutcome",
]
