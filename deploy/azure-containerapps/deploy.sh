#!/usr/bin/env bash
# Deploy ResNet Print Ops to Azure Container Apps: no VM or App Service quota needed.
#
# Run from the repository root on your Mac (with the project's .venv set up):
#     ./deploy/azure-containerapps/deploy.sh
#
# What it builds (all in resource group rg-resnet-print-ops):
#   - a storage account with an Azure Files share "wepa": the code (/app) and the collected data
#     (/data), so data survives restarts and redeploys;
#   - a Container Apps environment (consumption plan) and one app running the stock python:3.12-slim
#     image, which installs the requirements at start and runs gunicorn (dashboard + collector);
#   - HTTPS on https://<app>.<random>.<region>.azurecontainerapps.io, and a site username/password.
# Later runs upload the new code and restart the app; data is kept.
#   SITE_PASSWORD_RESET=1   set a new site username/password on this run
#   LOCATION=eastus2        region (default: eastus, then a few fallbacks on the first run)
#   SKIP_WAIT=1             don't wait for the restarted app to answer
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-resnet-print-ops}"
REGIONS="${LOCATION:-eastus eastus2 centralus westus2}"
ENV_NAME="${ENV_NAME:-wepa-env}"
APP_NAME="${APP_NAME:-resnet-print-ops}"
SHARE="wepa"
IMAGE="docker.io/library/python:3.12-slim"

cd "$(git rev-parse --show-toplevel)"
command -v az >/dev/null 2>&1 || { echo "Install the Azure CLI first: brew install azure-cli" >&2; exit 1; }
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
"$PY" -c "import werkzeug" 2>/dev/null || { echo "Run this from the project with its .venv set up (see README)." >&2; exit 1; }
git diff --quiet HEAD -- || echo "Note: uncommitted changes aren't deployed (git archive HEAD)." >&2

echo "==> Checking your Azure sign-in"
az account show -o none 2>/dev/null || az login -o none
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
echo "    Subscription: $(az account show --query name -o tsv)"
SUFFIX="$(printf '%s' "$SUBSCRIPTION_ID" | shasum | cut -c1-6)"
STORAGE="${STORAGE:-wepadata$SUFFIX}"
az extension add --name containerapp --upgrade --only-show-errors -y >/dev/null 2>&1 || true
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.Storage; do
  az provider register --namespace "$ns" --wait -o none
done
[ "$(az group exists -n "$RESOURCE_GROUP")" = "true" ] || az group create -n "$RESOURCE_GROUP" -l "${REGIONS%% *}" -o none

FIRST_RUN=0
if ! az containerapp env show -g "$RESOURCE_GROUP" -n "$ENV_NAME" -o none 2>/dev/null; then
  FIRST_RUN=1
  echo "==> Creating the Container Apps environment (tries regions until one accepts)"
  ERR="$(mktemp)"; LOC=""
  for r in $REGIONS; do
    echo "    trying $r ..."
    if az containerapp env create -g "$RESOURCE_GROUP" -n "$ENV_NAME" -l "$r" --logs-destination none \
        -o none 2>"$ERR"; then LOC="$r"; break; fi
    echo "      refused: $(grep -oE '\(([A-Za-z]+)\)[^.]*' "$ERR" | head -1)"
  done
  [ -n "$LOC" ] || { echo "No region accepted a Container Apps environment. Last error:" >&2; tail -5 "$ERR" >&2; exit 1; }
  echo "    environment created in $LOC"
fi
LOC="$(az containerapp env show -g "$RESOURCE_GROUP" -n "$ENV_NAME" --query location -o tsv | tr -d ' ' | tr 'A-Z' 'a-z')"

echo "==> Storage for code and data"
if ! az storage account show -g "$RESOURCE_GROUP" -n "$STORAGE" -o none 2>/dev/null; then
  az storage account create -g "$RESOURCE_GROUP" -n "$STORAGE" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false -o none
fi
KEY="$(az storage account keys list -g "$RESOURCE_GROUP" -n "$STORAGE" --query '[0].value' -o tsv)"
az storage share-rm create -g "$RESOURCE_GROUP" --storage-account "$STORAGE" -n "$SHARE" --quota 20 -o none 2>/dev/null || true
az containerapp env storage set -g "$RESOURCE_GROUP" -n "$ENV_NAME" --storage-name wepafiles \
  --azure-file-account-name "$STORAGE" --azure-file-account-key "$KEY" --azure-file-share-name "$SHARE" \
  --access-mode ReadWrite -o none

echo "==> Uploading the committed code"
WORKDIR="$(mktemp -d)"; trap 'rm -rf "$WORKDIR"' EXIT
mkdir -p "$WORKDIR/app" && git archive HEAD | tar -x -C "$WORKDIR/app"
az storage file delete-batch --account-name "$STORAGE" --account-key "$KEY" -s "$SHARE" --pattern 'app/*' -o none 2>/dev/null || true
az storage file upload-batch --account-name "$STORAGE" --account-key "$KEY" -d "$SHARE" --destination-path app \
  -s "$WORKDIR/app" --max-connections 8 -o none
for d in data data/live data/campus; do
  az storage directory create --account-name "$STORAGE" --account-key "$KEY" -s "$SHARE" -n "$d" -o none
done

