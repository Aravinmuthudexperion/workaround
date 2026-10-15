# Azure client lab

A small, realistic client estate for testing ARC Tower's Azure connectors against a live subscription.
Two products from two different teams share one resource group and one platform-owned Azure OpenAI
account. The infrastructure is built the way client teams usually build it. Nothing in it is shaped
to help ARC, so the test shows how the connector handles a client it has never seen.

| Product | What it does | Azure services |
|---|---|---|
| **ResumeX** (HR tech team) | `POST /api/screen` scores a résumé against a rubric | Function App (Flex Consumption), Storage, Key Vault, Application Insights, Log Analytics |
| **Warranty** (customer care team) | `POST /claims` triages a warranty claim, `GET /claims` lists them | Web App (App Service F1), Cosmos DB (serverless), Storage, Application Insights, Log Analytics |
| *Platform (shared)* | Model calls for both products | Azure OpenAI, with deployments `resumex-chat` and `warranty-chat` |

The two teams work in different ways, as real teams do:

| | ResumeX | Warranty |
|---|---|---|
| Access to data | Managed identity + RBAC roles | Keys and connection strings in app settings |
| Tags | `application=ResumeX`, `team`, `costCenter` on every resource | `Project=Warranty-Claims` on some resources; Storage and the plan are untagged |
| Telemetry | OpenTelemetry → its own App Insights | same |

The OpenAI account carries no product tag. Both apps call it, so the cost of that one resource
has to be split between the products.

## What to look for in ARC

- **Mapping.** From one starting point per product, does ARC propose the right resources? Warranty is
  the harder case: no identity, partial tags. Does ARC miss the untagged storage account?
- **Shared detection.** Does ARC see that the OpenAI account serves both products? ResumeX reaches it
  through a role assignment. Warranty uses only a key, which leaves no RBAC evidence.
- **Observability.** `arc_azure_metric` for the sites, storage, Cosmos DB and OpenAI (split by deployment).
- **Traceability.** The Azure platform connector reads `gen_ai.*` spans from each product's App Insights,
  which it found during the mapping. No workspace ID to enter, and no second connector.
- **Cost.** A live list-price estimate per service right away (set the connector's currency to INR), and
  the billed cost from Cost Management once Azure publishes it, 8–24 h late (up to 48 h on a new subscription).

## Cost (free credits)

Rough estimate for a month of light testing: **well under ₹500**.

- Flex Consumption and App Service F1: free grant and free tier.
- Storage, Key Vault, Cosmos DB serverless: a few rupees.
- Azure OpenAI `gpt-4.1-mini`: about ₹1 per few hundred requests.
- Log Analytics: first 5 GB a month is free, and each workspace is capped at 0.1 GB a day.

A ₹1000 monthly budget on the resource group emails you at 50 % and 90 %. Run `teardown.sh` when you're
done. The free credits expire on 12 Nov 2026.

## Run it

Requires the Azure CLI (`brew install azure-cli`) and `zip`.

```bash
az login                                   # the new free account
cd azure_lab
./scripts/deploy.sh                        # ~15 min: RG, infra, both apps, budget
./scripts/grant_arc_access.sh              # read-only app registration for ARC; prints connector values
python3 scripts/traffic.py --count 50      # usage for metrics, traces and cost
./scripts/teardown.sh                      # delete everything
```

Settings for `deploy.sh` (environment variables):

| Variable | Default |
|---|---|
| `RG` | `rg-client-lab` |
| `LOCATION` | `centralindia` (free trials have no App Service F1 quota in eastus, eastus2, westus2) |
| `OPENAI_LOCATION` | `eastus2` |
| `CHAT_MODEL` / `CHAT_MODEL_VERSION` | `gpt-4.1-mini` / `2025-04-14` |
| `BUDGET_INR` | `1000` |

If the model isn't offered in the region, list what is available with
`az cognitiveservices model list -l eastus2 -o table` and set `CHAT_MODEL` / `CHAT_MODEL_VERSION`.

## Onboard in ARC Tower

Create a product for each and add the **Azure platform** connector with the values
`grant_arc_access.sh` prints: subscription, resource group, tenant, client ID and secret. The starting
point is the function app (ResumeX) or the web app (Warranty). Leave the product tag empty.

Review the proposed resources in step 3, then set ResumeX's and Warranty's share of the OpenAI account.
Metrics, traces and cost then appear on the product page and on the **Azure Resources** and
**Agent Overview** dashboards.

Secrets and generated values live in `azure_lab/.lab.env` (git-ignored).
