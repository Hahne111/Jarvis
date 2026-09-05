"""Agent Coordinator (SPEC §5.1 "Agent Coordinator", §6.3, Phase 3 steps 19-22).

    route model -> provider.complete(messages, tools of the mission allowlist)
      -> filter_tool_calls (zero-trust allowlist)
      -> every allowed proposal runs through VerifiedExecutor (permission -> gateway -> verifier)
      -> tool results go back to the model as messages
      -> until the model stops calling tools, or the budget/kill switch/approval stops the run.

A run that hits an approval is *paused*: its full state (messages, pending call, budget) is written
to the event log (``agent.run.paused``) so it can be resumed after a restart, by decision_id.
The coordinator never executes anything itself and never sees credentials.
"""

from __future__ import annotations

import json
from typing import Any

from core.agents.prompts import SYSTEM_PROMPT
from core.agents.run import AgentRun, RunOutcome, messages_from_dicts, messages_to_dicts
from core.capabilities.gateway import InvocationStatus
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import DEFAULT_USER_ID, Event, Priority, Sensitivity
from core.models.budget import AgentBudget, BudgetExceeded, BudgetTracker
from core.models.provider import (
    IntelligenceProvider,
    Message,
    ProviderError,
    ToolSpec,
    filter_tool_calls,
)
from core.models.router import ModelRouter, NoEligibleModel, Path, RoutingRequest
from core.verifier.service import VerifiedExecutor

SOURCE = "agent-coordinator"


