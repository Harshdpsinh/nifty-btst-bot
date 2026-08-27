"""NIFTY 50 BTST scanner — 3:20 PM entry scan + 30m Heikin-Ashi exit monitor.

Playbook: buy near the close, hold ONE night, exit on the next session's
own 30m candles. Never two nights. Angel One SmartAPI only.

Usage:
    python btst_engine.py auto        # decide from the IST clock (default)
    python btst_engine.py entry
    python btst_engine.py exit
    python btst_engine.py selftest    # verify Telegram + data plumbing
    python btst_engine.py fill 108.50 # set actual fill so 2x partial is right
    python btst_engine.py entry --force   # ignore all clock gating
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import pandas as pd
import pytz
import requests

from providers import get_provider

IST = pytz.timezone("Asia/Kolkata")

# ----------------------- STRATEGY (do not change) -----------------------
SYMBOL = "^NSEI"
DIVERGENCE_THRESHOLD = 11.0          # points, applied symmetrically
HARD_EXIT_TIME = dt.time(15, 13)     # square-off cutoff
ENTRY_WINDOW = "3:21 PM - 3:28 PM IST"
TARGET_PREMIUM = "~Rs.100"
TARGET_PREMIUM_VALUE = 100.0         # numeric form, used to pick the nearest strike

PARTIAL_PROFIT_MULTIPLIER = 2.0
PARTIAL_PROFIT_FRACTION = 0.5

WEEKLY_EXPIRY_WEEKDAY = 1  # Tuesday
# ------------------------------------------------------------------------

# --- scheduling gates (operational, not strategy) ---
ENTRY_ACTIONABLE_FROM = dt.time(15, 18)   # earliest a signal may be acted on
ENTRY_LATE_LIMIT = dt.time(15, 28)        # after this the window has closed
AUTO_ENTRY_UNTIL = dt.time(20, 0)         # still report a *missed* window until here
EXIT_MONITOR_FROM = dt.time(9, 45)        # first 30m candle close — first level can arm
LEFTOVER_WATCH_FROM = dt.time(9, 15)      # leftover square-off can fire at the open
EXIT_MONITOR_UNTIL = dt.time(15, 16)      # just past HARD_EXIT_TIME

DAILY_LOOKBACK_DAYS = 10
INTRADAY_LOOKBACK_DAYS = 5
INTRADAY_INTERVAL_MIN = 30
MAX_INTRADAY_STALENESS_MIN = 90
REQUEST_TIMEOUT = 10
TELEGRAM_MAX_CHARS = 4096
TELEGRAM_SEND_RETRIES = 3
TELEGRAM_RETRY_BACKOFF_SEC = 2
WATCHER_STALE_SECONDS = 120          # cron covers exits if heartbeat older than this

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_PATH = pathlib.Path(os.getenv("BTST_STATE_FILE", "state.json"))
LOCK_PATH = STATE_PATH.with_suffix(".lock")
# Send the routine 30m status update even when no position is open.
STATUS_WHEN_FLAT = os.getenv("BTST_STATUS_WHEN_FLAT", "1") not in ("0", "false", "False")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("btst")

PROVIDER = get_provider()
log.info("Data provider: %s", PROVIDER.name)

_send_failures = 0


class StaleDataError(RuntimeError):
    """Raised when the feed returns data too old to act on."""


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d %H:%M:%S IST")


def nse_30m_bucket_start(now: dt.datetime) -> dt.datetime:
    """Start of the current 30m NSE bucket (session opens 9:15 → :15/:45)."""
    minute = now.minute
    if minute >= 45:
        return now.replace(minute=45, second=0, microsecond=0)
    if minute >= 15:
        return now.replace(minute=15, second=0, microsecond=0)
    return now.replace(minute=45, second=0, microsecond=0) - dt.timedelta(hours=1)


def ist_minute(ts) -> pd.Timestamp:
    """Timezone-safe minute stamp so API bars and local clocks compare equal."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(IST)
    else:
        t = t.tz_convert(IST)
    return t.floor("min")


def _bar_start(idx) -> pd.Timestamp:
    return ist_minute(idx)


def split_closed_and_forming(day_df: pd.DataFrame, now: dt.datetime) -> tuple[pd.DataFrame, pd.Series | None]:
    """Today's fully closed 30m bars vs the still-forming bucket (if the API returned it).

    A bar is closed when its start < current NSE 30m bucket. The last row is
    NOT blindly treated as forming — that was arming one candle late whenever
    getCandleData omitted the in-progress bar.
    """
    if day_df is None or day_df.empty:
        empty = day_df if day_df is not None else pd.DataFrame()
        return empty, None
    bucket = ist_minute(nse_30m_bucket_start(now))
    closed_idx = []
    forming: pd.Series | None = None
    for idx, row in day_df.iterrows():
        start = ist_minute(idx)
        if start < bucket:
            closed_idx.append(idx)
        elif start == bucket:
            forming = row
    closed = day_df.loc[closed_idx] if closed_idx else day_df.iloc[0:0]
    return closed, forming


