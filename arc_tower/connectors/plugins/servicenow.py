"""ServiceNow: AI agent execution records via the Table API.

The execution table differs between releases, so it is configurable. Each record becomes one
invoke_agent span (sys_created_on -> sys_updated_on); failed states become error spans + logs.
"""
from __future__ import annotations

from arc_tower.otlp.builders import LogEntry, SpanRecord
from arc_tower.utils.http import client

from ..base import ConnectorError, PullConnector, RunContext, RunResult
from .common import id_bytes, parse_time

_FAILED_STATES = {"error", "failed", "cancelled", "canceled"}


class ServiceNowConnector(PullConnector):
    type_key = "servicenow"

    def run(self, ctx: RunContext) -> RunResult:
        base = ctx.config["instance_url"].rstrip("/")
        table = ctx.config.get("table") or "sn_aia_execution_plan"
        since = ctx.since.strftime("%Y-%m-%d %H:%M:%S")
        with client(base, auth=(ctx.config["username"], ctx.secrets.get("password", ""))) as http:
            resp = http.get(f"/api/now/table/{table}", headers={"Accept": "application/json"}, params={
                "sysparm_query": f"sys_updated_on>{since}^ORDERBYsys_updated_on",
                "sysparm_limit": 1000, "sysparm_display_value": "true", "sysparm_exclude_reference_link": "true",
            })
        if resp.status_code == 401:
            raise ConnectorError("ServiceNow rejected the credentials")
        if resp.status_code == 404:
            raise ConnectorError(f"table '{table}' not found - check the execution table name for your release")
        resp.raise_for_status()
        count, newest = 0, ctx.since
        for record in resp.json().get("result", []):
            start = parse_time(record.get("sys_created_on"))
            end = parse_time(record.get("sys_updated_on")) or start
            if start is None:
                continue
            agent = str(record.get("agent") or record.get("ai_agent") or record.get("name") or "servicenow-agent")
            state = str(record.get("state") or record.get("status") or "").lower()
            failed = state in _FAILED_STATES
            trace_id = id_bytes(record.get("sys_id", ""), 16)
            ctx.emitter.span(agent, SpanRecord(
                name=f"invoke_agent {agent}", start=start, end=max(end, start), trace_id=trace_id,
                attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
                            "session.id": record.get("conversation") or record.get("sys_id"),
                            "servicenow.state": state, "servicenow.record": record.get("number") or record.get("sys_id")},
                error=f"state {state}" if failed else None))
            if failed:
                ctx.emitter.log(agent, LogEntry(f"{agent} execution {record.get('sys_id')} ended in state {state}",
                                                end, "ERROR", {"event.name": "agent.error"}, trace_id=trace_id))
            count += 1
            newest = max(newest, end)
        return RunResult(records=count, cursor={"since": newest.isoformat()}, message=f"{count} executions")
