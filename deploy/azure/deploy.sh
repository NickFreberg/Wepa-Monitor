#!/usr/bin/env bash
# Deploy BSU Student Printing Ops to Azure App Service (Linux, Python 3.12, Basic B1 plan).
#
# Run from the repository root on your Mac:
#     ./deploy/azure/deploy.sh
#
# The first run creates the resource group, App Service plan and web app, then deploys.
# Later runs (after `git pull`) only update settings and redeploy the code. Collected data
# lives in /home/data/live on the app's persistent storage and survives redeploys.
#
# Override any of these by setting them before the command, e.g. LOCATION=eastus2 ./deploy/azure/deploy.sh
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-resnet-print-ops}"
LOCATION="${LOCATION:-eastus}"
PLAN="${PLAN:-plan-resnet-print-ops}"
SKU="${SKU:-B1}"                         # B1 = 1 core, 1.75 GB RAM, Always On supported (~$13/month)
RUNTIME="${RUNTIME:-PYTHON:3.12}"
STARTUP="gunicorn --bind=0.0.0.0:8000 --workers 1 --threads 8 --timeout 300 wepa_monitor.wsgi:server"

cd "$(git rev-parse --show-toplevel)"

if ! command -v az >/dev/null 2>&1; then
  echo "The Azure CLI isn't installed. On a Mac: brew install azure-cli" >&2
  exit 1
fi
if ! git diff --quiet HEAD -- ; then
  echo "Note: you have uncommitted changes. Only committed code is deployed (git archive HEAD)." >&2
fi

echo "==> Checking your Azure sign-in"
az account show -o none 2>/dev/null || az login -o none
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
echo "    Subscription: $(az account show --query name -o tsv) ($SUBSCRIPTION_ID)"

# Web app names are global across Azure, so add a short suffix that is stable for your subscription.
APP_NAME="${APP_NAME:-resnet-print-ops-$(printf '%s' "$SUBSCRIPTION_ID" | shasum | cut -c1-6)}"
echo "    App name:     $APP_NAME"

if ! az webapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none 2>/dev/null; then
  echo "==> First deployment: creating resources in $LOCATION"
  # New subscriptions must register the App Service provider once (no-op if already registered).
  az provider register --namespace Microsoft.Web --wait -o none
  # The resource group is only a folder; keep the existing one if a previous attempt created it
  # (possibly in another region), and put the plan and app in $LOCATION.
  if [ "$(az group exists -n "$RESOURCE_GROUP")" != "true" ]; then
    az group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none
  fi
  if ! az appservice plan show -g "$RESOURCE_GROUP" -n "$PLAN" -o none 2>/dev/null; then
    if ! az appservice plan create -g "$RESOURCE_GROUP" -n "$PLAN" -l "$LOCATION" --sku "$SKU" --is-linux -o none; then
      cat >&2 <<QUOTA

Azure refused to create the $SKU plan in $LOCATION. New subscriptions often start with a quota of 0
for App Service. Request 1 more "$SKU VMs" (portal: Quotas -> provider Microsoft.Web -> region
$LOCATION -> Request increase), or retry another region: LOCATION=centralus ./deploy/azure/deploy.sh
See deploy/azure/README.md, Troubleshooting.
QUOTA
      exit 1
    fi
  fi
  az webapp create -g "$RESOURCE_GROUP" -p "$PLAN" -n "$APP_NAME" --runtime "$RUNTIME" -o none
fi

echo "==> Applying configuration"
az webapp config set -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --always-on true \
  --startup-file "$STARTUP" \
  --min-tls-version 1.2 \
  --ftps-state Disabled \
  -o none
az webapp config appsettings set -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none --settings \
  WEPA_DATA_DIR=/home/data/live \
  WEPA_CAMPUS_DIR=/home/data/campus \
  WEPA_COLLECT=1 \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true
az webapp update -g "$RESOURCE_GROUP" -n "$APP_NAME" --https-only true -o none
az webapp log config -g "$RESOURCE_GROUP" -n "$APP_NAME" --docker-container-logging filesystem -o none

echo "==> Packaging the committed code"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
git archive --format=zip -o "$WORKDIR/app.zip" HEAD

echo "==> Deploying (Azure installs the Python packages; this takes a few minutes)"
az webapp deploy -g "$RESOURCE_GROUP" -n "$APP_NAME" --src-path "$WORKDIR/app.zip" --type zip -o none

URL="https://$(az webapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" --query defaultHostName -o tsv)"
cat <<DONE

Done. Your dashboard: $URL
  - The first page load after a deploy can take ~30 seconds while the app starts.
  - Live logs:          az webapp log tail -g $RESOURCE_GROUP -n $APP_NAME
  - Next: put Microsoft sign-in in front of it (deploy/azure/README.md, step 5).
DONE