APP_EXISTS=0
az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none 2>/dev/null && APP_EXISTS=1
AUTH=""
if [ "$APP_EXISTS" = 0 ] || [ "${SITE_PASSWORD_RESET:-0}" = 1 ]; then
  echo "==> Choose a username and password for the dashboard (you'll type these in the browser)"
  read -r -p "    Username: " SITE_USER
  while :; do
    read -r -s -p "    Password (12+ characters): " SITE_PASS; echo
    read -r -s -p "    Again: " AGAIN; echo
    [ "$SITE_PASS" = "$AGAIN" ] && [ ${#SITE_PASS} -ge 12 ] && break
    echo "    Passwords didn't match or were shorter than 12 characters; try again."
  done
  # Only a salted hash is stored (as a Container Apps secret); the password itself never leaves this Mac.
  AUTH="$SITE_USER:$(printf '%s' "$SITE_PASS" | "$PY" -c 'import sys; from werkzeug.security import generate_password_hash as h; print(h(sys.stdin.read()))')"
fi

if [ "$APP_EXISTS" = 0 ]; then
  echo "==> Creating the app"
  ENV_ID="$(az containerapp env show -g "$RESOURCE_GROUP" -n "$ENV_NAME" --query id -o tsv)"
  AUTH="$AUTH" ENV_ID="$ENV_ID" LOC="$LOC" IMAGE="$IMAGE" "$PY" - > "$WORKDIR/app.yaml" <<'PYEOF'
import json, os
start = ("pip install --no-cache-dir --disable-pip-version-check -q -r /mnt/wepa/app/requirements.txt"
         " && cd /mnt/wepa/app && exec gunicorn --bind=0.0.0.0:8000 --workers 1 --threads 8 --timeout 300"
         " wepa_monitor.wsgi:server")
spec = {
  "location": os.environ["LOC"],
  "properties": {
    "managedEnvironmentId": os.environ["ENV_ID"],
    "configuration": {
      "activeRevisionsMode": "Single",
      "ingress": {"external": True, "targetPort": 8000, "transport": "auto", "allowInsecure": False},
      "secrets": [{"name": "basic-auth", "value": os.environ["AUTH"]}],
    },
    "template": {
      "containers": [{
        "name": "wepa", "image": os.environ["IMAGE"],
        "command": ["/bin/sh", "-c"], "args": [start],
        "env": [
          {"name": "WEPA_DATA_DIR", "value": "/mnt/wepa/data/live"},
          {"name": "WEPA_CAMPUS_DIR", "value": "/mnt/wepa/data/campus"},
          {"name": "WEPA_COLLECT", "value": "1"},
          {"name": "WEPA_BASIC_AUTH", "secretRef": "basic-auth"},
          {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
        ],
        "resources": {"cpu": 0.75, "memory": "1.5Gi"},
        # Installing packages takes a minute or two on each start; allow up to 10 minutes.
        "probes": [{"type": "Startup", "tcpSocket": {"port": 8000}, "initialDelaySeconds": 30,
                    "periodSeconds": 60, "failureThreshold": 10}],
        "volumeMounts": [{"volumeName": "wepa", "mountPath": "/mnt/wepa"}],
      }],
      # Exactly one copy: one collector.
      "scale": {"minReplicas": 1, "maxReplicas": 1},
      "volumes": [{"name": "wepa", "storageType": "AzureFile", "storageName": "wepafiles"}],
    },
  },
}
print(json.dumps(spec, indent=2))   # JSON is valid YAML
PYEOF
  az containerapp create -g "$RESOURCE_GROUP" -n "$APP_NAME" --yaml "$WORKDIR/app.yaml" -o none
else
  if [ -n "$AUTH" ]; then
    az containerapp secret set -g "$RESOURCE_GROUP" -n "$APP_NAME" --secrets "basic-auth=$AUTH" -o none
  fi
  echo "==> Restarting the app with the new code"
  REV="$(az containerapp revision list -g "$RESOURCE_GROUP" -n "$APP_NAME" --query "[?properties.active].name | [0]" -o tsv)"
  if [ -n "$REV" ]; then
    az containerapp revision restart -g "$RESOURCE_GROUP" -n "$APP_NAME" --revision "$REV" -o none
  else   # no active revision reported: roll out a fresh one, which also reads the new code
    az containerapp update -g "$RESOURCE_GROUP" -n "$APP_NAME" --set-env-vars "DEPLOYED_AT=$(date +%s)" -o none
  fi
fi

FQDN="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" --query properties.configuration.ingress.fqdn -o tsv)"
echo "==> Waiting for the app to start (it installs its packages first: 2-5 minutes)"
[ "${SKIP_WAIT:-0}" = 1 ] && { echo "    (skipped) https://$FQDN"; exit 0; }
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://$FQDN/" || true)"
  [ "$code" = 401 ] || [ "$code" = 200 ] && break
  sleep 10
done
cat <<DONE

$([ "${code:-}" = 401 ] || [ "${code:-}" = 200 ] && echo "Done." || echo "Deployed, but it hasn't answered yet; check the log below.") Your dashboard: https://$FQDN
  - Sign in with the username and password you chose.
  - Live log:  az containerapp logs show -g $RESOURCE_GROUP -n $APP_NAME --follow --format text
  - Update:    git pull && ./deploy/azure-containerapps/deploy.sh
  - Backup:    az storage file download-batch --account-name $STORAGE -s $SHARE --pattern 'data/*' -d ./wepa-backup
DONE
