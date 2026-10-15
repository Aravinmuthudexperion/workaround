"""Azure AI Foundry: agent/model telemetry from the linked Application Insights (Log Analytics) workspace.

For agents that are not mapped with the Azure platform connector: that connector already pulls these
traces from every App Insights resource confirmed for the product, with no workspace ID to enter.

Foundry agents and the Azure AI inference SDK write OpenTelemetry spans to App Insights; they land
in the AppDependencies table with gen_ai.* keys in Properties. We query them with KQL and re-emit
them as OTLP spans. Requires: pip install -e .[azure], and 'Log Analytics Reader' on the workspace.
"""
from __future__ import annotations

from arc_tower.utils.timeutil import utcnow

from ..base import ConnectorError, PullConnector, RunContext, RunResult, require
from .app_insights import COLUMNS, GEN_AI_FILTER, span_from_row

KQL = f"""
AppDependencies
| where TimeGenerated > datetime({{since}})
| where {GEN_AI_FILTER}
| project {COLUMNS}
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
                converted = span_from_row(dict(zip(columns, row)), "azure-foundry-agent")
                if converted is None:
                    continue
                service, span = converted
                ctx.emitter.span(service, span)
                count += 1
                newest = max(newest, span.start)
        return RunResult(records=count, cursor={"since": newest.isoformat()},
                         message=f"{count} spans from App Insights")
