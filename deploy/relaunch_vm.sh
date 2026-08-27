#!/usr/bin/env bash
# Launch a REPLACEMENT Always Free VM with an SSH key you actually hold.
#
# Run this IN ORACLE CLOUD SHELL (it needs the oci CLI and your tenancy auth) —
# unlike the other two scripts in deploy/, Cloud Shell is the RIGHT place for
# this one. It only calls the OCI API; it installs nothing here.
#
# Use it when you are locked out of an existing VM: `ssh -v` shows your key
# being offered and refused, which means the public key was never installed
# on that instance. There is no way to inject one into a running instance,
# so the fix is a new instance created WITH your key.
#
#   curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/relaunch_vm.sh | bash -s -- btst-trading-bot-2
#
# The old VM is left running and untouched. Always Free allows two micro
# instances, so this usually succeeds without terminating anything.

set -euo pipefail

OLD_NAME="${1:-}"
NEW_NAME="${2:-btst-bot-$(date +%m%d%H%M)}"
PUBKEY="${PUBKEY:-$HOME/.ssh/btst_key.pub}"

die() { echo "ERROR: $*" >&2; exit 1; }
# Trim whitespace AND newlines — a bare `tr -d '[],"'` leaves a leading \n on
# --raw-output values, which the API then rejects as a 404 on a mangled OCID.
clean() { tr -d '[]",' | tr -d '[[:space:]]'; }

command -v oci >/dev/null || die "oci CLI not found. Run this in Oracle Cloud Shell."
[ -n "$OLD_NAME" ] || die "Usage: relaunch_vm.sh <existing-vm-display-name> [new-name]"
[ -f "$PUBKEY" ] || die "No public key at $PUBKEY. Set PUBKEY=/path/to/key.pub, or generate one:
  ssh-keygen -t rsa -b 4096 -f ~/.ssh/btst_key -N ''"
[ -n "${OCI_TENANCY:-}" ] || die "OCI_TENANCY is unset — are you in Cloud Shell?"

echo "==> Locating '$OLD_NAME' to copy its placement"
OLD_ID="$(oci compute instance list -c "$OCI_TENANCY" --all \
  --query "data[?\"display-name\"=='${OLD_NAME}'].id | [0]" --raw-output 2>/dev/null | clean)"
[ -n "$OLD_ID" ] && [ "$OLD_ID" != "None" ] || die "No instance named '$OLD_NAME' in this tenancy."

COMPARTMENT="$(oci compute instance get --instance-id "$OLD_ID" --query 'data."compartment-id"' --raw-output | clean)"
AD="$(oci compute instance get --instance-id "$OLD_ID" --query 'data."availability-domain"' --raw-output)"
SHAPE="$(oci compute instance get --instance-id "$OLD_ID" --query 'data.shape' --raw-output | clean)"
SUBNET="$(oci compute instance list-vnics --instance-id "$OLD_ID" --query 'data[0]."subnet-id"' --raw-output | clean)"

# Reusing the old VM's subnet matters: its security list already opens port 22.
[ -n "$SUBNET" ] && [ "$SUBNET" != "None" ] || die "Could not read the old VM's subnet."

echo "    shape=$SHAPE"
echo "    ad=$AD"
echo "    subnet=$SUBNET"

echo "==> Newest Ubuntu image for $SHAPE"
IMAGE="$(oci compute image list -c "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
  --shape "$SHAPE" --sort-by TIMECREATED --limit 1 --query 'data[0].id' --raw-output 2>/dev/null | clean)"
[ -n "$IMAGE" ] && [ "$IMAGE" != "None" ] || die "No Ubuntu image available for shape $SHAPE."
echo "    image=$IMAGE"

# Flex shapes must be told how many OCPUs/GB; fixed shapes must NOT be.
EXTRA=()
case "$SHAPE" in
  *Flex*)
    OCPUS="$(oci compute instance get --instance-id "$OLD_ID" --query 'data."shape-config".ocpus' --raw-output | clean)"
    MEM="$(oci compute instance get --instance-id "$OLD_ID" --query 'data."shape-config"."memory-in-gbs"' --raw-output | clean)"
    EXTRA=(--shape-config "{\"ocpus\":${OCPUS:-1},\"memoryInGBs\":${MEM:-6}}")
    echo "    shape-config: ocpus=${OCPUS:-1} memory=${MEM:-6}GB"
    ;;
esac

echo "==> Launching '$NEW_NAME' with $(basename "$PUBKEY")"
echo "    key fingerprint: $(ssh-keygen -lf "$PUBKEY" | awk '{print $2}')"
NEW_ID="$(oci compute instance launch \
  -c "$COMPARTMENT" --availability-domain "$AD" --shape "$SHAPE" \
  --subnet-id "$SUBNET" --image-id "$IMAGE" \
  --display-name "$NEW_NAME" --assign-public-ip true \
  "${EXTRA[@]}" \
  --metadata "{\"ssh_authorized_keys\":\"$(cat "$PUBKEY")\"}" \
  --wait-for-state RUNNING \
  --query 'data.id' --raw-output | clean)"
[ -n "$NEW_ID" ] || die "Launch failed. If it said 'Out of host capacity', retry later or
terminate the old VM to free an Always Free slot."

NEW_IP="$(oci compute instance list-vnics --instance-id "$NEW_ID" --query 'data[0]."public-ip"' --raw-output | clean)"

PRIVKEY="${PUBKEY%.pub}"
cat <<DONE

================================================================
 '$NEW_NAME' is RUNNING at $NEW_IP
================================================================
The old VM was NOT touched. Next, from this Cloud Shell:

  ssh -i ${PRIVKEY} ubuntu@${NEW_IP}

Then, INSIDE that SSH session:

  curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/oracle_vm_setup.sh | bash
  nano ~/.btst.env          # TELEGRAM_* and ANGELONE_* secrets
  sudo systemctl restart btst-watcher
  ~/nifty-btst-bot/.venv/bin/python ~/nifty-btst-bot/btst_engine.py selftest

The selftest must land a Telegram message before you trust it with a trade.
Boot can take a minute — if SSH refuses at first, wait and retry.
DONE
