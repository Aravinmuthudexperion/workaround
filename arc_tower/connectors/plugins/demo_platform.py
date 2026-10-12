"""Demo enterprise platform: simulates agent executions pulled from a SaaS platform.

It shows the whole pull path (worker -> emitter -> gateway -> stores -> dashboards) without
real credentials, using the same code path as the real plugins.
"""
from __future__ import annotations

import random
from datetime import timedelta

from arc_tower.otlp.builders import LogEntry, SpanRecord, new_span_id, new_trace_id
from arc_tower.utils.timeutil import utcnow

from ..base import PullConnector, RunContext, RunResult

_MODELS = ["claude-haiku-4-5", "gpt-4.1-mini", "amazon.nova-lite"]
_TOPICS = {"it-helpdesk-agent": ["password reset", "VPN not connecting", "laptop replacement", "MFA enrolment"],
           "hr-policy-agent": ["leave balance", "parental leave policy", "expense limits", "travel approval"]}
_MAX_BACKFILL = timedelta(minutes=10)


class DemoPlatformConnector(PullConnector):
    type_key = "demo_platform"

    def run(self, ctx: RunContext) -> RunResult:
        now = utcnow()
        start = max(ctx.since, now - _MAX_BACKFILL)
        agents = [a.strip() for a in ctx.config.get("agents", "").split(",") if a.strip()] or ["demo-agent"]
        minutes = max((now - start).total_seconds() / 60, 0.5)
        count = 0
        for agent in agents:
            for _ in range(int(minutes * random.uniform(3, 6))):
                at = start + (now - start) * random.random()
                self._execution(ctx, agent, at)
                count += 1
        return RunResult(records=count, message=f"{count} executions from {len(agents)} agents")

    @staticmethod
    def _execution(ctx: RunContext, agent: str, at) -> None:
        model = random.choice(_MODELS)
        trace_id, root_id = new_trace_id(), new_span_id()
        duration = random.uniform(0.8, 4.0)
        failed = random.random() < 0.03
        topic = random.choice(_TOPICS.get(agent, ["general question"]))
        ctx.emitter.span(agent, SpanRecord(
            name=f"invoke_agent {agent}", start=at, end=at + timedelta(seconds=duration), trace_id=trace_id,
            span_id=root_id, error="platform action failed" if failed else None,
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
                        "session.id": f"case-{random.randint(10000, 99999)}", "input.value": topic}))
        llm_start = at + timedelta(seconds=0.1)
        ctx.emitter.span(agent, SpanRecord(
            name=f"chat {model}", start=llm_start, end=llm_start + timedelta(seconds=duration * 0.7),
            trace_id=trace_id, parent_span_id=root_id,
            attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.agent.name": agent,
                        "gen_ai.usage.input_tokens": random.randint(200, 900),
                        "gen_ai.usage.output_tokens": random.randint(40, 300)}))
        if failed:
            ctx.emitter.log(agent, LogEntry(f"{agent}: action failed while handling '{topic}'", at, "ERROR",
                                            {"event.name": "agent.error"}, trace_id=trace_id, span_id=root_id))
