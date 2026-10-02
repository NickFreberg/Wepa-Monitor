#!/usr/bin/env bash
# Deploy ResNet Print Ops to a small Azure Linux VM (Ubuntu 24.04, Standard_B1ms: 1 vCPU, 2 GB RAM).
#
# Run from the repository root on your Mac:
#     ./deploy/azure-vm/deploy.sh
#
# First run: creates the VM with an https://<name>.<region>.cloudapp.azure.com address, asks you for a
# username and password for the site, and installs everything. Later runs (after `git pull`) upload
# the new code and restart the app; collected data in /var/lib/wepa on the VM is kept.
#
# Override any of these before the command, e.g.  LOCATION=centralus SIZE=Standard_B1ms ./deploy/azure-vm/deploy.sh
#   SITE_PASSWORD_RESET=1   ask for a new site username/password on this run
#   ACME_EMAIL=you@x.com    email for certificate notices (also enables a backup certificate authority)
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-resnet-print-ops}"
VM_NAME="${VM_NAME:-vm-resnet-print-ops}"
# Where to try creating the VM, in order, until Azure has capacity for your subscription. New
# subscriptions are often turned away from busy regions/sizes ("SkuNotAvailable"). Sizes need
# 2 GB+ of RAM:  B1ms 1 vCPU/2 GB ~$15/mo · B2als_v2 2 vCPU/4 GB ~$27/mo · B2s 2 vCPU/4 GB ~$30/mo.
# Set LOCATION and/or SIZE to pin one choice.
REGIONS="${LOCATION:-eastus2 centralus northcentralus westus2 westus3 southcentralus eastus}"
SIZES="${SIZE:-Standard_B1ms Standard_B2als_v2 Standard_B2s}"
ADMIN="${ADMIN:-azureuser}"
IMAGE="Canonical:ubuntu-24_04-lts:server:latest"

cd "$(git rev-parse --show-toplevel)"
command -v az >/dev/null 2>&1 || { echo "Install the Azure CLI first: brew install azure-cli" >&2; exit 1; }
git diff --quiet HEAD -- || echo "Note: uncommitted changes aren't deployed (git archive HEAD)." >&2

echo "==> Checking your Azure sign-in"
az account show -o none 2>/dev/null || az login -o none
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
echo "    Subscription: $(az account show --query name -o tsv)"
DNS_LABEL="${DNS_LABEL:-resnet-print-ops-$(printf '%s' "$SUBSCRIPTION_ID" | shasum | cut -c1-6)}"

FIRST_RUN=0
if ! az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" -o none 2>/dev/null; then
  FIRST_RUN=1
  echo "==> First deployment: creating the VM (trying regions and sizes until Azure has capacity)"
  for ns in Microsoft.Compute Microsoft.Network; do az provider register --namespace "$ns" --wait -o none; done
  if [ "$(az group exists -n "$RESOURCE_GROUP")" != "true" ]; then
    az group create -n "$RESOURCE_GROUP" -l "$(echo $REGIONS | cut -d' ' -f1)" -o none
  fi
  ERR="$(mktemp)"
  CREATED=""
  for LOCATION in $REGIONS; do
    for SIZE in $SIZES; do
      echo "    trying $SIZE in $LOCATION ..."
      if az vm create -g "$RESOURCE_GROUP" -n "$VM_NAME" -l "$LOCATION" --image "$IMAGE" --size "$SIZE" \
          --admin-username "$ADMIN" --generate-ssh-keys --public-ip-sku Standard \
          --public-ip-address-dns-name "$DNS_LABEL" --os-disk-size-gb 32 --storage-sku StandardSSD_LRS \
          -o none 2>"$ERR"; then
        CREATED="$SIZE in $LOCATION"; break 2
      fi
      if grep -qE "SkuNotAvailable|NotAvailableForSubscription|Capacity" "$ERR"; then
        echo "      no capacity for this subscription; next option"
      elif grep -qiE "quota" "$ERR"; then
        echo "      no quota for this size here; next option"
      else
        echo "Azure couldn't create the VM:" >&2
        grep -E "Message:|Code:|ERROR" "$ERR" | head -6 >&2 || tail -20 "$ERR" >&2
        exit 1
      fi
      # A failed attempt can leave a network or IP behind; clear them so the next region starts clean.
      for kind in "network nic" "network public-ip" "network nsg" "network vnet"; do
        for id in $(az $kind list -g "$RESOURCE_GROUP" --query "[?starts_with(name, '$VM_NAME')].id" -o tsv); do
          az $kind delete --ids "$id" -o none 2>/dev/null || true
        done
      done
    done
  done
  if [ -z "$CREATED" ]; then
    cat >&2 <<NONE

