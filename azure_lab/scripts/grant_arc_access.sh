#!/usr/bin/env bash
# The client's onboarding step: a read-only app registration for ARC Tower on the resource group.
#   Reader, Monitoring Reader  -> Resource Graph inventory, role assignments, Azure Monitor metrics
#   Cost Management Reader     -> month-to-date cost per resource
#   Log Analytics Reader       -> App Insights traces
# Prints the values to paste into ARC's connector forms. Re-running rotates the secret.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$LAB_DIR/.lab.env"
[ -f "$STATE" ] || { echo "run scripts/deploy.sh first"; exit 1; }
source "$STATE"
save() { grep -v "^$1=" "$STATE" > "$STATE.tmp" || true; mv "$STATE.tmp" "$STATE"; printf '%s=%q\n' "$1" "$2" >> "$STATE"; }

SCOPE="/subscriptions/$SUB_ID/resourceGroups/$RG"
NAME="arc-tower-reader-$SUFFIX"
echo "[lab] app registration $NAME with Reader on $RG"
CREDS=$(az ad sp create-for-rbac --name "$NAME" --role Reader --scopes "$SCOPE" \
          --query "[appId, password, tenant]" -o tsv)
read -r APP_ID SECRET TENANT <<< "$(echo $CREDS)"
OBJECT_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
for role in "Monitoring Reader" "Cost Management Reader" "Log Analytics Reader"; do
  echo "[lab] + $role"
  az role assignment create --assignee-object-id "$OBJECT_ID" --assignee-principal-type ServicePrincipal \
     --role "$role" --scope "$SCOPE" -o none
done
save ARC_TENANT_ID "$TENANT"; save ARC_CLIENT_ID "$APP_ID"; save ARC_CLIENT_SECRET "$SECRET"

cat <<EOF

Paste into ARC Tower (Products > new product > connector). Role assignments take up to ~5 min to apply.

  Azure platform connector (one per product: resources, metrics, traces, cost)
    Subscription ID(s)   $SUB_ID
    Resource group(s)    $RG
    Entra tenant ID      $TENANT
    Client ID            $APP_ID
    Client secret        $SECRET
    Starting point       resumex: $RESUMEX_FUNCTION   |  warranty: $WARRANTY_WEBAPP
    Product tag          leave empty (let ARC work it out)
EOF
