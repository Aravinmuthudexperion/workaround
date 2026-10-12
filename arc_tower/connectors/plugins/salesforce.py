"""Salesforce Agentforce: interaction records via SOQL (OAuth client-credentials flow).

The SOQL query is configurable because Agentforce objects vary by org and release; `{since}`
is replaced with the last sync time. Each row becomes one invoke_agent span.
"""
from __future__ import annotations

from datetime import timedelta

from arc_tower.otlp.builders import SpanRecord
from arc_tower.utils.http import client

from ..base import ConnectorError, PullConnector, RunContext, RunResult
from .common import id_bytes, parse_time

API_VERSION = "v61.0"


class SalesforceConnector(PullConnector):
    type_key = "salesforce"

    def run(self, ctx: RunContext) -> RunResult:
        base = ctx.config["instance_url"].rstrip("/")
        with client(base) as http:
            token = http.post("/services/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": ctx.config["client_id"],
                "client_secret": ctx.secrets.get("client_secret", ""),
            })
            if token.status_code >= 400:
                raise ConnectorError(f"Salesforce OAuth failed: {token.text[:200]}")
            soql = ctx.config["soql"].replace("{since}", ctx.since.strftime("%Y-%m-%dT%H:%M:%SZ"))
            resp = http.get(f"/services/data/{API_VERSION}/query", params={"q": soql},
                            headers={"Authorization": f"Bearer {token.json()['access_token']}"})
        if resp.status_code >= 400:
            raise ConnectorError(f"SOQL query failed: {resp.text[:300]}")
        count, newest = 0, ctx.since
        for record in resp.json().get("records", []):
            start = parse_time(record.get("CreatedDate"))
            if start is None:
                continue
            agent = str(record.get("AgentName") or record.get("Name") or "agentforce-agent")
            ctx.emitter.span(agent, SpanRecord(
                name=f"invoke_agent {agent}", start=start,
                end=start + timedelta(milliseconds=float(record.get("DurationMs") or 0)),
                trace_id=id_bytes(record.get("Id", ""), 16),
                attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
                            "salesforce.record_id": record.get("Id")}))
            count += 1
            newest = max(newest, start)
        return RunResult(records=count, cursor={"since": newest.isoformat()}, message=f"{count} interactions")