None of the regions/sizes tried had capacity or quota for this subscription. Request quota in the
portal (Quotas -> Compute -> e.g. East US 2 -> "Standard BS Family vCPUs" -> 2; usually approved
automatically), then rerun. You can pin a choice: LOCATION=eastus2 SIZE=Standard_B1ms $0
NONE
    exit 1
  fi
  echo "    created: $CREATED"
  az vm open-port -g "$RESOURCE_GROUP" -n "$VM_NAME" --port 80,443 --priority 1010 -o none
fi

# SSH is only reachable from the computer running this script (updated on every run).
MY_IP="$(curl -fsS https://api.ipify.org)"
NSG="$(az network nsg list -g "$RESOURCE_GROUP" --query "[?starts_with(name, '$VM_NAME')].name | [0]" -o tsv)"
az network nsg rule update -g "$RESOURCE_GROUP" --nsg-name "$NSG" -n default-allow-ssh \
  --source-address-prefixes "$MY_IP/32" -o none
FQDN="$(az vm show -d -g "$RESOURCE_GROUP" -n "$VM_NAME" --query fqdns -o tsv)"
HOST="$ADMIN@$FQDN"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$HOST")
echo "    Address: https://$FQDN   (SSH allowed from $MY_IP)"

echo "==> Waiting for the VM to accept SSH"
for _ in $(seq 1 30); do "${SSH[@]}" true 2>/dev/null && break; sleep 10; done

SITE_USER=""; SITE_PASS=""
if [ "$FIRST_RUN" = 1 ] || [ "${SITE_PASSWORD_RESET:-0}" = 1 ] || ! "${SSH[@]}" test -s /etc/caddy/site-user 2>/dev/null; then
  echo "==> Choose a username and password for the dashboard (you'll type these in the browser)"
  read -r -p "    Username: " SITE_USER
  while :; do
    read -r -s -p "    Password (12+ characters): " SITE_PASS; echo
    read -r -s -p "    Again: " AGAIN; echo
    [ "$SITE_PASS" = "$AGAIN" ] && [ ${#SITE_PASS} -ge 12 ] && break
    echo "    Passwords didn't match or were shorter than 12 characters; try again."
  done
fi

echo "==> Uploading the committed code"
WORKDIR="$(mktemp -d)"; trap 'rm -rf "$WORKDIR"' EXIT
git archive --format=tar.gz -o "$WORKDIR/app.tar.gz" HEAD
scp -q -o StrictHostKeyChecking=accept-new "$WORKDIR/app.tar.gz" deploy/azure-vm/setup.sh "$HOST:/tmp/"

echo "==> Installing on the VM (first run: ~5 minutes)"
# The password goes over SSH on standard input, never on a command line.
printf '%s\n%s\n' "$SITE_USER" "$SITE_PASS" | "${SSH[@]}" \
  "sudo FQDN='$FQDN' ACME_EMAIL='${ACME_EMAIL:-}' bash /tmp/setup.sh /tmp/app.tar.gz"

cat <<DONE

Done. Your dashboard: https://$FQDN
  - The first HTTPS visit can take ~30 seconds while the certificate is issued.
  - Live log:   ssh $HOST sudo journalctl -u wepa -f
  - Backup:     ssh $HOST 'sudo tar czf - -C /var/lib/wepa live campus' > wepa-backup-\$(date +%F).tgz
  - Update:     git pull && ./deploy/azure-vm/deploy.sh
DONE
