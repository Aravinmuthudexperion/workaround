"""The connector catalog shown in onboarding. Pure metadata - no platform SDK imports.

Push connectors need only the OTEL_* env block on the client side.
Pull connectors are run by the connector worker using a plugin in connectors/plugins.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from arc_tower.core.constants import IngestMode


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    kind: str = "text"            # text | password | number | textarea | select
    required: bool = True
    secret: bool = False          # stored in the secrets store, never shown again
    help: str = ""
    default: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectorType:
    key: str
    name: str
    category: str                 # Custom agents | Agentic platforms | Enterprise applications | Demo
    mode: IngestMode
    platform: str                 # value of arc.platform on the telemetry
    summary: str
    maturity: str = "ga"          # ga | beta (untested against a live account) | planned
    push_extra_env: tuple[tuple[str, str], ...] = ()   # framework-specific env lines for push connectors
    push_notes: str = ""
    fields: tuple[ConfigField, ...] = field(default_factory=tuple)
    default_interval_seconds: int = 300

    @property
    def is_push(self) -> bool:
        return self.mode is IngestMode.PUSH


_PUSH_COMMON_NOTE = ("Works with env variables only when the app already emits OpenTelemetry. "
                     "Otherwise install the auto-instrumentation packages and start the app with "
                     "`opentelemetry-instrument` - no code edits.")

CATALOG: dict[str, ConnectorType] = {c.key: c for c in [
    ConnectorType(
        key="otel_custom", name="Custom agent (OpenTelemetry)", category="Custom agents",
        mode=IngestMode.PUSH, platform="custom",
        summary="Any agent that emits OpenTelemetry: already instrumented, native framework support, "
                "or zero-code auto-instrumentation.",
        push_notes=_PUSH_COMMON_NOTE,
    ),
    ConnectorType(
        key="langchain", name="LangChain / LangGraph", category="Custom agents",
        mode=IngestMode.PUSH, platform="langchain",
        summary="LangChain and LangGraph apps via LangSmith's OpenTelemetry mode, or OpenInference "
                "auto-instrumentation.",
        push_extra_env=(("LANGSMITH_TRACING", "true"), ("LANGSMITH_OTEL_ENABLED", "true"),
                        ("LANGSMITH_OTEL_ONLY", "true")),
        push_notes="LangSmith's OTel mode is env-only. Confirm the flag names against the LangSmith "
                   "version in use. Alternative: `pip install openinference-instrumentation-langchain` "
                   "and start with `opentelemetry-instrument`.",
    ),
    ConnectorType(
        key="n8n", name="n8n workflows", category="Agentic platforms", mode=IngestMode.PUSH,
        platform="n8n", summary="n8n's OpenTelemetry support pointed at the ARC ingest endpoint.",
        push_notes="Set the variables on the n8n server/container (self-hosted n8n).",
    ),
    ConnectorType(
        key="azure_foundry", name="Azure AI Foundry (App Insights)", category="Agentic platforms",
        mode=IngestMode.PULL, platform="azure-foundry", maturity="beta",
        summary="Pulls agent and model-call telemetry from the Application Insights resource linked "
                "to the Foundry project (KQL over the Logs API).",
        fields=(
            ConfigField("workspace_id", "Log Analytics workspace ID",
                        help="Workspace behind the App Insights resource (GUID)."),
            ConfigField("tenant_id", "Entra tenant ID"),
            ConfigField("client_id", "App registration (client) ID", help="Needs 'Log Analytics Reader'."),
            ConfigField("client_secret", "Client secret", kind="password", secret=True),
        ),
    ),
    ConnectorType(
        key="aws_bedrock", name="AWS Bedrock (CloudWatch)", category="Agentic platforms",
        mode=IngestMode.PULL, platform="aws-bedrock", maturity="beta",
        summary="Pulls Bedrock model-invocation logs from CloudWatch Logs (Logs Insights query).",
        fields=(
            ConfigField("region", "AWS region", default="us-east-1"),
            ConfigField("log_group", "Invocation log group", help="Bedrock > Settings > Model invocation logging."),
            ConfigField("access_key_id", "Access key ID", help="Or leave empty to use the host's AWS role.",
                        required=False),
            ConfigField("secret_access_key", "Secret access key", kind="password", secret=True, required=False),
        ),
    ),
    ConnectorType(
        key="servicenow", name="ServiceNow (Now Assist / AI Agents)", category="Enterprise applications",
        mode=IngestMode.PULL, platform="servicenow", maturity="beta",
        summary="Pulls AI agent execution records through the ServiceNow Table API.",
        fields=(
            ConfigField("instance_url", "Instance URL", help="https://<instance>.service-now.com"),
            ConfigField("table", "Execution table", default="sn_aia_execution_plan",
                        help="Table holding agent executions in your release; adjust if different."),
            ConfigField("username", "Integration user"),
            ConfigField("password", "Password", kind="password", secret=True),
        ),
    ),
    ConnectorType(
        key="salesforce", name="Salesforce (Agentforce)", category="Enterprise applications",
        mode=IngestMode.PULL, platform="salesforce", maturity="beta",
        summary="Runs a SOQL query over Agentforce session/interaction records.",
        fields=(
            ConfigField("instance_url", "Instance URL", help="https://<domain>.my.salesforce.com"),
            ConfigField("soql", "SOQL query", kind="textarea",
                        default="SELECT Id, CreatedDate, Name FROM AIAgentInteraction "
                                "WHERE CreatedDate > {since} ORDER BY CreatedDate LIMIT 2000"),
            ConfigField("client_id", "Connected app consumer key"),
            ConfigField("client_secret", "Consumer secret", kind="password", secret=True),
        ),
    ),
    ConnectorType(
        key="freshdesk", name="Freshdesk (Freddy AI)", category="Enterprise applications",
        mode=IngestMode.PULL, platform="freshdesk", maturity="planned",
        summary="Planned: Freddy AI usage via the Freshdesk API.",
    ),
    ConnectorType(
        key="airia", name="Airia", category="Agentic platforms", mode=IngestMode.PULL, platform="airia",
        maturity="planned", summary="Planned: Airia agent telemetry via its API / OTel export.",
    ),
    ConnectorType(
        key="demo_platform", name="Demo enterprise platform", category="Demo", mode=IngestMode.PULL,
        platform="demo", default_interval_seconds=60,
        summary="Simulated platform used to demonstrate the pull path end to end without real accounts.",
        fields=(ConfigField("agents", "Agent names (comma separated)", required=False,
                            default="it-helpdesk-agent,hr-policy-agent"),),
    ),
]}


def get(key: str) -> ConnectorType | None:
    return CATALOG.get(key)


def by_category() -> dict[str, list[ConnectorType]]:
    grouped: dict[str, list[ConnectorType]] = {}
    for connector in CATALOG.values():
        grouped.setdefault(connector.category, []).append(connector)
    return grouped
