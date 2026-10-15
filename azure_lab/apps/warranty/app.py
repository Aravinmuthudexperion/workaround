"""Warranty: triages warranty claims with Azure OpenAI and keeps them in Cosmos DB.

POST /claims {"customer": "...", "product": "...", "purchase_date": "YYYY-MM-DD", "issue": "..."}
  model triage (category, covered, priority, reply) -> claim saved in Cosmos DB -> raw claim archived in Blob Storage.
GET  /claims?product=...   latest claims
Uses keys and connection strings from app settings (no managed identity). Telemetry goes to Application Insights through
OpenTelemetry; each model call is a gen_ai.* span (see chat()) (read by ARC's Azure AI Foundry connector).
"""
import json
import os
import uuid
from datetime import datetime, timezone

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

configure_azure_monitor()          # before FastAPI is imported, so requests are traced too
tracer = trace.get_tracer(__name__)

from azure.cosmos import CosmosClient  # noqa: E402
from azure.storage.blob import BlobServiceClient  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from openai import AzureOpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

openai_client = AzureOpenAI(azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                            api_key=os.environ["AZURE_OPENAI_API_KEY"], api_version="2024-10-21")
DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
claims = CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"]).get_database_client("warranty") \
    .get_container_client("claims")
archive = BlobServiceClient.from_connection_string(os.environ["ARCHIVE_STORAGE_CONNECTION_STRING"]) \
    .get_container_client("claim-archive")


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


TRIAGE_PROMPT = (
    "You triage consumer-electronics warranty claims. Standard warranty is 12 months from purchase; "
    "physical or liquid damage is not covered. Reply as JSON: "
    '{"category": "hardware"|"software"|"damage"|"battery"|"other", "covered": bool, '
    '"priority": "low"|"medium"|"high", "reply": str (two sentences to the customer)}'
)

app = FastAPI(title="Warranty claims")


def require_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != os.environ.get("API_KEY"):
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")


class Claim(BaseModel):
    customer: str
    product: str
    purchase_date: str
    issue: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "product": "warranty"}


@app.post("/claims", dependencies=[Depends(require_key)])
def create_claim(claim: Claim) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    completion = chat(
        messages=[
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": f"Today: {today}\nProduct: {claim.product}\n"
                                        f"Purchased: {claim.purchase_date}\nIssue: {claim.issue}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=250,
        temperature=0.2,
    )
    try:
        triage = json.loads(completion.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        triage = {"category": "other", "covered": False, "priority": "medium", "reply": ""}
    record = {"id": str(uuid.uuid4()), **claim.model_dump(), "triage": triage,
              "created_at": datetime.now(timezone.utc).isoformat()}
    claims.create_item(record)
    archive.upload_blob(f"{record['id']}.json", json.dumps(record).encode())
    return record


@app.get("/claims", dependencies=[Depends(require_key)])
def list_claims(product: str | None = None, limit: int = 20) -> list[dict]:
    query = "SELECT TOP @n c.id, c.customer, c.product, c.triage, c.created_at FROM c"
    params = [{"name": "@n", "value": max(1, min(limit, 100))}]
    if product:
        query += " WHERE c.product = @p"
        params.append({"name": "@p", "value": product})
    query += " ORDER BY c.created_at DESC"
    return list(claims.query_items(query, parameters=params, enable_cross_partition_query=not product))
