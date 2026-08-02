"""NIFTY 50 BTST scanner — 3:20 PM entry scan + 30m Heikin-Ashi exit monitor.

Strategy logic is IDENTICAL to the original script. Every strategy parameter
lives in the STRATEGY block below; nothing else in this file changes them.

Everything outside that block is scheduling, state and delivery plumbing,
which exists because GitHub Actions' cron is best-effort: runs are routinely
delayed by 30-120 minutes and are sometimes dropped entirely. The engine
therefore decides what to do from the *actual* IST clock rather than trusting
the cron that woke it, keeps state between runs so redundant wake-ups are
harmless, and refuses to emit a tradeable signal outside the entry window.

Market data comes from a swappable provider (see providers.py) selected by
the DATA_PROVIDER env var — switching feeds is a config change, not a code
change. Everything below the STRATEGY block is provider-agnostic.

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

# Partial profit booking (Angel One only — needs a live per-strike premium,
# which Yahoo doesn't have): once the ACTUAL entry premium of the resolved
# contract doubles, book this fraction of the position; the remainder still
# exits purely on the existing 30m HA breakout rule, unchanged.
PARTIAL_PROFIT_MULTIPLIER = 2.0
PARTIAL_PROFIT_FRACTION = 0.5

# NSE NIFTY weekly options expiry. Monday=0 ... Sunday=6. Currently Tuesday;
# NSE has changed this weekday before (Thursday -> Monday -> Tuesday) — if it
# moves again, this one constant is the only thing that needs to change.
WEEKLY_EXPIRY_WEEKDAY = 1  # Tuesday

# Never buy a contract inside its own expiry week's Monday/Tuesday (1-2
# calendar days of runway left is too close: heavy theta decay, and a slow
# move has no room to play out overnight). Roll to the following week's
# expiry instead. Wed/Thu/Fri entries are far enough out (4-6 days) and use
# the nearest upcoming Tuesday as normal.
# ------------------------------------------------------------------------

# --- scheduling gates (operational, not strategy) ---
ENTRY_ACTIONABLE_FROM = dt.time(15, 18)   # earliest a signal may be acted on
ENTRY_LATE_LIMIT = dt.time(15, 28)        # after this the window has closed
AUTO_ENTRY_UNTIL = dt.time(20, 0)         # still report a *missed* window until here
EXIT_MONITOR_FROM = dt.time(9, 45)        # first 30m candle close
EXIT_MONITOR_UNTIL = dt.time(15, 16)      # just past HARD_EXIT_TIME

DAILY_LOOKBACK_DAYS = 10
INTRADAY_LOOKBACK_DAYS = 5
INTRADAY_INTERVAL_MIN = 30
MAX_INTRADAY_STALENESS_MIN = 90      # data-freshness guard, not a strategy rule
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

PROVIDER = get_provider()
log.info("Data provider: %s", PROVIDER.name)

_send_failures = 0


class StaleDataError(RuntimeError):
    """Raised when the feed returns data too old to act on."""


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d %H:%M:%S IST")


def _next_option_expiry(today: dt.date) -> tuple[dt.date, str]:
    """Pick which expiry to buy today, per the risk rule above: skip the
    current week's expiry entirely if today is its Monday or Tuesday (too
    close), rolling to the following week instead. Returns (date, label)
    where label is "MONTHLY" if that date happens to be the last Tuesday of
    its calendar month (NSE doesn't list a separate weekly contract that
    week — the weekly and monthly contract are the same instrument), else
    "WEEKLY".
    """
    days_ahead = (WEEKLY_EXPIRY_WEEKDAY - today.weekday()) % 7
    candidate = today + dt.timedelta(days=days_ahead)  # this week's expiry weekday
    if today.weekday() in (0, 1):  # Monday or Tuesday: this week's expiry is too close
        candidate += dt.timedelta(days=7)
    is_monthly = (candidate + dt.timedelta(days=7)).month != candidate.month
    return candidate, ("MONTHLY" if is_monthly else "WEEKLY")


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
    "position": None,               # {"side","entry_spot","opened_at","opened_date",
                                     #  "expiry_date","expiry_label", and (Angel One only)
                                     #  "tradingsymbol","symbol_token","entry_premium",
                                     #  "partial_booked"}
    "last_status_candle": None,     # candle key of the last routine status sent (cron path)
    "last_exit_signal_candle": None,
    "watcher_last_status_bucket": None,  # same idea, but for watcher.py's tick-level path
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
#
# All feed access goes through PROVIDER (see providers.py). Nothing below
# this point knows or cares whether bars came from Yahoo, a broker API, or
# anything else — that's the point of the abstraction.


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
    """Compute HA columns for a raw OHLC frame, seeded fresh from the
    frame's own first row. Shared by the multi-day series (diagnostics) and
    the day-filtered series (exit monitoring — see the docstring on
    calculate_30m_heikin_ashi_for_day for why exit monitoring needs its own
    independently-seeded series rather than a continuation from a prior day).
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
    """Build 30m Heikin-Ashi candles across the full multi-day lookback
    window. Used for diagnostics/tests; exit monitoring itself uses
    calculate_30m_heikin_ashi_for_day() instead. Raises on empty/stale/
    insufficient data.
    """
    df = PROVIDER.intraday_bars(SYMBOL, INTRADAY_INTERVAL_MIN, INTRADAY_LOOKBACK_DAYS)
    if len(df) < 2:
        raise ValueError("Need at least two 30m candles to evaluate exits.")

    age_min = (_now() - _last_timestamp_ist(df)).total_seconds() / 60.0
    if age_min > MAX_INTRADAY_STALENESS_MIN:
        raise StaleDataError(f"Latest 30m candle is {age_min:.0f} min old — feed lagging.")

    return _heikin_ashi(df)


