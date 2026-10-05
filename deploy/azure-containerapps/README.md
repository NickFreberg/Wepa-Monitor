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

## Update from your phone (Azure app → Cloud Shell)

No laptop needed. Cloud Shell is a terminal inside Azure that is already signed in.

1. In the Azure app, open **Cloud Shell** (the `>_` icon) and choose **Bash**.
2. Get the code and deploy (copy each line exactly):

   ```bash
   git clone https://github.com/nickfreberg/wepa-monitor.git && cd wepa-monitor
   ASK_SECRETS=1 ./deploy/azure-containerapps/deploy.sh
   ```

3. It asks for each optional key with hidden typing. Paste a key and press Enter, or just press Enter to
   skip it (the app keeps what it already has). Keys go straight into Container Apps secrets; nothing is
   written to the repository, the screen or the shell history.
4. Wait for "Done." (2-5 minutes; the site stays up meanwhile), then open the app and check the Software
   page.

Next time: `cd wepa-monitor && git pull && ./deploy/azure-containerapps/deploy.sh`.

## Day to day

| Task | How |
|---|---|
| Ship new code (no downtime, data kept) | `git pull && ./deploy/azure-containerapps/deploy.sh` |
| Change the site password | `SITE_PASSWORD_RESET=1 ./deploy/azure-containerapps/deploy.sh` |
| Watch the live log | `az containerapp logs show -g rg-resnet-print-ops -n resnet-print-ops --follow --format text` |
| Download a backup | `az storage file download-batch --account-name <storage> -s wepa --pattern 'data/*' -d ./wepa-backup` |
| Remove everything | `az group delete -n rg-resnet-print-ops` (back up first) |

A deploy starts a new copy of the app beside the running one. The site keeps serving from the old
copy until the new one has installed its packages (2–5 minutes) and passes its health check; then
traffic switches and the old copy stops. The new copy starts collecting the moment the old one exits,
so there is no gap in the data. `CLEAN=1` additionally removes files deleted from the repo.

## Bring in data collected on your Mac

If the Mac collected data before the cloud app started, add that history to the cloud:

1. Stop the Mac collector (Ctrl+C in its window). Only one collector should run.
2. Run:

   ```bash
   git pull && source .venv/bin/activate
   ./deploy/azure-containerapps/import-local-data.sh
   ```

The script updates the cloud app, packages `data/live`, and uploads it to `data/live/imports/` on
the share. The cloud merges it with its own data and drops duplicate minutes; its own files are
never changed. Running it again replaces the earlier import.

## Staff accounts

People sign in on the app's sign-in page with their own accounts. The administrator (`bsuresnet`,
from `WEPA_BASIC_AUTH`) creates them in one command from Azure Cloud Shell or a Mac. The script sets
up Python the first time (and again only when the requirements change), finds the app's address and
runs the command:

```bash
cd ~/wepa-monitor && git pull
./deploy/azure-containerapps/users.sh add jsmith --name "Jordan Smith" --email jsmith@bridgew.edu
./deploy/azure-containerapps/users.sh list
```

Or by hand, with the requirements installed:

```bash
export WEPA_URL=https://$(az containerapp show -g rg-resnet-print-ops -n resnet-print-ops --query properties.configuration.ingress.fqdn -o tsv)
python -m wepa_monitor users add jsmith --name "Jordan Smith" --email jsmith@bridgew.edu
python -m wepa_monitor users add alee --name "Alex Lee" --email alee@bridgew.edu --resident yes --hall "Scott Hall"
```

It asks for the `bsuresnet` password and prints a temporary password once. Accounts and pictures live
on the file share (`security/`), so they survive deploys. Optional: set a `WEPA_SECRET_KEY` secret
(any long random string) to sign sessions; otherwise one is generated and kept on the share. Full
rules: `docs/ACCOUNTS.md`.

## Turn on the AI analyst (optional)

The Assistant pane, Insights answers and summaries, and the IT Outcomes copy can be written by an AI
analyst. It never calculates anything itself: it reads a fact sheet and calls read-only tools
(`wepa_monitor/analyst.py`) that run the dashboard's own computations: availability, outages and
causes, repair times by desk, supplies and forecasts, usage, the report card, period comparisons,
campus calendar and class schedules, and the statistics (failure rates, warning-to-outage, coverage
gaps, staffing what-ifs). Its instructions (`SYSTEM_PROMPT` in `wepa_monitor/ai.py`) tell it to
state only facts from those results, to say when the data can't answer, to stay on BSU's printers and
campus, and never to discuss individuals. Every reply is then **fact-checked**: any figure that
doesn't appear in what it was shown gets one rewrite, and anything still unsupported is flagged on
the reply (the IT Outcomes page falls back to the built-in text instead). Without a provider, the
site uses its built-in, rule-written text.

**GitHub Copilot** (needs a GitHub account with a Copilot license; create a fine-grained token with
the "Copilot Requests" permission):

```bash
az containerapp secret set -g rg-resnet-print-ops -n resnet-print-ops --secrets copilot-token=<token>
az containerapp update -g rg-resnet-print-ops -n resnet-print-ops \
  --set-env-vars WEPA_AI_PROVIDER=copilot COPILOT_GITHUB_TOKEN=secretref:copilot-token
```

**Or Claude** (an API key from console.anthropic.com, billed per use; a claude.ai Pro/Max subscription
can't power a website):

```bash
az containerapp secret set -g rg-resnet-print-ops -n resnet-print-ops --secrets anthropic-key=<key>
az containerapp update -g rg-resnet-print-ops -n resnet-print-ops \
  --set-env-vars WEPA_AI_PROVIDER=anthropic ANTHROPIC_API_KEY=secretref:anthropic-key
```

The default model is Claude Opus 5.5; add `WEPA_AI_MODEL=claude-sonnet-5-5` or `claude-haiku-4-5` to
the same command for a cheaper model. An analyst answer usually makes a few data lookups, each a
round trip to the model (prompt caching keeps the repeats cheap), so expect a few cents per Assistant
answer on Opus and less on Sonnet or Haiku; summaries cost less. `WEPA_AI_MAX_PER_HOUR` caps the total,
and the Claude Console's spend limit is the hard stop.

**Or an OpenAI-compatible endpoint** (OpenAI, or Azure OpenAI in your BSU subscription):

```bash
az containerapp secret set -g rg-resnet-print-ops -n resnet-print-ops --secrets ai-key=<key>
az containerapp update -g rg-resnet-print-ops -n resnet-print-ops --set-env-vars WEPA_AI_PROVIDER=openai \
  WEPA_AI_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=2024-10-21 \
  WEPA_AI_API_KEY=secretref:ai-key
```

Either rolls out a new revision with no downtime. The System page shows whether AI is on and
whether its last request worked. `WEPA_AI_MAX_PER_HOUR` (default 120) caps the number of AI calls.
To turn it off: `az containerapp update ... --remove-env-vars WEPA_AI_PROVIDER`.

## Troubleshooting

- **The page doesn't answer for several minutes on first deploy:** check the log. It should show
  pip installing, then `Collector running`. A Python traceback there is the problem to fix.
- **Every region refuses the environment:** the subscription is blocked for Container Apps too;
  only support can lift it (see `deploy/azure-vm/README.md` for the ticket wording).
- **Don't scale it out:** the app is pinned to exactly one replica, so there is exactly one collector.
