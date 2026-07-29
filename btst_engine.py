"""NIFTY 50 BTST scanner — 3:20 PM entry scan + 30m Heikin-Ashi exit monitor.

Strategy logic is IDENTICAL to the original script. Every strategy parameter
lives in the STRATEGY block below; nothing else in this file changes them.

Everything outside that block is scheduling, state and delivery plumbing,
which exists because GitHub Actions' cron is best-effort: runs are routinely
delayed by 30-120 minutes and are sometimes dropped entirely. The engine
therefore decides what to do from the *actual* IST clock rather than trusting
the cron that woke it, keeps state between runs so redundant wake-ups are
harmless, and refuses to emit a tradeable signal outside the entry window.

Usage:
    python btst_engine.py auto        # decide from the IST clock (default)
    python btst_engine.py entry
    python btst_engine.py exit
    python btst_engine.py selftest    # verify Telegram + data plumbing
    python btst_engine.py entry --force   # ignore all clock gating

Exit code is non-zero if a scan aborted or a Telegram message failed to send,
so a silent bot shows up as a red run instead of a green one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time

import pandas as pd
import pytz
import requests
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")

# ----------------------- STRATEGY (do not change) -----------------------
SYMBOL = "^NSEI"
DIVERGENCE_THRESHOLD = 11.0          # points, applied symmetrically
HARD_EXIT_TIME = dt.time(15, 13)     # square-off cutoff
ENTRY_WINDOW = "3:21 PM - 3:28 PM IST"
TARGET_PREMIUM = "~Rs.100"
# ------------------------------------------------------------------------

# --- scheduling gates (operational, not strategy) ---
ENTRY_ACTIONABLE_FROM = dt.time(15, 18)   # earliest a signal may be acted on
ENTRY_LATE_LIMIT = dt.time(15, 28)        # after this the window has closed
AUTO_ENTRY_UNTIL = dt.time(20, 0)         # still report a *missed* window until here
EXIT_MONITOR_FROM = dt.time(9, 45)        # first 30m candle close
EXIT_MONITOR_UNTIL = dt.time(15, 16)      # just past HARD_EXIT_TIME

DAILY_PERIOD = "10d"
INTRADAY_PERIOD = "5d"
INTRADAY_INTERVAL = "30m"
MAX_INTRADAY_STALENESS_MIN = 90      # data-freshness guard, not a strategy rule
DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SEC = 2
REQUEST_TIMEOUT = 10
TELEGRAM_MAX_CHARS = 4096

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_PATH = pathlib.Path(os.getenv("BTST_STATE_FILE", "state.json"))
# Send the routine 30m status update even when no position is open.
STATUS_WHEN_FLAT = os.getenv("BTST_STATUS_WHEN_FLAT", "1") not in ("0", "false", "False")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("btst")

_send_failures = 0


class StaleDataError(RuntimeError):
    """Raised when the feed returns data too old to act on."""


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d %H:%M:%S IST")


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
            "Console output:\n%s",
            message,
        )
        _send_failures += 1
        return False

    if len(message) > TELEGRAM_MAX_CHARS:
        message = message[: TELEGRAM_MAX_CHARS - 3] + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
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
        detail = ""
        resp_obj = getattr(e, "response", None)
        if resp_obj is not None:
            detail = f" | response: {resp_obj.text[:300]}"
        log.error("Telegram send FAILED: %s%s\nMessage was:\n%s", e, detail, message)
        _send_failures += 1
        return False


# ------------------------------ state ------------------------------

_DEFAULT_STATE: dict = {
    "entry_scan_date": None,        # ISO date the entry scan completed
    "position": None,               # {"side","entry_spot","opened_at","opened_date"}
    "last_status_candle": None,     # candle key of the last routine status sent
    "last_exit_signal_candle": None,
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
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        log.info("State saved to %s: position=%s entry_scan_date=%s",
                 STATE_PATH, state.get("position"), state.get("entry_scan_date"))
    except OSError as e:
        log.error("Could not persist state to %s: %s", STATE_PATH, e)


# ------------------------------ data ------------------------------


def _download(period: str, interval: str) -> pd.DataFrame:
    """Download OHLC with retries; flattens MultiIndex columns."""
    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            df = yf.download(
                SYMBOL,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
            )
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
            last_err = ValueError("Yahoo Finance returned an empty frame")
        except Exception as e:  # network/parse errors from yfinance
            last_err = e
        log.warning("Download attempt %d/%d failed: %s", attempt, DOWNLOAD_RETRIES, last_err)
        if attempt < DOWNLOAD_RETRIES:
            time.sleep(RETRY_BACKOFF_SEC * attempt)
    raise RuntimeError(f"Failed to fetch {SYMBOL} {interval} data: {last_err}")


def _last_timestamp_ist(df: pd.DataFrame) -> pd.Timestamp:
    ts = pd.Timestamp(df.index[-1])
    return ts.tz_convert(IST) if ts.tzinfo is not None else ts.tz_localize(IST)


def get_live_daily_data() -> tuple[float, float]:
    """Return (live spot, forming daily Heikin-Ashi close)."""
    df = _download(DAILY_PERIOD, "1d")

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


def calculate_30m_heikin_ashi() -> pd.DataFrame:
    """Build 30m Heikin-Ashi candles. Raises on empty/stale/insufficient data."""
    df = _download(INTRADAY_PERIOD, INTRADAY_INTERVAL)
    if len(df) < 2:
        raise ValueError("Need at least two 30m candles to evaluate exits.")

    age_min = (_now() - _last_timestamp_ist(df)).total_seconds() / 60.0
    if age_min > MAX_INTRADAY_STALENESS_MIN:
        raise StaleDataError(f"Latest 30m candle is {age_min:.0f} min old — feed lagging.")

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


# ---------------------------- messages ----------------------------


def _signal_message(side: str, now_ist: str, spot: float, ha: float, div: float) -> str:
    is_call = side == "CE"
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
Contract: Nifty {'Call' if is_call else 'Put'} Option ({side})
Target Premium: {TARGET_PREMIUM}
Order Type: NRML / CNC (Do not use MIS)
Window: Execute between {ENTRY_WINDOW}

🤖 Exit engine active."""


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
        "This scan ran after the entry window closed — most likely GitHub Actions\n"
        "delayed the scheduled run. The numbers below are informational ONLY."
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
        send_telegram(
            f"🏖️ BTST ENTRY SCAN SKIPPED\nTime: {now_ist}\n\n{e}\n\nNo trade today."
        )
        state["entry_scan_date"] = today
        return
    except Exception as e:
        log.exception("Entry scan failed")
        send_telegram(
            f"❌ BTST Scanner Error (entry)\nTime: {now_ist}\n{type(e).__name__}: {e}\n\n"
            f"Not marked complete — a later scheduled run will retry."
        )
        return  # deliberately NOT marked done, so a later cron retries

    divergence = live_spot - ha_close
    log.info("Entry scan | spot=%.2f ha=%.2f div=%+.2f", live_spot, ha_close, divergence)

    actionable = force or (ENTRY_ACTIONABLE_FROM <= now.time() <= ENTRY_LATE_LIMIT)
    if not actionable:
        late = now.time() > ENTRY_LATE_LIMIT
        log.warning("Entry scan outside window (late=%s) — sending informational notice.", late)
        send_telegram(_out_of_window_message(now_ist, live_spot, ha_close, divergence, late))
        if late:
            state["entry_scan_date"] = today  # window is gone; don't retry today
        return

    if divergence >= DIVERGENCE_THRESHOLD:
        side = "CE"
    elif divergence <= -DIVERGENCE_THRESHOLD:
        side = "PE"
    else:
        side = None

    if side:
        send_telegram(_signal_message(side, now_ist, live_spot, ha_close, divergence))
        state["position"] = {
            "side": side,
            "entry_spot": round(live_spot, 2),
            "opened_at": now_ist,
            "opened_date": today,
        }
        state["last_exit_signal_candle"] = None
        log.info("Position recorded: %s @ %.2f", side, live_spot)
    else:
        send_telegram(_no_trade_message(now_ist, live_spot, ha_close, divergence))
        state["position"] = None

    state["entry_scan_date"] = today


