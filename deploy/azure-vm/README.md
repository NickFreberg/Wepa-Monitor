# Hosting on an Azure VM

The alternative to App Service when your subscription has no App Service quota. The same app and the
same every-minute collector run on one small Ubuntu VM. The site gets
`https://resnet-print-ops-xxxxxx.<region>.cloudapp.azure.com` with a free certificate and is
protected by a username and password.

**Cost:** about **$21/month** on a Standard_B1ms VM (1 vCPU, 2 GB) at about $15 (about $35 if only the
2 vCPU / 4 GB sizes have capacity), a 32 GB SSD at about
$2.50, and a static public IP at about $3.65.

**What's on the VM:**

| Path | What |
|---|---|
| `/opt/wepa/app` | The code, replaced on each deploy |
| `/var/lib/wepa` | Collected data and campus files; deploys never touch it |
| `wepa` service | Gunicorn on `127.0.0.1:8000`, restarted automatically |
| Caddy | HTTPS (Let's Encrypt, renewed automatically) and the password check, in front of it |

Ubuntu installs security updates automatically. SSH is only open to the IP address of the computer
that last ran the deploy script.

## Deploy

```bash
brew install azure-cli          # once
az login
cd Wepa-Monitor
git checkout master && git pull
./deploy/azure-vm/deploy.sh
```

On the first run the script:

1. creates the VM;
2. asks for a **username and password** for the dashboard (12+ characters);
3. installs everything and prints the address.

It takes about 10 minutes. The first HTTPS visit can take about 30 seconds while the certificate is
issued. Your browser then asks for the username and password.

Optional: `ACME_EMAIL=you@example.com ./deploy/azure-vm/deploy.sh` registers an email for certificate
notices and lets Caddy fall back to a second free certificate authority (ZeroSSL) if Let's Encrypt
is busy.

## Day to day

| Task | How |
|---|---|
| Ship new code (data is kept) | `git pull && ./deploy/azure-vm/deploy.sh` |
| Change the dashboard password | `SITE_PASSWORD_RESET=1 ./deploy/azure-vm/deploy.sh` |
| Watch the live log | `ssh azureuser@<address> sudo journalctl -u wepa -f` |
| Restart the app | `ssh azureuser@<address> sudo systemctl restart wepa` |
| Download a backup | `ssh azureuser@<address> 'sudo tar czf - -C /var/lib/wepa live campus' > wepa-backup.tgz` |
| Remove everything | `az group delete -n rg-resnet-print-ops` (back up first) |

If SSH times out after you've changed networks (home vs. campus), rerun the deploy script. It
re-allows SSH from your current IP.

## Troubleshooting

- **"SkuNotAvailable … Capacity Restrictions":** Azure has no room for that size in that region for
  your subscription right now (common for new subscriptions in busy regions like East US). The
  script already tries several regions (East US 2, Central US, North Central US, West US 2/3, South
  Central US, East US) and sizes (B1ms, B2als_v2, B2s) in turn. To pin one:
  `LOCATION=centralus SIZE=Standard_B1ms ./deploy/azure-vm/deploy.sh`.
- **Quota error creating the VM** ("Standard BS Family vCPUs"): portal → **Quotas → Compute** →
  your region → **Standard BS Family vCPUs** → request **2**. Compute requests are usually approved
  automatically within minutes. Or try another region: `LOCATION=centralus ./deploy/azure-vm/deploy.sh`.
- **Browser shows a certificate error** for the first minute: Caddy is still getting the
  certificate. Wait 30–60 seconds. If it persists, check `ssh azureuser@<address> sudo journalctl -u caddy -n 50`.
- **"The app didn't start"**: `ssh azureuser@<address> sudo journalctl -u wepa -n 50` shows the error.
- **Upgrading later:** if memory gets tight after a year or two (`ssh … free -h`), resize in the
  portal (VM → **Size** → Standard_B2s, 4 GB). It restarts once; data is kept.
