# Hosting on Azure App Service

This puts ResNet Print Ops on a permanent HTTPS address (`https://resnet-print-ops-xxxxxx.azurewebsites.net`)
that collects a snapshot every minute, around the clock, and sits behind Microsoft sign-in.

**What it runs on:** one Linux App Service on the **Basic B1** plan (1 core, 1.75 GB RAM),
**about $13/month**. The dashboard and the every-minute collector run in the same app; collected data
is kept on the app's persistent storage (`/home/data/live`) and survives restarts and redeploys.

**Time needed:** about 20 minutes, most of it waiting for Azure.

---

## 1. Make sure you have an Azure subscription

Your Microsoft 365 subscription doesn't include Azure, but the same tenant and admin account can own
one. Azure is billed separately, pay as you go.

1. Go to [portal.azure.com](https://portal.azure.com) and sign in with your Microsoft 365 admin account.
2. Search for **Subscriptions**. If one is listed, you're set. If not, click **+ Add** and create a
   **Pay-As-You-Go** subscription (it asks for a card).

> **Tip: set a spending alert.** Search **Cost Management → Budgets → + Add**, set the scope to your
> subscription, and choose $20/month with an email alert. This app should cost about $13.

## 2. Install the Azure CLI on your Mac

```bash
brew install azure-cli
az login          # opens the browser; sign in with the same account
```

If you have more than one subscription, pick the right one:
`az account set --subscription "<name or id>"`.

## 3. Get the latest code

```bash
cd Wepa-Monitor
git pull
```

## 4. Deploy

```bash
./deploy/azure/deploy.sh
```

The script:

- creates a resource group `rg-resnet-print-ops` in **East US**, a B1 Linux plan, and the web app;
- turns on **Always On** so the collector never sleeps, and enforces HTTPS;
- sets the data folder (`WEPA_DATA_DIR=/home/data/live`) and the campus folder
  (`WEPA_CAMPUS_DIR=/home/data/campus`) where the bridgew.edu calendar and library hours are refreshed;
- uploads the committed code, and Azure installs the Python packages.

The first run takes 5–10 minutes and finishes by printing your URL. Open it: the Overview page shows
live status within a minute or two. The first load after a deploy can take ~30 seconds.

To use a different region or name: `LOCATION=eastus2 APP_NAME=my-print-ops ./deploy/azure/deploy.sh`.

## 5. Require Microsoft sign-in (recommended)

Without this, anyone with the link can view the dashboard. The data comes from a public Wepa page,
but it's still better behind a login, especially if you share it with BSU staff.

1. In the portal, open your web app (search for **resnet-print-ops**).
2. Go to **Settings → Authentication → Add identity provider**.
3. Choose **Microsoft**. Keep **Create new app registration**, and set **Supported account types**
   to **Current tenant – Single tenant**.
4. Under **App Service authentication settings**, choose **Require authentication** and
   **HTTP 302 Found redirect**. Click **Add**.

Now only accounts in your tenant can sign in. To limit it to specific people:

5. Go to the [Microsoft Entra admin center](https://entra.microsoft.com) → **Enterprise applications**
   and open the app with your web app's name.
6. **Properties → Assignment required? → Yes → Save**.
7. **Users and groups → + Add user/group**, and add yourself and anyone else who should have access.
   Adding individual users is free; adding *groups* requires Entra ID P1, which some Microsoft 365
   plans include.

### Sharing with your old bosses (guest access)

1. In the Entra admin center: **Users → All users → + New user → Invite external user**, and enter
   their BSU email address.
2. Add them in step 7 above.

They'll get an invitation email and then sign in with their BSU account.

## Day to day

| Task | How |
|---|---|
| Update after new code | `git pull && ./deploy/azure/deploy.sh` (data is kept) |
| Watch the live log | `az webapp log tail -g rg-resnet-print-ops -n <app-name>` |
| Restart | `az webapp restart -g rg-resnet-print-ops -n <app-name>` |
| Download a backup of the data | Portal → web app → **Advanced Tools (Kudu) → Go**, then open `https://<app-name>.scm.azurewebsites.net/api/zip/home/data/` to download a zip |
| Check health | The dashboard header shows **LIVE**, the "As of" time, and the data quality score; Analytics → Data quality shows completeness and failed refreshes |
| Remove everything (stops all charges) | `az group delete -n rg-resnet-print-ops` (also deletes the collected data; back it up first) |

## Troubleshooting

- **"Application Error" page:** run the log-tail command above and look for a Python traceback. The
  first deploy needs a few minutes to install packages before it can start.
- **Name already taken:** web app names are global across Azure. Run again with
  `APP_NAME=some-other-name ./deploy/azure/deploy.sh`.
- **No data appearing:** the log should print `Collector running: one snapshot per minute`. If it says
  another process holds the lock, wait a minute after a redeploy; the old copy releases it when it stops.
- **Python version:** the script asks for Python 3.12. To see what Azure offers:
  `az webapp list-runtimes --os linux | grep PYTHON`.

## How it's set up (for the curious)

- **Startup command:** `gunicorn --bind=0.0.0.0:8000 --workers 1 --threads 8 --timeout 300 wepa_monitor.wsgi:server`.
  `wepa_monitor/wsgi.py` serves the dashboard and starts the collector thread. There is **one worker**,
  so there is one collector. A lock file stops a second copy during redeploys, and any duplicate
  minute is dropped when data loads.
- **Keeping it light:**
  - Raw minute snapshots, about 44,000 rows a day, are stored compressed (about 140 KB a day, 51 MB
    a year). Each finished day is rolled up once into small summaries (hourly availability,
    status changes, consumable change points), cached under `/home/data/live/derived/`. Pages read
    only those summaries, never the raw minutes.
  - A background thread refreshes the dataset 15 seconds after each minute's snapshot. It
    re-uses everything from finished days and recomputes only today. Page requests never wait
    for it.
  - Expensive results (part forecasts, statistical models) are computed once per refresh and
    shared by every viewer and page.
  - Measured with a full year of data: about 1.5 s of background work a minute, pages in
    0.1–0.4 s (Executive about 0.9 s), about 140 MB of data in memory. The B1 plan (1.75 GB) has
    room for about two years. Move to B2 when memory passes about 70% (Portal → web app →
    Metrics → Memory working set).
- **App settings:** `WEPA_DATA_DIR=/home/data/live`, `WEPA_CAMPUS_DIR=/home/data/campus`, `WEPA_COLLECT=1`,
  `SCM_DO_BUILD_DURING_DEPLOYMENT=true`.
- **Campus data:** the collector refreshes the academic calendar (yearly) and Maxwell Library hours
  (weekly) into `/home/data/campus`, falling back to the copies committed in `reference/`.
