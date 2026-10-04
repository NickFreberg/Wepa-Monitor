#!/usr/bin/env bash
# Deploy BSU Student Printing Ops to a small Azure Linux VM (Ubuntu 24.04, Standard_B1ms: 1 vCPU, 2 GB RAM).
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
# subscriptions are often turned away from busy regions/sizes ("SkuNotAvailable"), sometimes for a
# whole size family, so the list spans families. All have 2 GB+ of RAM (approx. Linux pay-as-you-go):
#   B1ms (1 vCPU/2 GB) ~$15/mo · B2pls_v2 (Arm, 2 vCPU/4 GB) ~$22/mo · B2als_v2 (2/4) ~$27/mo
#   D2as_v5 (2/8) ~$63/mo · A2_v2 (2/4, older) ~$66/mo
# Set LOCATION and/or SIZE to pin one choice.
REGIONS="${LOCATION:-eastus2 centralus northcentralus westus2 westus3 southcentralus eastus}"
SIZES="${SIZE:-Standard_B1ms Standard_B2pls_v2 Standard_B2als_v2 Standard_D2as_v5 Standard_A2_v2}"
# When a region is pinned, also try each availability zone: a zone often has room when the region
# as a whole reports "Capacity Restrictions". ("any" = let Azure choose.)
ZONES="${ZONES:-$([ -n "${LOCATION:-}" ] && echo "any 1 2 3" || echo "any")}"
ADMIN="${ADMIN:-azureuser}"
IMAGE_X64="Canonical:ubuntu-24_04-lts:server:latest"
IMAGE_ARM="Canonical:ubuntu-24_04-lts:server-arm64:latest"   # for Arm sizes (a "p" after the digits, e.g. B2pls_v2)

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
  REASONS=""
  for LOCATION in $REGIONS; do
    for SIZE in $SIZES; do
    for ZONE in $ZONES; do
      ZONE_ARGS=(); [ "$ZONE" != any ] && ZONE_ARGS=(--zone "$ZONE")
      IMAGE="$IMAGE_X64"; [[ "${SIZE#Standard_}" =~ ^[A-Z]+[0-9]+[a-z]*p ]] && IMAGE="$IMAGE_ARM"
      echo "    trying $SIZE in $LOCATION$([ "$ZONE" != any ] && echo " zone $ZONE") ..."
      if az vm create -g "$RESOURCE_GROUP" -n "$VM_NAME" -l "$LOCATION" --image "$IMAGE" --size "$SIZE" \
          --admin-username "$ADMIN" --generate-ssh-keys --public-ip-sku Standard \
          --public-ip-address-dns-name "$DNS_LABEL" --os-disk-size-gb 32 --storage-sku StandardSSD_LRS \
          ${ZONE_ARGS[@]+"${ZONE_ARGS[@]}"} -o none 2>"$ERR"; then
        CREATED="$SIZE in $LOCATION$([ "$ZONE" != any ] && echo " zone $ZONE" || true)"; break 3
      fi
      if grep -qE "NotAvailableForSubscription" "$ERR"; then
        echo "      size restricted for this subscription (needs a support request, not quota); next option"
        REASONS="$REASONS restricted"
      elif grep -qE "SkuNotAvailable|Capacity" "$ERR"; then
        echo "      no capacity for this subscription; next option"
        REASONS="$REASONS capacity"
      elif grep -qiE "quota" "$ERR"; then
        echo "      no quota: $(grep -oiE "[A-Za-z ]*family[A-Za-z ]*(cores|vCPUs)[^.]*|Current Limit[^.]*" "$ERR" | head -1)"
        REASONS="$REASONS quota"
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
  done
  if [ -z "$CREATED" ]; then
    cat >&2 <<NONE

None of the regions/sizes worked. Reasons seen: $(for r in $REASONS; do echo "$r"; done | sort | uniq -c | awk '{printf "%s x%s  ", $2, $1}')
  quota      -> portal: Quotas -> Compute -> East US 2 -> "Standard BS Family vCPUs" -> request 2
                (also "Total Regional vCPUs" if it is 0); usually approved automatically.
  restricted -> Help + support -> Create a support request -> "Service and subscription limits
                (quotas)" -> "Compute-VM (cores-vCPUs) subscription limit increases", and ask them to
                enable Standard_B1ms (BS family) in East US 2 for this subscription.
  capacity   -> temporary; try again later.
