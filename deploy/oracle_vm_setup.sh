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
#   2. Installs git + cron, and Python 3.11 specifically (via the deadsnakes
#      PPA if not already present) rather than trusting whatever "python3"
#      resolves to. requirements.txt pins pandas/numpy versions that dropped
#      Python 3.8 support, and Ubuntu 20.04 -- a common, still-offered Oracle
#      image -- ships 3.8 by default, which fails pip install with no
#      matching distribution found.
#   3. Clones (or updates) this repo into ~/nifty-btst-bot.
#   4. Creates a Python venv and installs requirements.txt into it.
#   5. Creates ~/.btst.env the first time only — a secrets file (chmod 600,
#      deliberately kept OUTSIDE the git repo so a later `git pull` can
#      never touch it) with empty placeholders. Nothing gets sent until you
#      fill these in.
#   6. Creates deploy/run_engine.sh, a wrapper that loads ~/.btst.env and
#      runs the engine once (this is what cron calls — daily entry decision
#      plus a 30m exit fallback if the watcher heartbeat is stale).
#   7. Installs a crontab entry running that wrapper every 5 minutes,
#      9:00–15:35 IST, Monday–Friday. Re-running this script is safe: it
#      replaces its own crontab line instead of duplicating it.
#   8. Installs and enables btst-watcher.service (systemd) — a persistent
#      tick-level exit monitor. Cron covers 30m exits if the watcher's
#      heartbeat goes stale. Runs continuously, auto-restarts on crash,
#      survives reboots.
#
# State (open position, last scan date, last candle reported) lives in
# ~/.btst_state.json — outside the git working tree, so pulling a later
# code update can never conflict with or overwrite it. Both cron and the
# watcher service read/write it, coordinated by a file lock (see
# watcher.py / btst_engine.locked_state()) so the two can never corrupt each other.

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
sudo apt-get install -y software-properties-common python3 python3-venv python3-pip git cron

# Try for Python 3.11 via deadsnakes, but this is best-effort: deadsnakes
# stops building for an Ubuntu release once it goes end-of-life, so on an
# older/EOL box (e.g. 20.04 "focal", EOL May 2025) this can legitimately
# have nothing to install. Never let that abort the whole setup -- fall
# back to whatever "python3" already is on the system instead.
PYTHON="python3"
if ! command -v python3.11 >/dev/null 2>&1; then
  echo "==> Python 3.11 not found — trying the deadsnakes PPA (best-effort)"
  sudo add-apt-repository -y ppa:deadsnakes/ppa || true
  sudo apt-get update -y || true
fi
if sudo apt-get install -y python3.11 python3.11-venv python3.11-dev 2>/dev/null; then
  PYTHON="python3.11"
else
  echo "==> python3.11 unavailable for this OS release — falling back to $($PYTHON --version)"
fi

echo "==> Fetching the bot into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating virtualenv ($PYTHON) and installing dependencies"
cd "$APP_DIR"
# --clear: if a venv already exists from an older run of this script (e.g.
# one made with a different Python before this fix), wipe and recreate it
# cleanly rather than leaving a stale/mismatched environment behind.
"$PYTHON" -m venv --clear .venv
.venv/bin/pip install --upgrade pip --quiet

# requirements.txt pins exact versions (deliberately, so a scheduled job
# never silently picks up a breaking release) that need a fairly modern
# Python. If that Python wasn't available above and we fell back to an
# older system "python3", those exact pins can be impossible to satisfy --
# fall back to installing the same packages unpinned so pip resolves
# whatever versions actually work on this interpreter, rather than leaving
# the whole setup dead in the water.
if ! .venv/bin/pip install -r requirements.txt --quiet; then
  echo "==> Exact-pinned versions don't support this Python — retrying unpinned"
  grep -v '^#' requirements.txt | sed 's/==.*//' | xargs .venv/bin/pip install --quiet
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Creating $ENV_FILE — fill this in before the bot can send anything"
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATA_PROVIDER=angelone
ANGELONE_API_KEY=
ANGELONE_CLIENT_ID=
ANGELONE_PASSWORD=
ANGELONE_TOTP_SECRET=
BTST_STATE_FILE=$STATE_FILE
EOF
  chmod 600 "$ENV_FILE"
else
  echo "==> $ENV_FILE already exists — leaving it as-is"
  if grep -q '^DATA_PROVIDER=yahoo' "$ENV_FILE" 2>/dev/null; then
    sed -i 's/^DATA_PROVIDER=yahoo/DATA_PROVIDER=angelone/' "$ENV_FILE"
    echo "==> Flipped DATA_PROVIDER yahoo -> angelone in $ENV_FILE (Yahoo was removed)"
  fi
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
     and ANGELONE_* (Yahoo was removed — Angel One only).
  2. Test it right now:
       $RUN_SCRIPT && tail -30 $APP_DIR/engine.log
  3. Once that Telegram message arrives, you're live — cron runs this
     automatically every 5 minutes during market hours from now on, with a
     real per-second clock instead of GitHub Actions' best-effort queue.
  4. After filling in ANGELONE_* secrets, restart the watcher so it picks
     them up (EnvironmentFile is only read at service start):
       sudo systemctl restart btst-watcher
       sudo systemctl status btst-watcher
       tail -30 $APP_DIR/watcher.log
  5. GitHub Actions has no schedule. Keep it disabled. Use workflow_dispatch
     only for an occasional selftest.
EOF
