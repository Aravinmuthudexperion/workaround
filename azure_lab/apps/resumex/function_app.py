"""ResumeX: screens a résumé against a role rubric with Azure OpenAI.

POST /api/screen {"candidate": "...", "role": "...", "resume": "..."}
  rubric from Key Vault -> résumé archived in Blob Storage -> score from Azure OpenAI.
All Azure access uses the function's managed identity. Telemetry goes to Application Insights through
OpenTelemetry; each model call is a gen_ai.* span (see chat()) (read by ARC's Azure AI Foundry connector).
"""
import functools
import json
import logging
import os
import uuid
from pathlib import Path

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

configure_azure_monitor()
tracer = trace.get_tracer(__name__)

import azure.functions as func  # noqa: E402  (after telemetry setup)
from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: E402
from azure.keyvault.secrets import SecretClient  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402
from openai import AzureOpenAI  # noqa: E402

credential = DefaultAzureCredential()
openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    azure_ad_token_provider=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
    api_version="2024-10-21",
)
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
vault = SecretClient(os.environ["KEY_VAULT_URL"], credential)
resumes = BlobServiceClient(os.environ["RESUME_STORAGE_URL"], credential).get_container_client("resumes")


def chat(**kwargs):
    """Chat completion wrapped in an OpenTelemetry gen_ai span (GenAI semantic conventions)."""
    with tracer.start_as_current_span(f"chat {DEPLOYMENT}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "az.ai.openai")
        span.set_attribute("gen_ai.request.model", DEPLOYMENT)
        span.set_attribute("gen_ai.request.max_tokens", kwargs.get("max_tokens", 0))
        span.set_attribute("gen_ai.request.temperature", kwargs.get("temperature", 1.0))
        try:
            completion = openai_client.chat.completions.create(model=DEPLOYMENT, **kwargs)
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        span.set_attribute("gen_ai.response.id", completion.id)
        span.set_attribute("gen_ai.response.model", completion.model)
        span.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason or "" for c in completion.choices])
        if completion.usage:
            span.set_attribute("gen_ai.usage.input_tokens", completion.usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion.usage.completion_tokens)
        return completion


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@functools.lru_cache(maxsize=1)
def rubric() -> str:
    return vault.get_secret("screening-rubric").value


def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


@app.route(route="screen", methods=["POST"])
def screen(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)
    resume = str(body.get("resume", "")).strip()
    if not resume:
        return _json({"error": "resume is required"}, 400)
    role = str(body.get("role") or "Software Engineer")
    candidate = str(body.get("candidate") or "anonymous")

    blob_name = f"{uuid.uuid4()}.txt"
    resumes.upload_blob(blob_name, resume.encode(), metadata={"role": role})

    completion = chat(
        messages=[
            {"role": "system", "content": "You screen résumés. Rubric: " + rubric() +
             ' Reply as JSON: {"score": int, "verdict": "shortlist"|"maybe"|"reject", "reasons": [str]}'},
            {"role": "user", "content": f"Role: {role}\nCandidate: {candidate}\n\n{resume}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=300,
        temperature=0.2,
    )
    try:
        result = json.loads(completion.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        logging.warning("model returned non-JSON output")
        result = {"verdict": "unknown"}
    return _json({"id": blob_name, "candidate": candidate, "role": role, **result})


@app.route(route="ui", methods=["GET"])
def ui(req: func.HttpRequest) -> func.HttpResponse:
    """Browser page for trying the screener; same function key as the API (open /api/ui?code=<key>)."""
    page = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")
    return func.HttpResponse(page, mimetype="text/html")


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json({"status": "ok", "product": "resumex"})
