#!/usr/bin/env bash
# One-time setup for running the NIFTY BTST bot on an Oracle Cloud Always
# Free VM (or any Ubuntu/Debian box you leave running) using real cron
# instead of GitHub Actions' best-effort scheduler.
#
# Run this ONCE after SSH-ing into your VM as a normal (non-root) user:
#   curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/oracle_vm_setup.sh | bash
# or copy this file over and run: bash oracle_vm_setup.sh
#
# What it does:
#   1. Sets the VM's timezone to IST, so a plain "9-15 * * 1-5" cron entry
#      means what it looks like — no UTC math, unlike GitHub Actions cron.
#   2. Installs Python3 + git + cron if missing.
#   3. Clones (or updates) this repo into ~/nifty-btst-bot.
#   4. Creates a Python venv and installs requirements.txt into it.
#   5. Creates ~/.btst.env the first time only — a secrets file (chmod 600,
#      deliberately kept OUTSIDE the git repo so a later `git pull` can
#      never touch it) with empty placeholders. Nothing gets sent until you
#      fill these in.
#   6. Creates deploy/run_engine.sh, a wrapper that loads ~/.btst.env and
#      runs the engine once (this is what cron calls — daily entry decision
#      on every provider, plus 30m exit monitoring as a Yahoo fallback).
#   7. Installs a crontab entry running that wrapper every 5 minutes,
#      9:00–15:35 IST, Monday–Friday. Re-running this script is safe: it
#      replaces its own crontab line instead of duplicating it.
#   8. Installs and enables btst-watcher.service (systemd) — a persistent
#      tick-level exit monitor that only does anything when DATA_PROVIDER=
#      angelone (needs live per-tick option/index quotes Yahoo doesn't
#      have); on any other provider it just idles. It's what actually owns
#      exit monitoring on Angel One — the cron path steps aside for it
#      automatically. Runs continuously, auto-restarts on crash, survives
#      reboots.
#
# State (open position, last scan date, last candle reported) lives in
# ~/.btst_state.json — outside the git working tree, so pulling a later
# code update can never conflict with or overwrite it. Both cron and the
# watcher service read/write it, coordinated by a file lock (see
# watcher.py's _locked_state()) so the two can never corrupt each other.

set -euo pipefail

REPO_URL="https://github.com/Harshdpsinh/nifty-btst-bot.git"
APP_DIR="$HOME/nifty-btst-bot"
ENV_FILE="$HOME/.btst.env"
STATE_FILE="$HOME/.btst_state.json"
RUN_SCRIPT="$APP_DIR/deploy/run_engine.sh"

echo "==> Setting timezone to Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git cron

echo "==> Fetching the bot into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating virtualenv and installing dependencies"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Creating $ENV_FILE — fill this in before the bot can send anything"
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATA_PROVIDER=yahoo
ANGELONE_API_KEY=
ANGELONE_CLIENT_ID=
ANGELONE_PASSWORD=
ANGELONE_TOTP_SECRET=
BTST_STATE_FILE=$STATE_FILE
EOF
  chmod 600 "$ENV_FILE"
else
  echo "==> $ENV_FILE already exists — leaving it as-is"
fi

mkdir -p "$APP_DIR/deploy"
cat > "$RUN_SCRIPT" <<EOF
#!/usr/bin/env bash
# Loads secrets from $ENV_FILE and runs one engine cycle. Invoked by cron;
# safe to run by hand any time to test.
set -a
source "$ENV_FILE"
set +a
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" btst_engine.py auto >> "$APP_DIR/engine.log" 2>&1
EOF
chmod +x "$RUN_SCRIPT"

echo "==> Installing crontab entry (every 5 min, 9:00-15:35 IST, Mon-Fri)"
CRON_LINE="*/5 9-15 * * 1-5 $RUN_SCRIPT"
# `|| true` matters: under `set -e`, grep -v exiting 1 (nothing to filter on a
# first run / empty crontab) would otherwise abort this subshell before the
# echo runs, silently installing an EMPTY crontab.
( crontab -l 2>/dev/null | grep -vF "$RUN_SCRIPT" || true ; echo "$CRON_LINE" ) | crontab -

echo "==> Installing btst-watcher.service (tick-level exit monitor, Angel One only)"
SERVICE_NAME="btst-watcher.service"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"
sed -e "s|__USER__|$(whoami)|g" -e "s|__HOME__|$HOME|g" \
  "$APP_DIR/deploy/btst-watcher.service" | sudo tee "$SERVICE_DST" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

cat <<EOF

Done. Next steps:
  1. Edit $ENV_FILE with your real TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
     (and ANGELONE_* if you're switching DATA_PROVIDER to angelone).
  2. Test it right now:
       $RUN_SCRIPT && tail -30 $APP_DIR/engine.log
  3. Once that Telegram message arrives, you're live — cron runs this
     automatically every 5 minutes during market hours from now on, with a
     real per-second clock instead of GitHub Actions' best-effort queue.
  4. If you're using DATA_PROVIDER=angelone: after filling in the
     ANGELONE_* secrets in $ENV_FILE, restart the watcher so it picks them
     up:
       sudo systemctl restart btst-watcher
     Check it's alive:
       sudo systemctl status btst-watcher
       tail -30 $APP_DIR/watcher.log
     On any other provider it's safe to leave enabled -- it just idles.
  5. Go disable the GitHub Actions schedule (see the README's "Running on
     your own server" section) so you stop getting duplicate notifications
     from both places.
EOF
