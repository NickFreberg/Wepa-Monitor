# Hosting on Azure Container Apps

For subscriptions that Azure won't yet give App Service or VM capacity (common for new
Pay-As-You-Go subscriptions). Container Apps uses a separate pool of limits that new subscriptions
usually have.

**What you get:** `https://resnet-print-ops.<random>.<region>.azurecontainerapps.io`, with HTTPS
and a site username/password. One copy of the app runs all the time: the dashboard and the
every-minute collector. Code and collected data live on an Azure Files share, so they survive
restarts and redeploys.

**How it runs:** the stock `python:3.12-slim` image mounts the share, installs `requirements.txt`
at start (1–3 minutes), then runs gunicorn. There's no image to build and no registry.

**Cost:** billed per second; roughly **$20–55/month** for 0.75 vCPU / 1.5 GB. The low end applies
when the app is idle, which is most of the time. Storage is about $1–2.

## Deploy

```bash
cd Wepa-Monitor
git checkout master && git pull
source .venv/bin/activate          # the script uses it to hash your site password
./deploy/azure-containerapps/deploy.sh
```

The script:

1. creates the environment, trying East US, East US 2, Central US and West US 2 until one accepts;
2. creates a storage account and file share, and uploads the code;
3. asks for a dashboard **username and password** (12+ characters). Only a salted hash is stored,
   as a Container Apps secret;
4. creates the app and waits until it answers.

The first start takes 2–5 minutes.

## Day to day

| Task | How |
|---|---|
| Ship new code (data is kept) | `git pull && ./deploy/azure-containerapps/deploy.sh` |
| Change the site password | `SITE_PASSWORD_RESET=1 ./deploy/azure-containerapps/deploy.sh` |
| Watch the live log | `az containerapp logs show -g rg-resnet-print-ops -n resnet-print-ops --follow --format text` |
| Download a backup | `az storage file download-batch --account-name <storage> -s wepa --pattern 'data/*' -d ./wepa-backup` |
| Remove everything | `az group delete -n rg-resnet-print-ops` (back up first) |

## Troubleshooting

- **The page doesn't answer for several minutes on first deploy:** check the log. It should show
  pip installing, then `Collector running`. A Python traceback there is the problem to fix.
- **Every region refuses the environment:** the subscription is blocked for Container Apps too;
  only support can lift it (see `deploy/azure-vm/README.md` for the ticket wording).
- **Don't scale it out:** the app is pinned to exactly one replica, so there is exactly one collector.
