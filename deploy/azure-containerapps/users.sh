#!/usr/bin/env bash
# Manage staff accounts on the running app in one command, from Azure Cloud Shell or a Mac:
#
#   ./deploy/azure-containerapps/users.sh add jsmith --name "Jordan Smith" --email jsmith@bridgew.edu
#   ./deploy/azure-containerapps/users.sh list
#   ./deploy/azure-containerapps/users.sh reset jsmith
#
# Each run: makes the Python environment the first time (and refreshes it only when requirements.txt
# changes), finds the app's address from Azure, then runs `python -m wepa_monitor users …` with your
# arguments. It asks for the administrator password; nothing is saved.
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-resnet-print-ops}"
APP_NAME="${APP_NAME:-resnet-print-ops}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ $# -eq 0 ]; then
  echo "Usage: $0 add|update|reset|deactivate|activate|list|show|halls [username] [options]"
  echo "Example: $0 add jsmith --name \"Jordan Smith\" --email jsmith@bridgew.edu"
  exit 1
fi

# 1. The Python environment: made once, refreshed only when the requirements change.
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python (first time only, a few minutes)…"
  python3 -m venv .venv
fi
STAMP=".venv/.requirements.sha"
WANT="$(sha256sum requirements.txt 2>/dev/null || shasum -a 256 requirements.txt)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  echo "Installing requirements…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

# 2. The app's address, unless WEPA_URL is already set.
if [ -z "${WEPA_URL:-}" ]; then
  FQDN="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
          --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"
  if [ -z "$FQDN" ]; then
    echo "Couldn't find the app $APP_NAME in $RESOURCE_GROUP. Sign in with 'az login', or set WEPA_URL." >&2
    exit 1
  fi
  export WEPA_URL="https://$FQDN"
fi
echo "App: $WEPA_URL"

# 3. The command itself.
exec .venv/bin/python -m wepa_monitor users "$@"
