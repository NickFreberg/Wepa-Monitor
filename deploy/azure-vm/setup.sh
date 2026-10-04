#!/usr/bin/env bash
# Runs ON the VM as root (deploy.sh calls it): installs or updates BSU Student Printing Ops.
#
#   FQDN=<host> [ACME_EMAIL=...] bash setup.sh /tmp/app.tar.gz   < "username\npassword" (blank keeps current)
#
# Layout:  /opt/wepa/app   code (replaced on each deploy)      /opt/wepa/venv  Python packages
#          /var/lib/wepa   collected data (never touched by deploys)
#          systemd service "wepa" (gunicorn on 127.0.0.1:8000) behind Caddy (HTTPS + password)
set -euo pipefail
ARCHIVE="$1"
APP=/opt/wepa
DATA=/var/lib/wepa
: "${FQDN:?FQDN is required}"
read -r SITE_USER || true
read -r SITE_PASS || true

export DEBIAN_FRONTEND=noninteractive
cloud-init status --wait >/dev/null 2>&1 || true          # first boot: let cloud-init finish with apt

if ! command -v caddy >/dev/null 2>&1; then
  echo "    installing system packages"
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip curl gnupg debian-keyring debian-archive-keyring \
    apt-transport-https unattended-upgrades >/dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

# A little swap so a memory spike (e.g. the first load of a long history) can't kill the app.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id wepa >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin wepa
mkdir -p "$APP" "$DATA/live" "$DATA/campus"
chown -R wepa:wepa "$DATA"

echo "    installing the app"
rm -rf "$APP/app.new" && mkdir -p "$APP/app.new"
tar xzf "$ARCHIVE" -C "$APP/app.new"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q --require-hashes -r "$APP/app.new/requirements.lock"
"$APP/venv/bin/python" -m compileall -q "$APP/app.new/wepa_monitor" >/dev/null || true   # faster starts
rm -rf "$APP/app.old"; if [ -d "$APP/app" ]; then mv "$APP/app" "$APP/app.old"; fi
mv "$APP/app.new" "$APP/app"

cat > /etc/systemd/system/wepa.service <<UNIT
[Unit]
Description=BSU Student Printing Ops (dashboard + every-minute collector)
After=network-online.target
Wants=network-online.target

[Service]
User=wepa
WorkingDirectory=$APP/app
Environment=WEPA_DATA_DIR=$DATA/live
Environment=WEPA_CAMPUS_DIR=$DATA/campus
Environment=WEPA_COLLECT=1
# One worker = one collector (see wepa_monitor/wsgi.py).
ExecStart=$APP/venv/bin/gunicorn --bind=127.0.0.1:8000 --workers 1 --threads 8 --timeout 300 wepa_monitor.wsgi:server
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNIT

# Site login: keep the current one unless a new username/password came in on stdin.
if [ -n "$SITE_USER" ] && [ -n "$SITE_PASS" ]; then
  printf '%s' "$SITE_USER" > /etc/caddy/site-user
  printf '%s\n' "$SITE_PASS" | caddy hash-password > /etc/caddy/site-hash      # needs the trailing newline
  [ -s /etc/caddy/site-hash ] || { echo "Couldn't hash the site password" >&2; exit 1; }
  chmod 600 /etc/caddy/site-user /etc/caddy/site-hash
  chown caddy:caddy /etc/caddy/site-user /etc/caddy/site-hash
fi
[ -s /etc/caddy/site-user ] || { echo "No site username/password set; rerun with SITE_PASSWORD_RESET=1" >&2; exit 1; }

{
  if [ -n "${ACME_EMAIL:-}" ]; then printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"; fi
  cat <<CADDY
$FQDN {
	encode zstd gzip
	basic_auth {
		$(cat /etc/caddy/site-user) $(cat /etc/caddy/site-hash)
	}
	reverse_proxy 127.0.0.1:8000
}
CADDY
} > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

systemctl daemon-reload
systemctl enable -q wepa caddy
systemctl restart wepa
systemctl reload caddy || systemctl restart caddy

echo "    waiting for the app to start"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)"
  [ "$code" = 200 ] && break
  sleep 3
done
[ "$code" = 200 ] || { echo "The app didn't start; see: journalctl -u wepa -n 50" >&2; exit 1; }
journalctl -u wepa -n 20 --no-pager | grep -E "Collector running|campus" || true
echo "    running"
