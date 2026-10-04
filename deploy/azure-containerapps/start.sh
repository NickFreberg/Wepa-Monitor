#!/bin/sh
# How the container starts (Azure Container Apps runs: /bin/sh /mnt/wepa/app/deploy/azure-containerapps/start.sh).
# Installs the exact, hash-checked package versions, then serves the dashboard and runs the collector.
set -e
pip install --no-cache-dir --disable-pip-version-check -q --require-hashes -r /mnt/wepa/app/requirements.lock
cd /mnt/wepa/app
exec gunicorn --bind=0.0.0.0:8000 --workers 1 --threads 8 --timeout 300 wepa_monitor.wsgi:server