Then rerun, pinned to the region you requested: LOCATION=eastus2 SIZE=Standard_B1ms $0
NONE
    exit 1
  fi
  echo "    created: $CREATED"
  az vm open-port -g "$RESOURCE_GROUP" -n "$VM_NAME" --port 80,443 --priority 1010 -o none
fi

# Networking, whether this script or the portal created the VM: find the VM's network security
# group and public IP through its network card, open 80/443, limit SSH to this computer, and give
# the public IP a DNS name if it has none (the portal doesn't add one).
MY_IP="$(curl -fsS https://api.ipify.org)"
NIC_ID="$(az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" --query "networkProfile.networkInterfaces[0].id" -o tsv)"
NSG_ID="$(az network nic show --ids "$NIC_ID" --query networkSecurityGroup.id -o tsv)"
if [ -z "$NSG_ID" ]; then      # portal "Basic" networking may put the NSG on the subnet instead
  SUBNET_ID="$(az network nic show --ids "$NIC_ID" --query "ipConfigurations[0].subnet.id" -o tsv)"
  NSG_ID="$(az network vnet subnet show --ids "$SUBNET_ID" --query networkSecurityGroup.id -o tsv)"
fi
[ -n "$NSG_ID" ] || { echo "The VM has no network security group; add one in the portal (Networking)." >&2; exit 1; }
NSG="$(basename "$NSG_ID")"; NSG_RG="$(echo "$NSG_ID" | cut -d/ -f5)"
rule_for() {   # name of the inbound allow rule for a port, if any
  az network nsg rule list -g "$NSG_RG" --nsg-name "$NSG" --query \
    "[?direction=='Inbound' && access=='Allow' && (destinationPortRange=='$1' || contains(destinationPortRanges, '$1'))].name | [0]" -o tsv
}
for port_prio in "80 1010" "443 1011"; do
  set -- $port_prio
  [ -n "$(rule_for "$1")" ] || az network nsg rule create -g "$NSG_RG" --nsg-name "$NSG" -n "allow-$1" \
    --priority "$2" --direction Inbound --access Allow --protocol Tcp --destination-port-ranges "$1" -o none
done
SSH_RULE="$(rule_for 22)"
if [ -n "$SSH_RULE" ]; then
  az network nsg rule update -g "$NSG_RG" --nsg-name "$NSG" -n "$SSH_RULE" --source-address-prefixes "$MY_IP/32" -o none
else
  az network nsg rule create -g "$NSG_RG" --nsg-name "$NSG" -n allow-ssh-admin --priority 1000 --direction Inbound \
    --access Allow --protocol Tcp --destination-port-ranges 22 --source-address-prefixes "$MY_IP/32" -o none
fi
FQDN="$(az vm show -d -g "$RESOURCE_GROUP" -n "$VM_NAME" --query fqdns -o tsv)"
if [ -z "$FQDN" ]; then
  PIP_ID="$(az network nic show --ids "$NIC_ID" --query "ipConfigurations[0].publicIPAddress.id" -o tsv)"
  [ -n "$PIP_ID" ] || { echo "The VM has no public IP address; add one in the portal (Networking)." >&2; exit 1; }
  az network public-ip update --ids "$PIP_ID" --dns-name "$DNS_LABEL" -o none
  FQDN="$(az network public-ip show --ids "$PIP_ID" --query dnsSettings.fqdn -o tsv)"
fi
HOST="$ADMIN@$FQDN"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$HOST")
PROBE=(ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$HOST")   # -n: never reads your keyboard
echo "    Address: https://$FQDN   (SSH allowed from $MY_IP)"

echo "==> Waiting for the VM to accept SSH"
for _ in $(seq 1 30); do "${PROBE[@]}" true 2>/dev/null && break; sleep 10; done

SITE_USER=""; SITE_PASS=""
if [ "$FIRST_RUN" = 1 ] || [ "${SITE_PASSWORD_RESET:-0}" = 1 ] || ! "${PROBE[@]}" test -s /etc/caddy/site-user 2>/dev/null; then
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