def sticky_refs(closed: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    """Most recently closed red / green of TODAY. Opposite colour does not clear."""
    ref_red = None
    ref_green = None
    if closed is None or closed.empty:
        return None, None
    for _, row in closed.iterrows():
        if row["Is_Red"]:
            ref_red = row
        elif row["Is_Green"]:
            ref_green = row
    return ref_red, ref_green


def _opened_date(position: dict | None) -> dt.date | None:
    if not position:
        return None
    raw = position.get("opened_date")
    if not raw:
        return None
    if isinstance(raw, dt.date) and not isinstance(raw, dt.datetime):
        return raw
    return dt.date.fromisoformat(str(raw)[:10])


def is_same_day_position(position: dict | None, today: dt.date) -> bool:
    """Phase 1 just fired — hold overnight, do not use today's candles to exit."""
    opened = _opened_date(position)
    return opened is not None and opened == today


def is_leftover_position(position: dict | None, today: dt.date) -> bool:
    """Already had its one exit session and is still open — never a second night."""
    opened = _opened_date(position)
    if opened is None or opened >= today:
        return False
    exit_session = position.get("exit_session_date")
    if not exit_session:
        return False
    return str(exit_session)[:10] < today.isoformat()


def mark_exit_session(position: dict, today: dt.date) -> None:
    if position is not None and not position.get("exit_session_date"):
        position["exit_session_date"] = today.isoformat()


def _next_option_expiry(today: dt.date) -> tuple[dt.date, str]:
    """Skip current week's expiry on Mon/Tue; else nearest Tuesday. MONTHLY if last Tue of month."""
    days_ahead = (WEEKLY_EXPIRY_WEEKDAY - today.weekday()) % 7
    candidate = today + dt.timedelta(days=days_ahead)
    if today.weekday() in (0, 1):
        candidate += dt.timedelta(days=7)
    is_monthly = (candidate + dt.timedelta(days=7)).month != candidate.month
    return candidate, ("MONTHLY" if is_monthly else "WEEKLY")


def watcher_heartbeat_fresh(state: dict, now: dt.datetime | None = None) -> bool:
    now = now or _now()
    raw = state.get("watcher_heartbeat")
    if not raw:
        return False
    try:
        ts = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    return (now - ts).total_seconds() < WATCHER_STALE_SECONDS


# --------------------------- notifications ---------------------------


def send_telegram(message: str) -> bool:
    """Send a plain-text notification. Never raises; returns delivery success.

    parse_mode is deliberately NOT set: these messages are plain text, and
    error messages interpolate exception text that can contain '<', '>' or
    '&'. With parse_mode=HTML those get rejected by Telegram with a 400 and
    the alert you most need is the one that never arrives.
    """
    global _send_failures

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Telegram credentials missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). "
            "Console output:\\n%s",
            message,
        )
        _send_failures += 1
        return False

    if len(message) > TELEGRAM_MAX_CHARS:
        message = message[: TELEGRAM_MAX_CHARS - 3] + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    last_err: Exception | None = None
    for attempt in range(1, TELEGRAM_SEND_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                raise ValueError(f"Telegram replied ok=false: {body}")
            log.info("Telegram delivered (%d chars, message_id=%s).",
                     len(message), body.get("result", {}).get("message_id"))
            return True
        except (requests.RequestException, ValueError) as e:
            last_err = e
            detail = ""
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                detail = f" | response: {resp_obj.text[:300]}"
            log.warning("Telegram send attempt %d/%d failed: %s%s",
                        attempt, TELEGRAM_SEND_RETRIES, e, detail)
            if attempt < TELEGRAM_SEND_RETRIES:
                time.sleep(TELEGRAM_RETRY_BACKOFF_SEC * attempt)

    log.error("Telegram send FAILED after %d attempts: %s\\nMessage was:\\n%s",
              TELEGRAM_SEND_RETRIES, last_err, message)
    _send_failures += 1
    return False


# ------------------------------ state ------------------------------

_DEFAULT_STATE: dict = {
    "entry_scan_date": None,
    "position": None,
    "last_status_candle": None,
    "last_exit_signal_candle": None,
    "watcher_last_status_bucket": None,
    "watcher_heartbeat": None,
    "watcher_down_alert_date": None,
}


def load_state() -> dict:
    state = dict(_DEFAULT_STATE)
    if not STATE_PATH.exists():
        log.info("No state file at %s — starting fresh.", STATE_PATH)
        return state
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("State file unreadable (%s) — starting fresh.", e)
        return state
    if isinstance(data, dict):
        state.update(data)
    return state


def save_state(state: dict) -> None:
    """Atomic replace so a crash cannot leave truncated JSON (which would drop the position)."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, STATE_PATH)
        log.debug("State saved to %s: position=%s entry_scan_date=%s",
                  STATE_PATH, state.get("position"), state.get("entry_scan_date"))
    except OSError as e:
        log.error("Could not persist state to %s: %s", STATE_PATH, e)


@contextlib.contextmanager
def locked_state():
    """File-locked read-modify-write. Cron and watcher share this lock."""
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, "w") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            state = load_state()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                save_state(state)
            else:
                log.info("State unchanged.")
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


# ------------------------------ data ------------------------------


def _last_timestamp_ist(df: pd.DataFrame) -> pd.Timestamp:
    ts = pd.Timestamp(df.index[-1])
    return ts.tz_convert(IST) if ts.tzinfo is not None else ts.tz_localize(IST)


def get_live_daily_data() -> tuple[float, float]:
    """Return (live spot, forming daily Heikin-Ashi close)."""
    df = PROVIDER.daily_bars(SYMBOL, DAILY_LOOKBACK_DAYS)

    last_date = _last_timestamp_ist(df).date()
    today = _now().date()
    if last_date != today:
        raise StaleDataError(
            f"Latest daily candle is {last_date}, not {today} — market closed "
            f"(weekend / NSE holiday) or feed lagging."
        )

    live_spot = float(df["Close"].iloc[-1])
    open_price = float(df["Open"].iloc[-1])
    high_price = float(df["High"].iloc[-1])
    low_price = float(df["Low"].iloc[-1])

    ha_live_close = (open_price + high_price + low_price + live_spot) / 4.0
    return live_spot, ha_live_close


def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Compute HA columns over a continuous multi-day window, seeded from the first row.

    Slice to a single day only AFTER this — never before. HA_Open is recursive.
    """
    ha = df.copy()
    ha["HA_Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4.0

    ha_open = [(df["Open"].iloc[0] + df["Close"].iloc[0]) / 2.0]
    for i in range(1, len(df)):
        ha_open.append((ha_open[-1] + ha["HA_Close"].iloc[i - 1]) / 2.0)
    ha["HA_Open"] = ha_open

    ha["HA_High"] = ha[["High", "HA_Open", "HA_Close"]].max(axis=1)
    ha["HA_Low"] = ha[["Low", "HA_Open", "HA_Close"]].min(axis=1)
    ha["Is_Red"] = ha["HA_Close"] < ha["HA_Open"]
    ha["Is_Green"] = ha["HA_Close"] > ha["HA_Open"]
    return ha


def calculate_30m_heikin_ashi() -> pd.DataFrame:
    df = PROVIDER.intraday_bars(SYMBOL, INTRADAY_INTERVAL_MIN, INTRADAY_LOOKBACK_DAYS)
    if len(df) < 2:
        raise ValueError("Need at least two 30m candles to evaluate exits.")

    age_min = (_now() - _last_timestamp_ist(df)).total_seconds() / 60.0
    if age_min > MAX_INTRADAY_STALENESS_MIN:
        raise StaleDataError(f"Latest 30m candle is {age_min:.0f} min old — feed lagging.")

    return _heikin_ashi(df)


def calculate_30m_heikin_ashi_for_day(day: dt.date) -> pd.DataFrame:
    return calculate_30m_heikin_ashi_day_and_prev(day)[0]


def calculate_30m_heikin_ashi_day_and_prev(day: dt.date) -> tuple[pd.DataFrame, "pd.Series | None"]:
    """`day`'s HA rows plus the HA row immediately BEFORE them."""
    df = PROVIDER.intraday_bars(SYMBOL, INTRADAY_INTERVAL_MIN, INTRADAY_LOOKBACK_DAYS)
    if len(df) < 2:
        raise ValueError("Need at least two 30m candles to evaluate exits.")

    full = _heikin_ashi(df)
    day_df = full[full.index.date == day]
    if day_df.empty:
        raise StaleDataError(f"No 30m candle data for {day} in the fetched window yet.")

    age_min = (_now() - _last_timestamp_ist(day_df)).total_seconds() / 60.0
    if age_min > MAX_INTRADAY_STALENESS_MIN:
        raise StaleDataError(f"Latest 30m candle for {day} is {age_min:.0f} min old — feed lagging.")

    first_pos = full.index.get_loc(day_df.index[0])
    if hasattr(first_pos, "start"):
        first_pos = first_pos.start
    prev_row = full.iloc[first_pos - 1] if first_pos > 0 else None
    return day_df, prev_row


# ---------------------------- messages ----------------------------


def leftover_message(position: dict, now_ist: str) -> str:
    return f"""⏰ LEFTOVER POSITION — SQUARE OFF NOW
Time: {now_ist}
Open position: {position.get('side')} opened {position.get('opened_at', 'unknown')}
Contract: {position.get('tradingsymbol', position.get('side'))}

This trade already had its one exit session and is still open.
The playbook is one night — never two.

⚡ ACTION REQUIRED:
Square off ALL remaining lots immediately.
Do not carry this position any further."""


def cutoff_message(position: dict) -> str:
    return f"""⏰ TIME CUTOFF REACHED ({HARD_EXIT_TIME.strftime('%I:%M %p')} IST)
Asset: NIFTY 50
Open position: {position['side']} opened {position.get('opened_at', 'unknown')}

⚡ ACTION REQUIRED:
Square off ALL remaining open lots immediately!
Do not carry this position into a second night."""


def _signal_message(side: str, now_ist: str, spot: float, ha: float, div: float,
                     expiry_date: dt.date, expiry_label: str, expiry_rolled: bool,
                     contract: dict | None = None) -> str:
    is_call = side == "CE"
    expiry_note = (
        "\n  (this week's expiry was too close to buy today — rolled forward\n"
        "  per the no-buying-inside-expiry-week-Mon/Tue rule)"
        if expiry_rolled else ""
    )
    holiday_note = ""
    if contract and contract.get("expiry_date") and contract["expiry_date"] != expiry_date.isoformat():
        actual = dt.date.fromisoformat(contract["expiry_date"])
        holiday_note = (
            f"\n  (listed Tuesday {expiry_date.isoformat()} had no chain — "
            f"using {actual.strftime('%d %b %Y')} holiday walk-back)"
        )
        expiry_date = actual
    if contract:
        contract_line = f"Contract: {contract['tradingsymbol']}"
        premium_line = (
            f"Quoted premium (live): {contract['premium']:.2f}\n"
            f"Partial profit target: {contract['premium'] * PARTIAL_PROFIT_MULTIPLIER:.2f} "
            f"({PARTIAL_PROFIT_MULTIPLIER:.0f}x quoted — books "
            f"{PARTIAL_PROFIT_FRACTION * 100:.0f}% of the position)\n"
            f"If your fill differs, on the VM run:\n"
            f"  python btst_engine.py fill <your_fill_price>"
        )
    else:
        contract_line = f"Contract: Nifty {'Call' if is_call else 'Put'} Option ({side})"
        premium_line = f"Target Premium: {TARGET_PREMIUM}"
    return f"""🚨 BTST SIGNAL DETECTED 🚨
Asset: NIFTY 50 (Spot)
Time: {now_ist}
Timeframe: Daily (1D) Live

📊 MARKET DATA
• Daily Spot Price: {spot:.2f}
• Live Daily Heikin-Ashi: {ha:.2f}
• Momentum Divergence: {div:+.2f} pts
• System Threshold: {'+' if is_call else '-'}{DIVERGENCE_THRESHOLD:.1f} pts

{'📈 DIRECTION: BUY CALL (CE)' if is_call else '📉 DIRECTION: BUY PUT (PE)'}

⚡ ACTIONABLE STEPS
{contract_line}
Expiry: {expiry_date.strftime('%d %b %Y (%A)')} — {expiry_label}{expiry_note}{holiday_note}
{premium_line}
Order Type: NRML / CNC (Do not use MIS)
Window: Execute between {ENTRY_WINDOW}

🤖 Exit engine active. Hold overnight — do not square off today."""


def _no_trade_message(now_ist: str, spot: float, ha: float, div: float) -> str:
    return f"""🛡️ BTST SCAN COMPLETE — NO TRADE
Asset: NIFTY 50 (Spot)
Time: {now_ist}

📊 MARKET DATA
• Daily Spot Price: {spot:.2f}
• Live Daily Heikin-Ashi: {ha:.2f}
• Momentum Divergence: {div:+.2f} pts
• Neutral Zone: -{DIVERGENCE_THRESHOLD:.1f} to +{DIVERGENCE_THRESHOLD:.1f} pts

🔒 RESULT: Standby (Capital Preserved)
Divergence is insufficient for high-conviction overnight momentum."""


def _out_of_window_message(now_ist: str, spot: float, ha: float, div: float,
                           late: bool) -> str:
    would_be = (
        "BUY CALL (CE)" if div >= DIVERGENCE_THRESHOLD
        else "BUY PUT (PE)" if div <= -DIVERGENCE_THRESHOLD
        else "NO TRADE"
    )
    header = (
        "⚠️ BTST ENTRY WINDOW MISSED" if late
        else "ℹ️ BTST PRE-WINDOW PREVIEW (not actionable)"
    )
    why = (
        "This scan ran after the entry window closed. The numbers below are informational ONLY."
        if late else
        "This scan ran before the entry window opened. Numbers are indicative;\n"
        "the binding scan happens inside the window."
    )
    return f"""{header}
Asset: NIFTY 50 (Spot)
Time: {now_ist}
Window: {ENTRY_WINDOW}

{why}

📊 MARKET DATA (as of this scan)
• Daily Spot Price: {spot:.2f}
• Live Daily Heikin-Ashi: {ha:.2f}
• Momentum Divergence: {div:+.2f} pts
• Would have read as: {would_be}

🚫 DO NOT place this trade on the strength of this message.
No position has been recorded by the exit engine."""


def _resolve_contract(side: str, expiry_date: dt.date) -> dict | None:
    if not hasattr(PROVIDER, "resolve_option_contract"):
        return None
    try:
        contract = PROVIDER.resolve_option_contract(side, expiry_date, TARGET_PREMIUM_VALUE)
        log.info("Resolved contract: %s @ %.2f (expiry %s)",
                 contract["tradingsymbol"], contract["premium"],
                 contract.get("expiry_date", expiry_date.isoformat()))
        return contract
    except Exception as e:
        log.warning("Option contract resolution failed (%s) — falling back to generic "
                    "signal message with no partial-profit tracking.", e)
        return None


# ----------------------------- scans -----------------------------


def run_entry_scan(state: dict, force: bool = False) -> None:
    """Entry scanner. Idempotent per trading day."""
    now = _now()
    today = now.date().isoformat()
    now_ist = _stamp(now)

    if not force and state.get("entry_scan_date") == today:
        log.info("Entry scan already completed for %s — skipping (redundant run).", today)
        return

    try:
        live_spot, ha_close = get_live_daily_data()
    except StaleDataError as e:
        log.warning("Entry scan skipped: %s", e)
        delivered = send_telegram(
            f"🏖️ BTST ENTRY SCAN SKIPPED\nTime: {now_ist}\n\n{e}\n\nNo trade today."
        )
        # Only burn the day once the window is gone. Inside 15:18–15:28 a lagging
        # feed must retry at the next cron tick, not look like a holiday forever.
        if now.time() > ENTRY_LATE_LIMIT and delivered:
            state["entry_scan_date"] = today
        return
    except Exception as e:
        log.exception("Entry scan failed")
        send_telegram(
            f"❌ BTST Scanner Error (entry)\nTime: {now_ist}\n{type(e).__name__}: {e}\n\n"
            f"Not marked complete — a later scheduled run will retry."
        )
        return

    divergence = live_spot - ha_close
    log.info("Entry scan | spot=%.2f ha=%.2f div=%+.2f", live_spot, ha_close, divergence)

    actionable = force or (ENTRY_ACTIONABLE_FROM <= now.time() <= ENTRY_LATE_LIMIT)
    if not actionable:
        late = now.time() > ENTRY_LATE_LIMIT
        log.warning("Entry scan outside window (late=%s) — sending informational notice.", late)
        delivered = send_telegram(
            _out_of_window_message(now_ist, live_spot, ha_close, divergence, late)
        )
        if late and delivered:
            state["entry_scan_date"] = today
        return

    if divergence >= DIVERGENCE_THRESHOLD:
        side = "CE"
    elif divergence <= -DIVERGENCE_THRESHOLD:
        side = "PE"
    else:
        side = None

    if side:
        expiry_date, expiry_label = _next_option_expiry(now.date())
        expiry_rolled = now.weekday() in (0, 1)
        contract = _resolve_contract(side, expiry_date)
        if contract and contract.get("expiry_date"):
            expiry_date = dt.date.fromisoformat(contract["expiry_date"])
        if not send_telegram(_signal_message(side, now_ist, live_spot, ha_close, divergence,
                                              expiry_date, expiry_label, expiry_rolled, contract)):
            log.error("BUY signal UNDELIVERED — no position recorded, will retry in-window.")
            return
        state["position"] = {
            "side": side,
            "entry_spot": round(live_spot, 2),
            "opened_at": now_ist,
            "opened_date": today,
            "expiry_date": expiry_date.isoformat(),
            "expiry_label": expiry_label,
        }
        if contract:
            state["position"].update({
                "tradingsymbol": contract["tradingsymbol"],
                "symbol_token": contract["symbol_token"],
                "entry_premium": contract["premium"],
                "partial_booked": False,
            })
        state["last_exit_signal_candle"] = None
        log.info("Position recorded: %s @ %.2f, expiry %s (%s), contract=%s",
                  side, live_spot, expiry_date.isoformat(), expiry_label,
                  contract["tradingsymbol"] if contract else "unresolved")
    else:
        if not send_telegram(_no_trade_message(now_ist, live_spot, ha_close, divergence)):
            log.error("NO-TRADE notice UNDELIVERED — will retry in-window.")
            return
        state["position"] = None

    state["entry_scan_date"] = today


def _try_clear_position(state: dict, message: str) -> bool:
    """Send an exit/cutoff/leftover alert. Clear position only if delivered."""
    if not send_telegram(message):
        log.error("Exit/cutoff alert UNDELIVERED — position kept, will retry.")
        return False
    state["position"] = None
    return True


def run_exit_scan(state: dict, force: bool = False) -> None:
    """30m HA exit + leftover + 15:13 cutoff. Cron path and watcher-down fallback."""
    now = _now()
    today = now.date()
    now_time = now.strftime("%H:%M IST")
    position = state.get("position")

    # Leftover can fire from the open; HA monitoring waits for 09:45.
    if not force:
        if position and (is_leftover_position(position, today) or (
                _opened_date(position) is not None
                and _opened_date(position) < today
                and now.time() >= HARD_EXIT_TIME)):
            pass  # handle below even outside the 09:45–15:16 HA window
        elif not (EXIT_MONITOR_FROM <= now.time() < EXIT_MONITOR_UNTIL):
            log.info("Outside 30m monitoring hours (%s) — nothing to do.", now_time)
            return

    if position and is_same_day_position(position, today) and not force:
        log.info("Position opened today — overnight hold, no same-day HA exit.")
        return

    if position and is_leftover_position(position, today) and not force:
        _try_clear_position(state, leftover_message(position, _stamp(now)))
        return

    # Stamp the exit session the moment we know today is it — BEFORE the feed
    # call and BEFORE the cutoff. If it were stamped only after a successful
    # candle fetch, a day of stale data (or an undelivered cutoff) would leave
    # exit_session_date empty, is_leftover_position() would say False tomorrow,
    # and the position would quietly get a second night.
    if position and not is_same_day_position(position, today):
        mark_exit_session(position, today)

    if now.time() >= HARD_EXIT_TIME and not force:
        if not position:
            log.info("Past hard exit time and no open position — nothing to do.")
            return
        if is_same_day_position(position, today):
            log.info("Past 15:13 but position is today's entry — hold overnight.")
            return
        _try_clear_position(state, cutoff_message(position))
        return

    try:
        ha_df = calculate_30m_heikin_ashi_for_day(today)
    except StaleDataError as e:
        log.warning("Exit scan skipped: %s", e)
        return
    except Exception as e:
        log.exception("Exit scan failed")
        send_telegram(
            f"⚠️ BTST exit monitor could not read 30m Nifty data\n"
            f"Time: {now_time}\n{type(e).__name__}: {e}"
        )
        return

    closed, forming = split_closed_and_forming(ha_df, now)
    latest = forming if forming is not None else (ha_df.iloc[-1] if not ha_df.empty else None)
    if latest is None:
        log.warning("No 30m bar to evaluate.")
        return

    candle_key = str(latest.name)
    ref_red, ref_green = sticky_refs(closed)

    if latest["Is_Red"]:
        candle_color = "🔴 RED"
    elif latest["Is_Green"]:
        candle_color = "🟢 GREEN"
    else:
        candle_color = "⚪ FLAT"

    already_signalled = state.get("last_exit_signal_candle") == candle_key
    if position and not already_signalled:
        side = position["side"]
        if side == "CE" and ref_red is not None and latest["HA_Low"] < ref_red["HA_Low"]:
            msg = f"""🛑 CALL (CE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: Latest red 30m HA Low (today) broken by current HA Low

📊 HEIKIN-ASHI DATA
• Latest red 30m HA Low ({ref_red.name.strftime('%H:%M')}): {ref_red['HA_Low']:.2f}
• Current 30m HA Low: {latest['HA_Low']:.2f} (Broken 👇)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Call (CE) lots!"""
            if send_telegram(msg):
                state["last_exit_signal_candle"] = candle_key
                state["position"] = None
                position = None
            else:
                log.error("CE EXIT UNDELIVERED — position kept.")
                return

        elif side == "PE" and ref_green is not None and latest["HA_High"] > ref_green["HA_High"]:
            msg = f"""🛑 PUT (PE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: Latest green 30m HA High (today) broken by current HA High

📊 HEIKIN-ASHI DATA
• Latest green 30m HA High ({ref_green.name.strftime('%H:%M')}): {ref_green['HA_High']:.2f}
• Current 30m HA High: {latest['HA_High']:.2f} (Broken 👆)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Put (PE) lots!"""
            if send_telegram(msg):
                state["last_exit_signal_candle"] = candle_key
                state["position"] = None
                position = None
            else:
                log.error("PE EXIT UNDELIVERED — position kept.")
                return

    if not force and state.get("last_status_candle") == candle_key:
        log.info("Status for candle %s already sent — skipping duplicate.", candle_key)
        return
    if not position and not STATUS_WHEN_FLAT:
        log.info("Flat and STATUS_WHEN_FLAT disabled — skipping status update.")
        state["last_status_candle"] = candle_key
        return

    ref_lines = []
    if ref_red is not None:
        ref_lines.append(
            f"• ARMED (CE exit): latest red HA Low ({ref_red.name.strftime('%H:%M')}) "
            f"{ref_red['HA_Low']:.2f}"
        )
    if ref_green is not None:
        ref_lines.append(
            f"• ARMED (PE exit): latest green HA High ({ref_green.name.strftime('%H:%M')}) "
            f"{ref_green['HA_High']:.2f}"
        )
    if not ref_lines:
        ref_lines.append("• No exit level armed yet today (no red or green candle has closed yet)")
    ref_block = "\n".join(ref_lines)

    if position:
        pos_line = (
            f"📌 OPEN POSITION: {position['side']} @ spot "
            f"{position.get('entry_spot', 0):.2f} (opened {position.get('opened_date')})"
        )
    else:
        pos_line = "📌 OPEN POSITION: none — monitoring only"

    status = f"""⏱️ 30-MIN MARKET STATUS UPDATE
Time: {now_time}
Asset: NIFTY 50 (Spot)

{pos_line}

📊 LATEST 30M HEIKIN-ASHI DATA
• Standard Spot Close: {latest['Close']:.2f}
• HA Candle Color: {candle_color}
• Current HA Open: {latest['HA_Open']:.2f}
• Current HA Close: {latest['HA_Close']:.2f}
• Current HA High: {latest['HA_High']:.2f}
• Current HA Low: {latest['HA_Low']:.2f}

📉 REFERENCE EXIT LEVEL(S) — today only
{ref_block}

ℹ️ System Active."""
    if send_telegram(status):
        state["last_status_candle"] = candle_key
    else:
        log.error("Status update UNDELIVERED — will retry.")


def run_auto(state: dict) -> None:
    """Pick the right scan from the real IST clock. Cron covers exits if watcher is silent."""
    now = _now()
    t = now.time()

    if now.weekday() >= 5:
        log.info("Weekend (%s) — nothing to do.", now.strftime("%A"))
        return

    position = state.get("position")
    leftover = bool(position) and is_leftover_position(position, now.date())

    if leftover or (position and not is_same_day_position(position, now.date())
                    and t >= HARD_EXIT_TIME):
        log.info("Leftover / cutoff path via cron.")
        run_exit_scan(state)
        return

    if t < LEFTOVER_WATCH_FROM:
        log.info("Before market open (%s) — nothing to do.", t)
        return

    if t < EXIT_MONITOR_UNTIL:
        if watcher_heartbeat_fresh(state, now):
            log.info("Watcher heartbeat fresh — cron leaves exits to watcher.py.")
            if state.get("watcher_down_alert_date") == now.date().isoformat():
                if send_telegram(
                    f"✅ BTST WATCHER HEARTBEAT RESTORED\nTime: {_stamp(now)}\n\n"
                    "Tick-level exit monitoring is running again. Cron stepping aside."
                ):
                    state["watcher_down_alert_date"] = None
                else:
                    log.error("Watcher-restored notice UNDELIVERED — flag kept, will retry.")
            return
        # Watcher down: cron must cover HA exits or a live position is unwatched.
        if t >= EXIT_MONITOR_FROM or leftover:
            today = now.date().isoformat()
            if state.get("watcher_down_alert_date") != today:
                delivered = send_telegram(
                    f"""⚠️ WATCHER HEARTBEAT MISSING
Time: {_stamp(now)}

The tick-level watcher has not updated in over {WATCHER_STALE_SECONDS // 60} min.
Cron is covering 30-minute exits until it returns.

On the VM check:
  sudo systemctl status btst-watcher
  tail -50 ~/nifty-btst-bot/watcher.log"""
                )
                # Only burn the once-a-day flag if the warning actually landed.
                # Otherwise a Telegram blip silences the "watcher is down"
                # alert for the whole session — the exact failure mode that
                # made this bot go quiet for days.
                if delivered:
                    state["watcher_down_alert_date"] = today
                else:
                    log.error("Watcher-down alert UNDELIVERED — will retry next cron tick.")
            log.warning("Watcher heartbeat stale — cron running exit scan as fallback.")
            run_exit_scan(state)
        else:
            log.info("Watcher stale but first 30m candle has not closed yet.")
        return
    if t < ENTRY_ACTIONABLE_FROM:
        log.info("Between exit cutoff and entry window (%s) — nothing to do.", t)
        return
    if t <= AUTO_ENTRY_UNTIL:
        run_entry_scan(state)
        return
    log.info("Outside all scan windows (%s) — nothing to do.", t)


def run_fill(state: dict, price: float) -> None:
    """Record the actual fill so 2x partial-profit uses the playbook's real premium."""
    position = state.get("position")
    if not position:
        log.error("No open position to attach a fill price to.")
        send_telegram("⚠️ FILL ignored — no open position in state.")
        return
    if price <= 0:
        log.error("Fill price must be positive, got %s", price)
        return
    old = position.get("entry_premium")
    position["entry_premium"] = float(price)
    position["fill_set_at"] = _stamp()
    target = price * PARTIAL_PROFIT_MULTIPLIER
    send_telegram(
        f"""✏️ FILL PRICE UPDATED
Contract: {position.get('tradingsymbol', position.get('side'))}
Previous quoted premium: {old}
Your fill: {price:.2f}
New 2× partial target: {target:.2f}

Remaining 50% still exits on the 30m HA rule."""
    )


def run_selftest(state: dict) -> None:
    """Verify credentials, clock and data feed, and prove Telegram delivery."""
    now = _now()
    log.info("IST now: %s (weekday=%s)", _stamp(now), now.strftime("%A"))
    log.info("Data provider: %s", PROVIDER.name)
    log.info("TELEGRAM_BOT_TOKEN set: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID set: %s", bool(TELEGRAM_CHAT_ID))
    log.info("State file: %s (exists=%s)", STATE_PATH, STATE_PATH.exists())
    log.info("Current state: %s", json.dumps(state, sort_keys=True))

    data_line = "not attempted"
    try:
        spot, ha = get_live_daily_data()
        data_line = f"OK — spot {spot:.2f}, daily HA close {ha:.2f}, div {spot - ha:+.2f}"
    except Exception as e:
        data_line = f"FAILED — {type(e).__name__}: {e}"
    log.info("Daily feed: %s", data_line)

    contract_line = "n/a (provider has no option-chain support)"
    if hasattr(PROVIDER, "resolve_option_contract"):
        try:
            expiry_date, expiry_label = _next_option_expiry(now.date())
            test_contract = PROVIDER.resolve_option_contract("CE", expiry_date, TARGET_PREMIUM_VALUE)
            index_ltp = PROVIDER.get_index_ltp()
            option_ltp = PROVIDER.get_option_ltp(test_contract["symbol_token"])
            contract_line = (
                f"OK — resolved {test_contract['tradingsymbol']} @ "
                f"{test_contract['premium']:.2f} ({expiry_label}); "
                f"live index LTP {index_ltp:.2f}, live option LTP {option_ltp:.2f}"
            )
        except Exception as e:
            contract_line = f"FAILED — {type(e).__name__}: {e}"
    log.info("Contract resolution: %s", contract_line)

    hb = state.get("watcher_heartbeat") or "never"
    send_telegram(f"""✅ BTST SELFTEST
Time: {_stamp(now)}

• Data provider: {PROVIDER.name}
• Telegram delivery: working (you are reading this)
• Daily feed: {data_line}
• Contract resolution (CE, next valid expiry): {contract_line}
• Open position: {state.get('position') or 'none'}
• Last entry scan: {state.get('entry_scan_date') or 'never'}
• Watcher heartbeat: {hb}
• Watcher heartbeat fresh: {watcher_heartbeat_fresh(state, now)}

If this arrived, credentials and chat ID are correct.""")


def main() -> int:
    parser = argparse.ArgumentParser(description="NIFTY 50 BTST scanner")
    parser.add_argument(
        "mode",
        nargs="?",
        default="auto",
        choices=["auto", "entry", "exit", "all", "selftest", "fill"],
    )
    parser.add_argument(
        "fill_price",
        nargs="?",
        default=None,
        help="actual fill premium, used with mode=fill",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore clock gating and per-day/per-candle deduplication",
    )
    args = parser.parse_args()

    failed = False
    with locked_state() as state:
        try:
            if args.mode == "auto":
                run_auto(state)
            elif args.mode == "selftest":
                run_selftest(state)
            elif args.mode == "fill":
                if args.fill_price is None:
                    log.error("Usage: python btst_engine.py fill <price>")
                    failed = True
                else:
                    run_fill(state, float(args.fill_price))
            else:
                if args.mode in ("entry", "all"):
                    run_entry_scan(state, force=args.force)
                if args.mode in ("exit", "all"):
                    run_exit_scan(state, force=args.force)
        except Exception:
            log.exception("Unhandled error in mode=%s", args.mode)
            failed = True

    if _send_failures:
        log.error("%d Telegram message(s) failed to send.", _send_failures)
    return 1 if (failed or _send_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