def run_exit_scan(state: dict, force: bool = False) -> None:
    """Monitor 30m Heikin-Ashi breakouts and send a status update."""
    now = _now()
    now_time = now.strftime("%H:%M IST")
    position = state.get("position")

    if not force and not (EXIT_MONITOR_FROM <= now.time() < EXIT_MONITOR_UNTIL):
        log.info("Outside 30m monitoring hours (%s) — nothing to do.", now_time)
        return

    # 1. Time cutoff check — evaluated before any network call.
    #    Only meaningful when a position is actually open, otherwise this fired
    #    a "square off everything" alarm every single weekday.
    if now.time() >= HARD_EXIT_TIME and not force:
        if not position:
            log.info("Past hard exit time and no open position — nothing to do.")
            return
        send_telegram(f"""⏰ TIME CUTOFF REACHED ({HARD_EXIT_TIME.strftime('%I:%M %p')} IST)
Asset: NIFTY 50
Open position: {position['side']} opened {position.get('opened_at', 'unknown')}

⚡ ACTION REQUIRED:
Square off ALL remaining open lots immediately!
Do not carry this position into a second night.""")
        state["position"] = None
        return

    try:
        ha_df = calculate_30m_heikin_ashi()
    except StaleDataError as e:
        # Holiday or a lagging feed: log it, don't page the user every 15 min.
        log.warning("Exit scan skipped: %s", e)
        return
    except Exception as e:
        log.exception("Exit scan failed")
        send_telegram(
            f"⚠️ BTST exit monitor could not read 30m Nifty data\n"
            f"Time: {now_time}\n{type(e).__name__}: {e}"
        )
        return

    latest = ha_df.iloc[-1]
    prev = ha_df.iloc[-2]
    candle_key = str(ha_df.index[-1])

    if latest["Is_Red"]:
        candle_color = "🔴 RED"
    elif latest["Is_Green"]:
        candle_color = "🟢 GREEN"
    else:
        candle_color = "⚪ FLAT"

    # 2. Heikin-Ashi breakout exit signals — only for the side actually held,
    #    and only once per candle.
    already_signalled = state.get("last_exit_signal_candle") == candle_key
    if position and not already_signalled:
        side = position["side"]
        if side == "CE" and prev["Is_Red"] and latest["HA_Low"] < prev["HA_Low"]:
            send_telegram(f"""🛑 CALL (CE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: 30m HA Candle Low broken by Current HA Low

📊 HEIKIN-ASHI DATA
• Reference 30m Red HA Low: {prev['HA_Low']:.2f}
• Current 30m HA Low: {latest['HA_Low']:.2f} (Broken 👇)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Call (CE) lots!""")
            state["last_exit_signal_candle"] = candle_key
            state["position"] = None
            position = None

        elif side == "PE" and prev["Is_Green"] and latest["HA_High"] > prev["HA_High"]:
            send_telegram(f"""🛑 PUT (PE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: 30m HA Candle High broken by Current HA High

📊 HEIKIN-ASHI DATA
• Reference 30m Green HA High: {prev['HA_High']:.2f}
• Current 30m HA High: {latest['HA_High']:.2f} (Broken 👆)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Put (PE) lots!""")
            state["last_exit_signal_candle"] = candle_key
            state["position"] = None
            position = None

    # 3. Routine status update — once per completed 30m candle, so the 15-minute
    #    cron redundancy does not double-post.
    if not force and state.get("last_status_candle") == candle_key:
        log.info("Status for candle %s already sent — skipping duplicate.", candle_key)
        return
    if not position and not STATUS_WHEN_FLAT:
        log.info("Flat and STATUS_WHEN_FLAT disabled — skipping status update.")
        state["last_status_candle"] = candle_key
        return

    # Only one exit level is ever armed: the one matching the previous candle's
    # colour. Printing both made a green candle's low look like a red reference.
    if prev["Is_Red"]:
        ref_block = (
            f"• ARMED (CE exit): prev 🔴 RED HA Low {prev['HA_Low']:.2f}\n"
            f"  → exit Calls if current HA Low breaks below it"
        )
    elif prev["Is_Green"]:
        ref_block = (
            f"• ARMED (PE exit): prev 🟢 GREEN HA High {prev['HA_High']:.2f}\n"
            f"  → exit Puts if current HA High breaks above it"
        )
    else:
        ref_block = "• No exit level armed (previous HA candle is flat)"

    if position:
        pos_line = (
            f"📌 OPEN POSITION: {position['side']} @ spot "
            f"{position.get('entry_spot', 0):.2f} (opened {position.get('opened_date')})"
        )
    else:
        pos_line = "📌 OPEN POSITION: none — monitoring only"

    send_telegram(f"""⏱️ 30-MIN MARKET STATUS UPDATE
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

📉 REFERENCE EXIT LEVEL
{ref_block}

ℹ️ System Active.""")
    state["last_status_candle"] = candle_key


