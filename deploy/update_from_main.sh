#!/usr/bin/env bash
# Update an EXISTING Oracle VM install to origin/main.
# Does NOT wipe ~/.btst_state.json, does NOT recreate ~/.btst.env,
# does NOT re-enable GitHub Actions, does NOT recreate the venv.
#
# Run ON the VM (not in Grok chat):
#   curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/update_from_main.sh | bash

set -euo pipefail

# --- Wrong-host guard -------------------------------------------------------
# This bot needs systemd + cron on a machine that stays up. Oracle Cloud Shell
# is an ephemeral browser container: no systemd, and everything is destroyed
# when the tab closes. Detect it BEFORE doing any work.
_is_cloud_shell() {
  case "$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')" in
    *cloudshell*|*cloud-shell*) return 0 ;;
  esac
  [ -n "${OCI_CLOUD_SHELL:-}" ] && return 0
  [ -n "${CLOUD_SHELL_TOOL_CONFIG:-}" ] && return 0
  [ -n "${OCI_CS_HOME:-}" ] && return 0
  [ -d /etc/oci-cloud-shell ] && return 0
  return 1
}

# Canonical "systemd is PID 1 and running" test.
_has_systemd() { [ -d /run/systemd/system ]; }

_wrong_host_banner() {
  echo
  echo "================================================================"
  echo " WRONG MACHINE — this host cannot run the BTST bot."
  echo "================================================================"
  if _is_cloud_shell; then
    echo
    echo "You are in Oracle Cloud Shell (the browser terminal). It is"
    echo "ephemeral: no systemd, and the container is wiped when the tab"
    echo "closes. The watcher would stop the moment you walk away."
  else
    echo
    echo "This host has no running systemd (/run/systemd/system missing),"
    echo "so btst-watcher.service cannot be installed or kept alive."
  fi
  echo
  echo "Do this instead, from THIS Cloud Shell:"
  echo
  echo "  1. Find your Always Free Compute VM's public IP:"
  echo "       oci compute instance list --compartment-id \\"
  echo "         \"\$(oci iam compartment list --all --query 'data[0].id' --raw-output)\" \\"
  echo "         --query 'data[*].{name:\"display-name\",state:\"lifecycle-state\"}' --output table"
  echo
  echo "  2. SSH in with the key you actually have (check: ls ~/.ssh):"
  echo "       chmod 600 ~/.ssh/YOUR_KEY"
  echo "       ssh -i ~/.ssh/YOUR_KEY ubuntu@YOUR_VM_IP     # Ubuntu images"
  echo "       ssh -i ~/.ssh/YOUR_KEY opc@YOUR_VM_IP        # Oracle Linux images"
  echo
  echo "     The prompt must change away from 'cloudshell' before step 3."
  echo
  echo "  3. Re-run this same curl | bash line INSIDE that SSH session."
  echo
  echo "If you have no Compute instance yet, create an Always Free VM first."
  echo "Do NOT install the bot in Cloud Shell."
  echo
}

if _is_cloud_shell || ! _has_systemd; then
  _wrong_host_banner
  exit 1
fi
# ---------------------------------------------------------------------------

APP_DIR="${HOME}/nifty-btst-bot"
ENV_FILE="${HOME}/.btst.env"
STATE_FILE="${HOME}/.btst_state.json"
echo "==> This script never deletes ${STATE_FILE}"
if [ -f "$STATE_FILE" ]; then
  ls -l "$STATE_FILE"
else
  echo "NOTE: ${STATE_FILE} does not exist yet (ok if you have never had a position)."
fi

if [ ! -d "${APP_DIR}/.git" ]; then
  echo
  echo "ERROR: ${APP_DIR} is not a git clone."
  echo
  echo "This host has systemd but no bot install yet. Run first-time setup:"
  echo "  curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/oracle_vm_setup.sh | bash"
  exit 1
fi

echo "==> Pull origin/main"
cd "$APP_DIR"
git fetch origin
git checkout main
git pull --ff-only origin main
git log -1 --oneline

echo "==> Env (values hidden)"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: ${ENV_FILE} missing. Copy secrets in before the bot can run."
  exit 1
fi
if grep -q '^DATA_PROVIDER=yahoo' "$ENV_FILE"; then
  sed -i 's/^DATA_PROVIDER=yahoo/DATA_PROVIDER=angelone/' "$ENV_FILE"
  echo "flipped DATA_PROVIDER yahoo -> angelone"
fi
if ! grep -q '^DATA_PROVIDER=' "$ENV_FILE"; then
  echo 'DATA_PROVIDER=angelone' >> "$ENV_FILE"
  echo "added DATA_PROVIDER=angelone"
fi
grep '^DATA_PROVIDER=' "$ENV_FILE"
grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|ANGELONE_API_KEY|ANGELONE_CLIENT_ID|ANGELONE_PASSWORD|ANGELONE_TOTP_SECRET|BTST_STATE_FILE)=' "$ENV_FILE" \
  | sed 's/=.*/=SET/' || true
missing=0
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID ANGELONE_API_KEY ANGELONE_CLIENT_ID ANGELONE_PASSWORD ANGELONE_TOTP_SECRET; do
  val="$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -z "$val" ]; then
    echo "MISSING: ${key} is empty in ${ENV_FILE}"
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "Fill the missing keys in ${ENV_FILE}, then: sudo systemctl restart btst-watcher"
  exit 1
fi

echo "==> Python deps (venv kept, yfinance no longer required)"
if [ ! -x "${APP_DIR}/.venv/bin/pip" ]; then
  echo "ERROR: ${APP_DIR}/.venv missing. Re-run deploy/oracle_vm_setup.sh once."
  exit 1
fi
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Restart watcher"
sudo systemctl restart btst-watcher
sleep 3
sudo systemctl is-active btst-watcher
sudo systemctl status btst-watcher --no-pager -l | tail -20 || true
echo "==> watcher.log (last 40)"
tail -40 "${APP_DIR}/watcher.log" || echo "(no watcher.log yet)"

echo "==> Selftest (Telegram + Angel One)"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
"${APP_DIR}/.venv/bin/python" "${APP_DIR}/btst_engine.py" selftest

echo
echo "Done. State file was not touched. GitHub Actions schedule stays off."
echo "Confirm Telegram got a SELFTEST. Watcher should be active."