def calculate_30m_heikin_ashi_for_day(day: dt.date) -> pd.DataFrame:
    """Build 30m Heikin-Ashi candles using ONLY `day`'s own bars — a fresh,
    independently-seeded series, never a continuation from any prior day
    (including the entry day). Exit monitoring must only ever consider the
    candles of the day actually being watched.
    """
    df = PROVIDER.intraday_bars(SYMBOL, INTRADAY_INTERVAL_MIN, INTRADAY_LOOKBACK_DAYS)
    day_df = df[df.index.date == day]
    if day_df.empty:
        raise StaleDataError(f"No 30m candle data for {day} in the fetched window yet.")

    age_min = (_now() - _last_timestamp_ist(day_df)).total_seconds() / 60.0
    if age_min > MAX_INTRADAY_STALENESS_MIN:
        raise StaleDataError(f"Latest 30m candle for {day} is {age_min:.0f} min old — feed lagging.")

    return _heikin_ashi(day_df)


# ---------------------------- messages ----------------------------


def _signal_message(side: str, now_ist: str, spot: float, ha: float, div: float,
                     expiry_date: dt.date, expiry_label: str, expiry_rolled: bool,
                     contract: dict | None = None) -> str:
    is_call = side == "CE"
    expiry_note = (
        "\n  (this week's expiry was too close to buy today — rolled forward\n"
        "  per the no-buying-inside-expiry-week-Mon/Tue rule)"
        if expiry_rolled else ""
    )
    if contract:
        contract_line = f"Contract: {contract['tradingsymbol']}"
        premium_line = (
            f"Entry Premium (live, Angel One): {contract['premium']:.2f}\n"
            f"Partial profit target: {contract['premium'] * PARTIAL_PROFIT_MULTIPLIER:.2f} "
            f"({PARTIAL_PROFIT_MULTIPLIER:.0f}x entry — books "
            f"{PARTIAL_PROFIT_FRACTION * 100:.0f}% of the position automatically)"
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
Expiry: {expiry_date.strftime('%d %b %Y (%A)')} — {expiry_label}{expiry_note}
{premium_line}
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


def _resolve_contract(side: str, expiry_date: dt.date) -> dict | None:
    """Resolve the actual strike nearest TARGET_PREMIUM_VALUE, if the current
    provider supports it (Angel One only — Yahoo has no options-chain data).
    Never raises: a resolution failure shouldn't abort a valid divergence
    signal, it just falls back to the generic "buy near ~Rs.100" message
    with no partial-profit tracking for that position.
    """
    if not hasattr(PROVIDER, "resolve_option_contract"):
        return None
    try:
        contract = PROVIDER.resolve_option_contract(side, expiry_date, TARGET_PREMIUM_VALUE)
        log.info("Resolved contract: %s @ %.2f", contract["tradingsymbol"], contract["premium"])
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
        expiry_date, expiry_label = _next_option_expiry(now.date())
        expiry_rolled = now.weekday() in (0, 1)
        contract = _resolve_contract(side, expiry_date)
        send_telegram(_signal_message(side, now_ist, live_spot, ha_close, divergence,
                                       expiry_date, expiry_label, expiry_rolled, contract))
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
        send_telegram(_no_trade_message(now_ist, live_spot, ha_close, divergence))
        state["position"] = None

    state["entry_scan_date"] = today


def run_exit_scan(state: dict, force: bool = False) -> None:
    """Monitor 30m Heikin-Ashi breakouts at cron cadence and send a status
    update. This is the fallback path for providers without live per-tick
    quotes (Yahoo) and for manual `python btst_engine.py exit` runs. On
    Angel One, watcher.py runs this same rule tick-by-tick and is what
    actually fires exits during market hours — run_auto() steps aside for
    it automatically so the two never race on the same position.

    Reference rule: only the EXIT day's own 30m candles ever count (never
    the entry day's or any earlier day's). Holding CE, the armed level is
    the most recently CLOSED red candle's HA Low, seen so far today — it
    updates forward every time a newer red candle closes, and a green
    candle closing in between does not erase it. Holding PE, same thing
    mirrored on the most recent green candle's HA High.
    """
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
        ha_df = calculate_30m_heikin_ashi_for_day(now.date())
    except StaleDataError as e:
        # Holiday, market not yet open enough for a candle, or a lagging
        # feed: log it, don't page the user every 15 min.
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
    candle_key = str(ha_df.index[-1])
    closed = ha_df.iloc[:-1]  # every candle except the possibly still-forming last one

    # Sticky references: the most recent CLOSED red/green candle of TODAY
    # only, each updating independently forward as newer ones close.
    ref_red = None
    ref_green = None
    for _, row in closed.iterrows():
        if row["Is_Red"]:
            ref_red = row
        elif row["Is_Green"]:
            ref_green = row

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
        if side == "CE" and ref_red is not None and latest["HA_Low"] < ref_red["HA_Low"]:
            send_telegram(f"""🛑 CALL (CE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: Latest red 30m HA Low (today) broken by current HA Low

📊 HEIKIN-ASHI DATA
• Latest red 30m HA Low ({ref_red.name.strftime('%H:%M')}): {ref_red['HA_Low']:.2f}
• Current 30m HA Low: {latest['HA_Low']:.2f} (Broken 👇)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Call (CE) lots!""")
            state["last_exit_signal_candle"] = candle_key
            state["position"] = None
            position = None

        elif side == "PE" and ref_green is not None and latest["HA_High"] > ref_green["HA_High"]:
            send_telegram(f"""🛑 PUT (PE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: Latest green 30m HA High (today) broken by current HA High

📊 HEIKIN-ASHI DATA
• Latest green 30m HA High ({ref_green.name.strftime('%H:%M')}): {ref_green['HA_High']:.2f}
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

📉 REFERENCE EXIT LEVEL(S) — today only
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
        if hasattr(PROVIDER, "get_index_ltp"):
            # watcher.py owns exit monitoring exclusively on this provider —
            # tick-level, not cron's 5-15 min cadence. Both writing to the
            # same position from two processes would race; only one may.
            log.info("Exit monitoring is owned by watcher.py on provider '%s' — "
                      "nothing for cron to do here.", PROVIDER.name)
            return
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

    # Contract resolution + live LTP are new, Angel-One-specific, and directly
    # affect real trades (partial-profit booking, tick-level exits) — worth
    # proving end to end before trusting them unattended.
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

    send_telegram(f"""✅ BTST SELFTEST
Time: {_stamp(now)}

• Data provider: {PROVIDER.name}
• Telegram delivery: working (you are reading this)
• Daily feed: {data_line}
• Contract resolution (CE, next valid expiry): {contract_line}
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
