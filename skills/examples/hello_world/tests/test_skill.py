"""Skill self-tests: run by the Core inside the sandboxed workspace before install."""

from __future__ import annotations

import asyncio

from skill import HelloWorldSkill
from skills.sdk import SkillContext, load_manifest


class FakeCtx(SkillContext):
    def __init__(self):
        async def call(cap, args):
            return {"now": "12:00"}

        async def log(msg, data):
            return None

        super().__init__("hello_world", call=call, log=log, allowed=("mock.clock",))


def test_manifest_and_handlers_match():
    m = load_manifest(__file__.rsplit("/tests/", 1)[0])
    s = HelloWorldSkill()
    assert set(s.handlers()) == {c.name for c in m.capabilities}


def test_greet_and_count():
    s = HelloWorldSkill()
    ctx = FakeCtx()
    assert asyncio.run(s.greet(ctx, {"name": "Malte"})) == {"text": "Hello, Malte."}
    assert asyncio.run(s.count(ctx, {"step": 2}))["counter"] == 2
    ok, evidence = s.verifiers()["counter_is"]({"step": 2}, {"applied": 2})
    assert ok and evidence["counter"] == 2
    assert asyncio.run(s.time_greeting(ctx, {}))["text"].endswith("12:00.")