def run_auto(state: dict) -> None:
    """Pick the right scan from the real IST clock, not from the cron that fired."""
    now = _now()
    t = now.time()

    if now.weekday() >= 5:
        log.info("Weekend (%s) — nothing to do.", now.strftime("%A"))
        return
    if t < EXIT_MONITOR_FROM:
        log.info("Before first 30m candle close (%s) — nothing to do.", t)
        return
    if t < EXIT_MONITOR_UNTIL:
        run_exit_scan(state)
        return
    if t < ENTRY_ACTIONABLE_FROM:
        log.info("Between exit cutoff and entry window (%s) — nothing to do.", t)
        return
    if t <= AUTO_ENTRY_UNTIL:
        run_entry_scan(state)
        return
    log.info("Outside all scan windows (%s) — nothing to do.", t)


def run_selftest(state: dict) -> None:
    """Verify credentials, clock and data feed, and prove Telegram delivery."""
    now = _now()
    log.info("IST now: %s (weekday=%s)", _stamp(now), now.strftime("%A"))
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

    send_telegram(f"""✅ BTST SELFTEST
Time: {_stamp(now)}

• Telegram delivery: working (you are reading this)
• Daily feed: {data_line}
• Open position: {state.get('position') or 'none'}
• Last entry scan: {state.get('entry_scan_date') or 'never'}

If this arrived, credentials and chat ID are correct.""")


def main() -> int:
    parser = argparse.ArgumentParser(description="NIFTY 50 BTST scanner")
    parser.add_argument(
        "mode",
        nargs="?",
        default="auto",
        choices=["auto", "entry", "exit", "all", "selftest"],
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore clock gating and per-day/per-candle deduplication",
    )
    args = parser.parse_args()

    state = load_state()
    before = json.dumps(state, sort_keys=True)
    failed = False

    try:
        if args.mode == "auto":
            run_auto(state)
        elif args.mode == "selftest":
            run_selftest(state)
        else:
            if args.mode in ("entry", "all"):
                run_entry_scan(state, force=args.force)
            if args.mode in ("exit", "all"):
                run_exit_scan(state, force=args.force)
    except Exception:
        log.exception("Unhandled error in mode=%s", args.mode)
        failed = True

    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
    else:
        log.info("State unchanged.")

    if _send_failures:
        log.error("%d Telegram message(s) failed to send.", _send_failures)
    return 1 if (failed or _send_failures) else 0


if __name__ == "__main__":
    sys.exit(main())
