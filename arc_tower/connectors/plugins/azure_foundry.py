"""Azure AI Foundry: agent/model telemetry from the linked Application Insights (Log Analytics) workspace.

Foundry agents and the Azure AI inference SDK write OpenTelemetry spans to App Insights; they land
in the AppDependencies table with gen_ai.* keys in Properties. We query them with KQL and re-emit
them as OTLP spans. Requires: pip install -e .[azure], and 'Log Analytics Reader' on the workspace.
"""
from __future__ import annotations

from datetime import timedelta

from arc_tower.otlp.builders import SpanRecord
from arc_tower.utils.timeutil import utcnow

from ..base import ConnectorError, PullConnector, RunContext, RunResult, require
from .common import as_int, id_bytes, parse_time

KQL = """
AppDependencies
| where TimeGenerated > datetime({since})
| where isnotempty(tostring(Properties["gen_ai.operation.name"])) or isnotempty(tostring(Properties["gen_ai.request.model"]))
| project TimeGenerated, OperationId, Id, ParentId, Name, DurationMs, Success, AppRoleName, Properties
| order by TimeGenerated asc
| take 5000
"""


class AzureFoundryConnector(PullConnector):
    type_key = "azure_foundry"

    def run(self, ctx: RunContext) -> RunResult:
        identity = require("azure.identity", "azure")
        monitor = require("azure.monitor.query", "azure")
        credential = identity.ClientSecretCredential(ctx.config["tenant_id"], ctx.config["client_id"],
                                                     ctx.secrets.get("client_secret", ""))
        client = monitor.LogsQueryClient(credential)
        response = client.query_workspace(ctx.config["workspace_id"], KQL.format(since=ctx.since.isoformat()),
                                          timespan=(ctx.since, utcnow()))
        if response.status != monitor.LogsQueryStatus.SUCCESS:
            raise ConnectorError(f"KQL query failed: {getattr(response, 'partial_error', response.status)}")
        count, newest = 0, ctx.since
        for table in response.tables:
            columns = list(table.columns)
            for row in table.rows:
                record = dict(zip(columns, row))
                start = parse_time(record["TimeGenerated"])
                if start is None:
                    continue
                props = record.get("Properties") or {}
                attrs = {k: v for k, v in props.items() if k.startswith(("gen_ai.", "session.", "user."))}
                for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
                    if key in attrs:
                        attrs[key] = as_int(attrs[key])
                service = record.get("AppRoleName") or "azure-foundry-agent"
                ctx.emitter.span(service, SpanRecord(
                    name=record.get("Name") or "gen_ai", start=start,
                    end=start + timedelta(milliseconds=float(record.get("DurationMs") or 0)),
                    trace_id=id_bytes(record.get("OperationId", ""), 16), span_id=id_bytes(record.get("Id", ""), 8),
                    parent_span_id=id_bytes(record["ParentId"], 8) if record.get("ParentId") else b"",
                    attributes=attrs, error=None if record.get("Success") in (True, "True", 1) else "failed"))
                count += 1
                newest = max(newest, start)
        return RunResult(records=count, cursor={"since": newest.isoformat()},
                         message=f"{count} spans from App Insights")
