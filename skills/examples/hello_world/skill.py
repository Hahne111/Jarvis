"""Reference skill for the JARVIS Skill Factory.

Only the SDK and a few pure-Python modules are imported; the world is reached exclusively via
``ctx.call`` (here: ``mock.clock``), which runs through the Core's gate.
"""

from __future__ import annotations

from typing import Any

from skills.sdk import Skill, SkillContext


class HelloWorldSkill(Skill):
    def __init__(self) -> None:
        self.counter = 0

    async def greet(self, ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "world")
        await ctx.log("greeted", name=name)
        return {"text": f"Hello, {name}."}

    async def count(self, ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
        step = int(args.get("step") or 1)
        if step > 100:
            return {"counter": self.counter, "applied": 0}  # "did nothing" -> verifier catches it
        self.counter += step
        return {"counter": self.counter, "applied": step}

    async def time_greeting(self, ctx: SkillContext, args: dict[str, Any]) -> dict[str, Any]:
        clock = await ctx.call("mock.clock", {})
        return {"text": f"Hello. The core says it is {clock.get('now', 'unknown')}."}

    def handlers(self):
        return {"greet": self.greet, "count": self.count, "time_greeting": self.time_greeting}

    def verifiers(self):
        def counter_is(args: dict[str, Any], result: dict[str, Any] | None):
            wanted = int(args.get("step") or 1)
            applied = int((result or {}).get("applied", 0))
            return applied == wanted, {
                "counter": self.counter,
                "applied": applied,
                "wanted": wanted,
            }

        return {"counter_is": counter_is}
