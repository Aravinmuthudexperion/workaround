"""Sample client agent - stands in for a client team's AI application.

It depends only on the OpenTelemetry SDK and knows nothing about ARC Tower:
where to send telemetry and how to authenticate come entirely from the standard
OTEL_* environment variables (see agent.env.example). This is "case 3" from the
architecture page: an app that is already instrumented with OpenTelemetry.

The LLM and tools are simulated (no API keys needed), but the spans follow the
OpenTelemetry GenAI semantic conventions exactly like a real instrumented agent.

    python claims_agent.py --minutes 5 --rate 2
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import time
import uuid

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

MODELS = {"planner": "gpt-4o-mini", "answer": "gpt-4o", "fraud": "gpt-4.1-mini"}
TOOLS = ["lookup_policy", "get_claim_history", "estimate_repair_cost", "check_coverage"]
QUESTIONS = [
    "Is water damage from a burst pipe covered under my home policy?",
    "What is the status of claim CLM-{n}?",
    "My car was hit in a parking lot, how do I file a claim?",
    "Can you email the settlement letter to {email}?",
    "Update my card {card} for the deductible payment.",
    "Why was my claim CLM-{n} partially rejected?",
]

log = logging.getLogger("claims-agent")


def setup_telemetry() -> trace.Tracer:
    """Standard OTel SDK setup. Every exporter setting is read from OTEL_* env variables."""
    provider = TracerProvider()                      # resource from OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    if os.environ.get("OTEL_LOGS_EXPORTER", "otlp") != "none":
        logger_provider = LoggerProvider(resource=provider.resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    return trace.get_tracer("claims-agent", "1.4.0")


def _ns(seconds_from_start: float, start_ns: int) -> int:
    return start_ns + int(seconds_from_start * 1e9)


def llm_call(tracer, parent_ctx, start_ns: int, offset: float, agent: str, role: str, prompt: str,
             degraded: bool) -> float:
    """One chat completion span. Returns its simulated duration in seconds."""
    model = MODELS[role]
    input_tokens = random.randint(350, 1400) * (2 if degraded else 1)
    output_tokens = random.randint(60, 420) * (2 if degraded else 1)
    duration = random.uniform(0.4, 2.2) * (2.5 if degraded else 1) + output_tokens / 180
    span = tracer.start_span(f"chat {model}", context=parent_ctx, kind=SpanKind.CLIENT,
                             start_time=_ns(offset, start_ns))
    span.set_attributes({
        "gen_ai.operation.name": "chat",
        "gen_ai.system": "openai",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.agent.name": agent,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.request.temperature": 0.2,
        "gen_ai.prompt": prompt,
        "gen_ai.completion": f"[{role}] simulated answer ({output_tokens} tokens)",
    })
    span.end(end_time=_ns(offset + duration, start_ns))
    return duration


def tool_call(tracer, parent_ctx, start_ns: int, offset: float, agent: str, fail: bool) -> float:
    tool = random.choice(TOOLS)
    duration = random.uniform(0.05, 0.6)
    span = tracer.start_span(f"execute_tool {tool}", context=parent_ctx, start_time=_ns(offset, start_ns))
    span.set_attributes({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                         "gen_ai.agent.name": agent})
    if fail:
        duration = 30.0
        span.set_status(Status(StatusCode.ERROR, f"{tool} timed out after 30s"))
        span.record_exception(TimeoutError(f"{tool} timed out after 30s"))
    span.end(end_time=_ns(offset + duration, start_ns))
    return duration


def conversation(tracer, degraded: bool) -> None:
    user = "u-" + hashlib.sha1(str(random.randint(1, 400)).encode()).hexdigest()[:10]
    session = f"sess-{uuid.uuid4().hex[:12]}"
    question = random.choice(QUESTIONS).format(n=random.randint(10000, 99999),
                                               email=f"customer{random.randint(1, 99)}@example.com",
                                               card="4111 1111 1111 1111")
    agent = "fraud-check-agent" if "rejected" in question else "claims-triage-agent"
    fail = random.random() < (0.12 if degraded else 0.04)
    start_ns = time.time_ns()

    root = tracer.start_span(f"invoke_agent {agent}", kind=SpanKind.SERVER, start_time=start_ns)
    root.set_attributes({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
                         "session.id": session, "user.id": user,
                         "input.value": question})
    ctx = trace.set_span_in_context(root)
    offset = 0.02
    offset += llm_call(tracer, ctx, start_ns, offset, agent, "planner", question, degraded)
    for _ in range(random.randint(1, 3)):
        offset += tool_call(tracer, ctx, start_ns, offset, agent, fail)
        if fail:
            break
    if not fail:
        offset += llm_call(tracer, ctx, start_ns, offset, agent,
                           "fraud" if agent == "fraud-check-agent" else "answer", question, degraded)
        root.set_attribute("output.value", "Here is what I found about your claim ...")
    else:
        root.set_status(Status(StatusCode.ERROR, "tool failure"))
        log.error("agent %s failed: tool timeout (session %s)", agent, session)

    if "card" in question or random.random() < 0.02:
        log.warning("guardrail blocked sensitive data in prompt for session %s", session,
                    extra={"event.name": "guardrail.violation", "guardrail.rule": "pii.payment_card"})
    root.end(end_time=_ns(offset + 0.02, start_ns))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=5, help="how long to run")
    parser.add_argument("--rate", type=float, default=1.0, help="conversations per second")
    parser.add_argument("--degrade-after", type=float, default=None,
                        help="minutes after which latency, tokens and errors rise (to demo drift/anomaly alerts)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise SystemExit("OTEL_EXPORTER_OTLP_ENDPOINT is not set - load agent.env first")
    tracer = setup_telemetry()
    log.info("sending telemetry to %s as service %s", endpoint, os.environ.get("OTEL_SERVICE_NAME"))
    started = time.time()
    count = 0
    while time.time() - started < args.minutes * 60:
        degraded = args.degrade_after is not None and time.time() - started > args.degrade_after * 60
        conversation(tracer, degraded)
        count += 1
        if count % 50 == 0:
            log.info("%d conversations sent%s", count, " (degraded mode)" if degraded else "")
        time.sleep(random.expovariate(args.rate))
    trace.get_tracer_provider().shutdown()
    log.info("done: %d conversations", count)


if __name__ == "__main__":
    main()
