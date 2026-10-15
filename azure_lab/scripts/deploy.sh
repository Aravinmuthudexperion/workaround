#!/usr/bin/env bash
# Deploy the client-style lab estate: resource group, infrastructure (Bicep), both apps, a budget alert.
# Re-runnable. Settings: LOCATION, OPENAI_LOCATION, RG, CHAT_MODEL, CHAT_MODEL_VERSION, BUDGET_INR.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$LAB_DIR/.lab.env"
BUILD="$LAB_DIR/.build"
RG="${RG:-rg-client-lab}"
LOCATION="${LOCATION:-centralindia}"     # App Service F1 quota is 0 in eastus/eastus2/westus2 on free trials
OPENAI_LOCATION="${OPENAI_LOCATION:-eastus2}"
CHAT_MODEL="${CHAT_MODEL:-gpt-4.1-mini}"
CHAT_MODEL_VERSION="${CHAT_MODEL_VERSION:-2025-04-14}"
BUDGET_INR="${BUDGET_INR:-1000}"

say() { printf '\n[lab] %s\n' "$*"; }
save() {  # save KEY VALUE into .lab.env (replacing an older value)
  touch "$STATE"; grep -v "^$1=" "$STATE" > "$STATE.tmp" || true; mv "$STATE.tmp" "$STATE"
  printf '%s=%q\n' "$1" "$2" >> "$STATE"
}
[ -f "$STATE" ] && source "$STATE"

command -v az >/dev/null || { echo "Azure CLI missing: brew install azure-cli"; exit 1; }
az account show >/dev/null 2>&1 || { echo "Not signed in: run 'az login' first"; exit 1; }
SUB_ID=$(az account show --query id -o tsv)
say "subscription $(az account show --query name -o tsv) ($SUB_ID), region $LOCATION"

say "registering resource providers (first run only takes a minute)"
for ns in Microsoft.Web Microsoft.Storage Microsoft.KeyVault Microsoft.CognitiveServices Microsoft.DocumentDB \
          Microsoft.Insights Microsoft.OperationalInsights Microsoft.Consumption Microsoft.CostManagement; do
  az provider register --namespace "$ns" --wait -o none
done

say "resource group $RG"
if [ "$(az group exists -n "$RG")" = "true" ]; then
  echo "  exists in $(az group show -n "$RG" --query location -o tsv) (resources still go to $LOCATION)"
else
  az group create -n "$RG" -l "$LOCATION" --tags environment=lab -o none
fi

WARRANTY_API_KEY="${WARRANTY_API_KEY:-$(openssl rand -hex 16)}"
save WARRANTY_API_KEY "$WARRANTY_API_KEY"

say "infrastructure (Bicep) - about 5-10 minutes"
az deployment group create -g "$RG" -n client-lab -f "$LAB_DIR/infra/main.bicep" -o none \
  -p location="$LOCATION" openAiLocation="$OPENAI_LOCATION" chatModel="$CHAT_MODEL" \
     chatModelVersion="$CHAT_MODEL_VERSION" warrantyApiKey="$WARRANTY_API_KEY"
out() { az deployment group show -g "$RG" -n client-lab --query "properties.outputs.$1.value" -o tsv; }
FUNC=$(out resumexFunctionName); WEB=$(out warrantyWebAppName)
save SUB_ID "$SUB_ID"; save RG "$RG"; save LOCATION "$LOCATION"; save SUFFIX "$(out suffix)"
save OPENAI_NAME "$(out openAiName)"; save OPENAI_LOCATION "$OPENAI_LOCATION"
save RESUMEX_FUNCTION "$FUNC"; save RESUMEX_URL "$(out resumexUrl)"; save RESUMEX_WORKSPACE_ID "$(out resumexWorkspaceId)"
save RESUMEX_VAULT "$(out resumexVaultName)"
save WARRANTY_WEBAPP "$WEB"; save WARRANTY_URL "$(out warrantyUrl)"; save WARRANTY_WORKSPACE_ID "$(out warrantyWorkspaceId)"

mkdir -p "$BUILD"
say "deploying resumex (Function App, remote build) - a few minutes"
rm -f "$BUILD/resumex.zip"
(cd "$LAB_DIR/apps/resumex" && zip -qr "$BUILD/resumex.zip" . -x '__pycache__/*' '*.pyc' '.venv/*')
az functionapp deployment source config-zip -g "$RG" -n "$FUNC" --src "$BUILD/resumex.zip" --build-remote true -o none

say "deploying warranty (Web App, Oryx build) - a few minutes on the free tier"
rm -f "$BUILD/warranty.zip"
(cd "$LAB_DIR/apps/warranty" && zip -qr "$BUILD/warranty.zip" . -x '__pycache__/*' '*.pyc' '.venv/*')
az webapp deploy -g "$RG" -n "$WEB" --src-path "$BUILD/warranty.zip" --type zip -o none

say "fetching the resumex function key"
for _ in $(seq 1 12); do
  KEY=$(az functionapp keys list -g "$RG" -n "$FUNC" --query functionKeys.default -o tsv 2>/dev/null || true)
  [ -n "$KEY" ] && break; sleep 10
done
[ -n "${KEY:-}" ] && save RESUMEX_FUNCTION_KEY "$KEY" || echo "  key not ready yet: re-run this script later"

say "monthly budget alert (₹$BUDGET_INR)"
EMAIL=$(az account show --query user.name -o tsv)
az deployment group create -g "$RG" -n client-lab-budget -f "$LAB_DIR/infra/budget.bicep" -o none \
  -p amount="$BUDGET_INR" contactEmail="$EMAIL" \
  || echo "  budget not created (some offers do not support budgets) - set one in Cost Management instead"

say "done. State saved in azure_lab/.lab.env"
source "$STATE"
cat <<EOF
  resumex   $RESUMEX_URL/api/health
  warranty  $WARRANTY_URL/health
Next: scripts/grant_arc_access.sh   (read-only identity for ARC Tower)
      python3 scripts/traffic.py   (generate usage)
EOF