class AgentCoordinator:
    def __init__(
        self,
        *,
        bus: EventBus,
        executor: VerifiedExecutor,
        capabilities: CapabilityRegistry,
        router: ModelRouter,
        providers: dict[str, IntelligenceProvider],
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._bus = bus
        self._executor = executor
        self._caps = capabilities
        self._router = router
        self._providers = providers
        self._system = system_prompt

    # -- availability --------------------------------------------------------------------------

    def providers(self) -> dict[str, IntelligenceProvider]:
        return dict(self._providers)

    def can_run(
        self, path: Path = Path.DEEP, sensitivity: Sensitivity = Sensitivity.PRIVATE
    ) -> bool:
        try:
            decision = self._router.choose(RoutingRequest(path, sensitivity=sensitivity))
        except NoEligibleModel:
            return False
        provider = self._providers.get(decision.model.provider)
        return provider is not None and provider.available()

    # -- run -----------------------------------------------------------------------------------

    async def run(
        self,
        mission_id: str,
        goal: str,
        *,
        allowlist: set[str] | frozenset[str],
        path: Path = Path.DEEP,
        sensitivity: Sensitivity = Sensitivity.PRIVATE,
        budget: AgentBudget | None = None,
        user_id: str = DEFAULT_USER_ID,
        device_id: str | None = None,
        device_trusted: bool = False,
    ) -> AgentRun:
        allow = frozenset(n for n in allowlist if n in self._caps)
        try:
            decision = self._router.choose(RoutingRequest(path, sensitivity=sensitivity))
        except NoEligibleModel as exc:
            run = AgentRun(mission_id, "none", "none", "none", sorted(allow))
            return await self._finish(run, RunOutcome.FAILED, error=str(exc))
        run = AgentRun(
            mission_id, decision.model.provider, decision.model.id, decision.effort, sorted(allow)
        )
        provider = self._providers.get(decision.model.provider)
        if provider is None or not provider.available():
            return await self._finish(
                run, RunOutcome.FAILED, error=f"provider {decision.model.provider!r} unavailable"
            )
        await self._emit(
            "agent.run.started",
            {"run": run.to_dict(), "goal": goal, "routing": decision.to_dict()},
            mission_id,
            user_id,
            device_id,
        )
        state = {
            "run": run,
            "messages": [Message("user", goal)],
            "tracker": BudgetTracker(budget or AgentBudget()),
            "env": {"user_id": user_id, "device_id": device_id, "device_trusted": device_trusted},
            "allow": allow,
        }
        return await self._loop(provider, state)

    async def resume(self, decision_id: str) -> AgentRun | None:
        """Continue the run that paused on ``decision_id`` (looked up in the event log)."""
        paused = None
        for _, event in self._bus.replay(type_prefix="agent.run"):
            if event.type == "agent.run.paused" and event.payload.get("decision_id") == decision_id:
                paused = event
            elif (
                event.type == "agent.run.resumed"
                and event.payload.get("decision_id") == decision_id
            ):
                paused = None  # already consumed
        if paused is None:
            return None
        p = paused.payload
        run = AgentRun(
            mission_id=p["run"]["mission_id"],
            provider=p["run"]["provider"],
            model=p["run"]["model"],
            effort=p["run"]["effort"],
            tools=list(p["run"]["tools"]),
            run_id=p["run"]["run_id"],
            steps=int(p["run"]["steps"]),
            tool_calls=int(p["run"]["tool_calls"]),
            cost_usd=float(p["run"]["cost_usd"]),
        )
        provider = self._providers.get(run.provider)
        if provider is None or not provider.available():
            return await self._finish(
                run, RunOutcome.FAILED, error=f"provider {run.provider!r} unavailable"
            )
        tracker = BudgetTracker(AgentBudget(**p["budget"]))
        tracker.steps, tracker.tool_calls, tracker.cost_usd = (
            run.steps,
            run.tool_calls,
            run.cost_usd,
        )
        state = {
            "run": run,
            "messages": messages_from_dicts(p["messages"]),
            "tracker": tracker,
            "env": dict(p["env"]),
            "allow": frozenset(p["run"]["tools"]),
        }
        await self._emit(
            "agent.run.resumed",
            {"run": run.to_dict(), "decision_id": decision_id},
            run.mission_id,
            state["env"]["user_id"],
            state["env"]["device_id"],
        )
        pending = p["pending"]
        # Execute the approved call with its grant, then hand the result back to the model.
        outcome = await self._call_tool(
            provider_state=state,
            call_id=pending["call_id"],
            name=pending["name"],
            args=pending["args"],
            decision_id=decision_id,
        )
        if outcome is not None:
            return outcome
        return await self._loop(provider, state)

    # -- internals -----------------------------------------------------------------------------

    async def _loop(self, provider: IntelligenceProvider, state: dict[str, Any]) -> AgentRun:
        run: AgentRun = state["run"]
        tracker: BudgetTracker = state["tracker"]
        env = state["env"]
        tools = [ToolSpec.from_manifest(self._caps.get(n).manifest) for n in sorted(state["allow"])]
        while True:
            try:
                tracker.record_step()
            except BudgetExceeded as exc:
                return await self._budget_exceeded(run, exc, env)
            run.steps = tracker.steps
            try:
                result = await provider.complete(
                    state["messages"],
                    system=self._system,
                    tools=tools,
                    model=run.model,
                    effort=run.effort,
                )
            except ProviderError as exc:
                return await self._finish(run, RunOutcome.FAILED, error=str(exc), env=env)
            run.usage = run.usage + result.usage
            run.cost_usd += result.cost_usd
            try:
                tracker.charge(result.usage, result.cost_usd)
            except BudgetExceeded as exc:
                return await self._budget_exceeded(run, exc, env)
            await self._emit(
                "agent.run.step",
                {
                    "run_id": run.run_id,
                    "step": run.steps,
                    "usage": result.usage.to_dict(),
                    "cost_usd": round(result.cost_usd, 6),
                    "proposed": [t.to_dict() for t in result.tool_calls],
                    "stop_reason": result.stop_reason,
                },
                run.mission_id,
                env["user_id"],
                env["device_id"],
            )
            if result.refused:
                return await self._finish(
                    run,
                    RunOutcome.REFUSED,
                    error=f"provider refused ({result.refusal_category})",
                    env=env,
                )
            if result.text:
                state["messages"].append(Message("assistant", result.text))
            if not result.tool_calls:
                run.final_text = result.text
                return await self._finish(run, RunOutcome.COMPLETED, env=env)

            allowed, rejected = filter_tool_calls(result.tool_calls, state["allow"])
            for call in rejected:
                await self._emit(
                    "agent.tool.rejected",
                    {
                        "run_id": run.run_id,
                        "call": call.to_dict(),
                        "reason": "not on mission allowlist",
                    },
                    run.mission_id,
                    env["user_id"],
                    env["device_id"],
                    priority=Priority.URGENT,
                )
                state["messages"].append(
                    Message(
                        "tool",
                        json.dumps({"status": "rejected", "reason": "not allowed"}),
                        tool_call_id=call.call_id,
                        name=call.name,
                    )
                )
            for call in allowed:
                try:
                    tracker.record_tool_call()
                except BudgetExceeded as exc:
                    return await self._budget_exceeded(run, exc, env)
                run.tool_calls = tracker.tool_calls
                stop = await self._call_tool(
                    provider_state=state, call_id=call.call_id, name=call.name, args=call.args
                )
                if stop is not None:
                    return stop

    async def _call_tool(
        self,
        *,
        provider_state: dict[str, Any],
        call_id: str,
        name: str,
        args: dict[str, Any],
        decision_id: str | None = None,
    ) -> AgentRun | None:
        """Run one proposal through the verified executor; return a finished run if it must stop."""
        run: AgentRun = provider_state["run"]
        env = provider_state["env"]
        await self._emit(
            "agent.tool.proposed",
            {"run_id": run.run_id, "call": {"call_id": call_id, "name": name, "args": args}},
            run.mission_id,
            env["user_id"],
            env["device_id"],
        )
        res = await self._executor.run(
            name,
            args,
            actor=f"agent:{run.run_id}",
            correlation_id=run.mission_id,
            user_id=env["user_id"],
            device_id=env["device_id"],
            device_trusted=env["device_trusted"],
            decision_id=decision_id,
        )
        inv = res.invocation
        if inv.status is InvocationStatus.AWAITING_APPROVAL:
            run.pending_decision_id = inv.decision_id
            await self._emit(
                "agent.run.paused",
                {
                    "run": run.to_dict(),
                    "decision_id": inv.decision_id,
                    "pending": {"call_id": call_id, "name": name, "args": args},
                    "messages": messages_to_dicts(provider_state["messages"]),
                    "budget": provider_state["tracker"].budget.to_dict(),
                    "env": env,
                },
                run.mission_id,
                env["user_id"],
                env["device_id"],
                priority=Priority.URGENT,
            )
            run.outcome = RunOutcome.AWAITING_APPROVAL
            return run
        if inv.status is InvocationStatus.HALTED:
            return await self._finish(run, RunOutcome.HALTED, error=inv.error, env=env)
        provider_state["messages"].append(
            Message(
                "tool",
                json.dumps(
                    {
                        "status": inv.status.value,
                        "verified": res.verification.outcome.value,
                        "result": inv.result,
                        "error": inv.error,
                    },
                    default=str,
                ),
                tool_call_id=call_id,
                name=name,
            )
        )
        return None

    async def _budget_exceeded(
        self, run: AgentRun, exc: BudgetExceeded, env: dict[str, Any]
    ) -> AgentRun:
        await self._emit(
            "agent.run.budget_exceeded",
            {
                "run_id": run.run_id,
                "dimension": exc.dimension,
                "limit": exc.limit,
                "used": exc.used,
            },
            run.mission_id,
            env["user_id"],
            env["device_id"],
            priority=Priority.URGENT,
        )
        return await self._finish(run, RunOutcome.BUDGET_EXCEEDED, error=str(exc), env=env)

    async def _finish(
        self,
        run: AgentRun,
        outcome: RunOutcome,
        *,
        error: str | None = None,
        env: dict[str, Any] | None = None,
    ) -> AgentRun:
        from datetime import UTC, datetime

        run.outcome = outcome
        run.error = error
        run.ended_at = datetime.now(UTC)
        env = env or {}
        await self._emit(
            "agent.run.finished",
            {"run": run.to_dict()},
            run.mission_id,
            env.get("user_id", DEFAULT_USER_ID),
            env.get("device_id"),
        )
        return run

    async def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        user_id: str,
        device_id: str | None,
        *,
        priority: Priority = Priority.NORMAL,
    ) -> None:
        await self._bus.publish(
            Event.new(
                event_type,
                SOURCE,
                payload,
                correlation_id=correlation_id,
                user_id=user_id,
                device_id=device_id,
                priority=priority,
            )
        )
