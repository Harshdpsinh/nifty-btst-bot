#!/usr/bin/env bash
# Update an EXISTING Oracle VM install to origin/main.
# Does NOT wipe ~/.btst_state.json, does NOT recreate ~/.btst.env,
# does NOT re-enable GitHub Actions, does NOT recreate the venv.
#
# Run ON the VM (not in Grok chat):
#   curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/update_from_main.sh | bash

set -euo pipefail

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
  echo "ERROR: ${APP_DIR} is not a git clone. Run deploy/oracle_vm_setup.sh once first."
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
