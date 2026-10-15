# ARC Tower

Observability and traceability control tower for every AI agent in the organisation.
Client teams onboard an AI product in six steps. They share **telemetry, never code**.
ARC Tower gives them traces, dashboards, KPIs, alerts and an agent registry.

Runs **natively, with no Docker**: Python in a virtualenv plus five downloaded binaries
(OpenTelemetry Collector, Prometheus, Loki, Grafana, prom-label-proxy). Works on Windows,
macOS and Linux. The architecture is in [`ARC Tower Architecture.html`](ARC%20Tower%20Architecture.html).

---

## Quick start

Requirements: Python 3.10+ and ~2 GB free disk space. Internet access is needed only for `setup`;
for offline machines, see [Offline install](#offline-install-locked-down-windows).

```bash
python start.py setup      # .venv, packages, binaries, .env with fresh secrets, database
python start.py up         # starts everything, prints the URLs and the admin login
python start.py demo       # demo tenant + products, then runs the sample agent for 5 minutes
```

Open **http://localhost:8000** and sign in with the admin credentials printed by `setup`
(also in `.env`: `ARC_ADMIN_EMAIL` / `ARC_ADMIN_PASSWORD`).

| Command | What it does |
|---|---|
| `python start.py status` | Health of every component |
| `python start.py logs <service>` | End of a service log (`run/logs/<service>.log`) |
| `python start.py down` | Stop everything |
| `python start.py up --only arc-app` | Start/restart selected services |
| `python start.py bundle` | Offline zip for Windows (see below) |

### Run the PoC on another machine

```bash
git clone https://github.com/Aravindmuthumurugan/observability.git arc-tower
cd arc-tower
python start.py setup          # needs internet once: packages + component binaries (~600 MB)
python start.py up
python start.py demo --minutes 10   # optional: demo tenant + sample agent traffic
```

- Windows: use `python` from PowerShell; everything else is the same.
- Port 8000 already taken? Set `ARC_PORT=8088` and `ARC_BASE_URL=http://localhost:8088` in `.env`, then `python start.py restart`.
- Locked-down machine without internet: build the offline zip elsewhere with `python start.py bundle` and copy it over (see below).
- Traces need a Langfuse: Langfuse Cloud keys in `.env` (below), or the local test Langfuse in
  [`deploy/langfuse-local/`](deploy/langfuse-local/README.md) if the machine can run containers.

### Langfuse (trace store)

Traces are stored in Langfuse. For the PoC use **Langfuse Cloud** (free tier) or any Langfuse URL.
Create a project, then set in `.env`:

```env
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Restart with `python start.py down && python start.py up`. Without keys, everything except the
trace explorer and Langfuse-based cost/quality works.

| Langfuse mode | How it works | Needs |
|---|---|---|
| **Shared** (default) | One project; every trace tagged `tenant:<t>` + `product:<p>` | Project keys in `.env` |
| **Own keys** | A product's traces go to its own project | Project keys entered on the product page |
| **Managed** | ARC creates a project + keys per product automatically | Langfuse **Enterprise** org keys (`LANGFUSE_ORG_*`) |

---

## How a client onboards

1. **Identify & connect**: create the product and pick a connector.
2. **Ingest**:
   - *Push*: the client adds the `OTEL_*` env block (with its ingest key) to the app's deployment. No code changes.
   - *Pull*: the client enters read-only platform credentials.
3. **Map & standardise**: a coverage score shows which GenAI fields arrive.
4. **KPIs & targets**: set budgets and SLOs.
5. **Alerts**: targets become Grafana alert rules, delivered to e-mail, Teams, Slack or a webhook.
6. **Dashboards**: standard dashboards go live. Editors can build their own and use the panel library.

The env block:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://<arc-host>:4318/otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=x-arc-key=<ingest key>
OTEL_SERVICE_NAME=claims-assistant
```

Env-only works when the app already emits OpenTelemetry. Otherwise use zero-code
auto-instrumentation: `pip install opentelemetry-distro opentelemetry-exporter-otlp
openinference-instrumentation-langchain`, then `opentelemetry-instrument python app.py`.

To accept agents from other machines, set `PROXY_HOST=0.0.0.0` and
`ARC_PUBLIC_INGEST_URL=http://<this-host>:4318/otlp` in `.env`.

---

## Architecture

```
client apps ──OTLP + x-arc-key──▶ ingest proxy ──▶ OTel Collector ──▶ Langfuse   (traces, cost, evals)
                                   (key → tenant,     (GenAI mapping,  ├▶ Prometheus (KPIs; tenant-isolated
                                    stamp, coverage,   PII masking,    │              via prom-label-proxy)
                                    agent discovery)   span metrics)   └▶ Loki       (logs; X-Scope-OrgID)
platforms ◀──pull── connector worker ─────────────────▲
                    intelligence jobs ──metrics──▶ Prometheus
ARC Tower app (FastAPI) ── registry (SQLite/Postgres), onboarding, provisioning ──▶ Grafana (org per tenant)
```

Tenant isolation, layer by layer:

| Layer | Mechanism |
|---|---|
| Ingest | The tenant comes from the ingest key; client-sent `arc.*` attributes are stripped |
| Metrics | Each tenant's Grafana org queries Prometheus through prom-label-proxy with `arc_tenant_id=<slug>` |
| Logs | Loki multi-tenancy (`X-Scope-OrgID = <slug>`) |
| Traces | Tags (shared mode) or a separate Langfuse project |
| UI and API | Every query is built from the user's scope. Users never submit raw PromQL. |

## Code layout

```
start.py                    thin entry point → arc_tower/launcher
arc_tower/
  core/          settings, ports, constants, security (hashing, encryption), logging
  db/            SQLAlchemy base/session + models/ (one module per entity)
  secretstore/   local (encrypted in DB) or HashiCorp Vault
  otlp/          codec, attribute helpers, tenant enrichment, builders, sender
  integrations/  prometheus/, loki/, grafana/ (client, dashboards, panels, queries), langfuse/
  services/      business logic: tenants, products, users, access, kpis, alerts, coverage, ...
  proxy/         ingest proxy (FastAPI): key resolution, stats flushing
  connectors/    catalog, plugin base, emitter, worker, plugins/ (demo, azure, aws, servicenow, salesforce)
  intelligence/  scheduler + jobs/ (pricing, inventory, finops, quality, drift, anomaly, compliance)
  web/           FastAPI app: routes/, api/v1/, templates/, static/, Grafana reverse proxy
  infra/         renders component configs from deploy/templates
  launcher/      stdlib-only process manager used by start.py
  cli/           init-db, render-configs, bootstrap, demo, create-tenant, create-user
deploy/templates/  Collector, Prometheus, Loki and Grafana config templates (Jinja)
sample_agent/      standalone client app using only the OpenTelemetry SDK
tests/             pytest suite
```

## Connectors

| Connector | Mode | Status |
|---|---|---|
| Custom agent (OpenTelemetry), LangChain/LangGraph, n8n | push | available |
| Demo enterprise platform | pull | available (simulated data) |
| Azure platform (resources, metrics, cost) | pull | beta: tested against a simulated Azure, not yet a live subscription (`pip install -e .[azure]`) |
| Azure AI Foundry (App Insights KQL) | pull | beta: built against documented APIs, not yet validated on a live account (`pip install -e .[azure]`) |
| AWS Bedrock (CloudWatch invocation logs) | pull | beta, as above (`pip install -e .[aws]`) |
| ServiceNow (Table API), Salesforce (SOQL) | pull | beta, as above |
| Freshdesk, Airia | pull | planned |

### Azure platform connector

For clients whose resource groups hold several products. The client names **one** resource of the
product (its web app, function app or container app); ARC proposes the rest from evidence that needs
only read access, and a person confirms it in onboarding step 3 (or on the product page):

| Evidence | Confidence weight |
|---|---|
| Role assignments / Key Vault access policies of the starting point's managed identity | high |
| App Service plan or Container Apps environment hosting it; App Insights linked to it; same product tag | high |
| Private endpoint in the starting point's virtual network | medium |
| Name contains the product's word (e.g. `warranty`) | low |

Resources other apps also use are marked **shared**; the reviewer sets this product's share of their
cost. The connector runs every minute and pulls the latest 1-minute Azure Monitor value of each
metric (`arc_azure_metric`; Azure's finest grain, published 1-3 minutes after the fact). It re-scans
the resources every 30 minutes (or at once on "Run now" / saving the connector), and pulls month-to-date
cost per resource every hour (`arc_azure_cost`, only what can be attributed). Scans also flag risks:
public network access, key-based auth, anonymous blob access, missing tags.

Access for the ARC identity (all read-only): **Reader** (+ **Monitoring Reader**) on the resource
groups and **Cost Management Reader**. Sign-in is an app registration's client secret, or ARC's
Managed Identity when the secret is left empty. Code: `services/azure_mapping.py` (mapping, pure),
`services/azure_resources.py` (review, cost), `connectors/plugins/azure_platform.py`,
`integrations/azure/client.py`.

To add a platform: add a `ConnectorType` to `connectors/catalog.py` and a `PullConnector`
subclass in `connectors/plugins/`, then register it in `connectors/registry.py`.

## Offline install (locked-down Windows)

On a machine with internet access (any OS):

```bash
python start.py bundle --target windows-amd64 --python 3.12
```

Copy `dist/arc-tower-windows-amd64.zip` to the target machine, unzip it, and run
`python start.py setup` then `python start.py up`. Setup installs from the bundled `wheels/`
folder and uses the bundled binaries, so it needs no downloads. Use the same Python minor
version as `--python`.

## Configuration

Everything is in `.env` (created from `.env.example`). Key settings:

- `ARC_DATABASE_URL`: SQLite by default; any SQLAlchemy URL (e.g. Postgres) for production.
- `ARC_SECRETS_BACKEND=vault` with `VAULT_ADDR`/`VAULT_TOKEN` stores connector credentials in Vault.
- `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` turn on SSO, e.g. Entra ID (`pip install -e .[oidc]`).
  SSO users still need an ARC account created by an admin.
- `ARC_MODEL_PRICES`: USD per 1M tokens, used for real-time cost estimates on dashboards and
  stamped onto each model-call span as its Langfuse cost at ingest (`langfuse.observation.cost_details`),
  so Langfuse shows cost without anyone registering model prices there. A cost the client already
  sends (`gen_ai.usage.cost` or `langfuse.observation.cost_details`) is left untouched.

Mapping rules for vendor attribute names live in `deploy/templates/otel-collector.yaml.j2`
(`transform/genai`), and PII patterns in `redaction/pii`.

## Tests

```bash
.venv/bin/python -m pytest          # Windows: .venv\Scripts\python -m pytest
```

## Known limits of this PoC

- Prometheus/Loki/Grafana run as single local instances. For production, use Mimir (or
  VictoriaMetrics) for Prometheus, and run Loki and Grafana HA.
- Grafana's auth proxy trusts `127.0.0.1`: do not expose port 3000 beyond localhost.
- Beta pull connectors need validation against real Azure, AWS, ServiceNow and Salesforce accounts.
