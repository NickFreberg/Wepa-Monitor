#!/usr/bin/env bash
# Copy history collected on this computer (data/live) into the cloud app's data, for the time
# before the cloud collector started. Safe while the cloud keeps collecting: the files go in their
# own folder (data/live/imports/) and are merged on load with duplicate minutes dropped; the
# cloud's own files are never touched. Rerunning replaces the earlier import.
#
#     ./deploy/azure-containerapps/import-local-data.sh
#
# Steps: makes sure the cloud runs code that understands imports (redeploys), packages local data,
# uploads it. Stop the local collector first (Ctrl+C in its window) so it doesn't keep running.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-resnet-print-ops}"
TAG="${TAG:-mac}"
SHARE="wepa"
cd "$(git rev-parse --show-toplevel)"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"

if pgrep -f "wepa_monitor (start|collect)" >/dev/null 2>&1; then
  echo "The local collector is still running. Stop it first (Ctrl+C in its window), then rerun this." >&2
  exit 1
fi
[ -d data/live/snapshots ] || { echo "No local data in data/live to import." >&2; exit 1; }

echo "==> Updating the cloud app to the latest code (it must understand imported files)"
SKIP_WAIT=1 ./deploy/azure-containerapps/deploy.sh

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
STORAGE="${STORAGE:-wepadata$(printf '%s' "$SUBSCRIPTION_ID" | shasum | cut -c1-6)}"
KEY="$(az storage account keys list -g "$RESOURCE_GROUP" -n "$STORAGE" --query '[0].value' -o tsv)"

echo "==> Packaging local data"
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
"$PY" -m wepa_monitor export --tag "$TAG" -o "$OUT"

echo "==> Uploading"
az storage file upload-batch --account-name "$STORAGE" --account-key "$KEY" -d "$SHARE" \
  --destination-path data/live -s "$OUT" --max-connections 8 -o none
echo
echo "Done. The cloud dashboard picks the imported history up within a minute or two."
