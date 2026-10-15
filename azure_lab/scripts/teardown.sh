#!/usr/bin/env bash
# Delete everything the lab created: the resource group, ARC's app registration, and the soft-deleted
# Key Vault / Azure OpenAI account (otherwise their names stay reserved for days).
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$LAB_DIR/.lab.env"
[ -f "$STATE" ] || { echo "nothing to tear down (no azure_lab/.lab.env)"; exit 0; }
source "$STATE"

read -r -p "Delete resource group $RG and everything in it? [y/N] " answer
[ "$answer" = "y" ] || exit 1

if [ -n "${ARC_CLIENT_ID:-}" ]; then
  echo "[lab] deleting app registration $ARC_CLIENT_ID"
  az ad app delete --id "$ARC_CLIENT_ID" || true
fi
echo "[lab] deleting resource group $RG (several minutes)"
az group delete -n "$RG" --yes
echo "[lab] purging soft-deleted Key Vault and Azure OpenAI account"
az keyvault purge -n "$RESUMEX_VAULT" -o none || true
az cognitiveservices account purge -l "$OPENAI_LOCATION" -g "$RG" -n "$OPENAI_NAME" -o none || true
rm -f "$STATE"
echo "[lab] done"
