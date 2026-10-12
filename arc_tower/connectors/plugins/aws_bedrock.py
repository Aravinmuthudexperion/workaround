"""AWS Bedrock: model-invocation logs from CloudWatch Logs (Logs Insights).

Enable Bedrock > Settings > Model invocation logging to CloudWatch first. Each log event has
modelId, input.inputTokenCount and output.outputTokenCount. Invocation logs carry no latency,
so spans have zero duration; latency KPIs for Bedrock come from AgentCore/OTel when available.
Requires: pip install -e .[aws], and logs:StartQuery / logs:GetQueryResults on the log group.
"""
from __future__ import annotations

import json
import time

from arc_tower.otlp.builders import SpanRecord
from arc_tower.utils.timeutil import utcnow

from ..base import ConnectorError, PullConnector, RunContext, RunResult, require
from .common import as_int, id_bytes, parse_time

QUERY = "fields @timestamp, @message | sort @timestamp asc | limit 5000"


class AwsBedrockConnector(PullConnector):
    type_key = "aws_bedrock"

    def run(self, ctx: RunContext) -> RunResult:
        boto3 = require("boto3", "aws")
        kwargs = {"region_name": ctx.config.get("region") or "us-east-1"}
        if ctx.config.get("access_key_id"):
            kwargs |= {"aws_access_key_id": ctx.config["access_key_id"],
                       "aws_secret_access_key": ctx.secrets.get("secret_access_key", "")}
        logs = boto3.client("logs", **kwargs)
        query_id = logs.start_query(logGroupName=ctx.config["log_group"], startTime=int(ctx.since.timestamp()),
                                    endTime=int(utcnow().timestamp()), queryString=QUERY)["queryId"]
        result = {"status": "Running"}
        for _ in range(60):
            result = logs.get_query_results(queryId=query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
                break
            time.sleep(1)
        if result["status"] != "Complete":
            raise ConnectorError(f"CloudWatch Logs Insights query {result['status'].lower()}")
        count, newest = 0, ctx.since
        for row in result.get("results", []):
            fields = {f["field"]: f["value"] for f in row}
            try:
                event = json.loads(fields.get("@message", "{}"))
            except json.JSONDecodeError:
                continue
            at = parse_time(event.get("timestamp")) or parse_time(fields.get("@timestamp"))
            model = event.get("modelId", "unknown").split("/")[-1]
            if at is None:
                continue
            identity = (event.get("identity") or {}).get("arn", "")
            service = identity.rsplit("/", 1)[-1] if identity else "bedrock-app"
            ctx.emitter.span(service, SpanRecord(
                name=f"chat {model}", start=at, end=at, trace_id=id_bytes(event.get("requestId", ""), 16),
                attributes={"gen_ai.operation.name": event.get("operation", "chat").lower(),
                            "gen_ai.system": "aws.bedrock", "gen_ai.request.model": model,
                            "gen_ai.usage.input_tokens": as_int((event.get("input") or {}).get("inputTokenCount")),
                            "gen_ai.usage.output_tokens": as_int((event.get("output") or {}).get("outputTokenCount"))},
            ))
            count += 1
            newest = max(newest, at)
        return RunResult(records=count, cursor={"since": newest.isoformat()},
                         message=f"{count} invocations from CloudWatch")
