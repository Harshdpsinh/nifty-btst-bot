"""NIFTY 50 BTST scanner — 3:20 PM entry scan + 30m Heikin-Ashi exit monitor.

Strategy logic is IDENTICAL to the original script. Every strategy parameter
lives in the STRATEGY block below; nothing else in this file changes them.

Usage:
    python nifty_btst_bot.py entry
    python nifty_btst_bot.py exit
    python nifty_btst_bot.py all
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("btst")


class StaleDataError(RuntimeError):
    """Raised when the feed returns data too old to act on."""


# --------------------------- notifications ---------------------------


def send_telegram(message: str) -> None:
    """Send an HTML-formatted notification. Never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing. Console output:\n%s", message)
        return

    if len(message) > TELEGRAM_MAX_CHARS:
        message = message[: TELEGRAM_MAX_CHARS - 3] + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Telegram send failed: %s\nMessage was:\n%s", e, message)


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
    today = dt.datetime.now(IST).date()
    if last_date != today:
        raise StaleDataError(
            f"Latest daily candle is {last_date}, not {today} — market closed or feed lagging."
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

    age_min = (dt.datetime.now(IST) - _last_timestamp_ist(df)).total_seconds() / 60.0
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


# ----------------------------- scans -----------------------------


def run_320_entry_scan() -> None:
    """Entry scanner (3:20 PM IST or on demand)."""
    now_ist = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    try:
        live_spot, ha_close = get_live_daily_data()
    except Exception as e:
        log.exception("Entry scan failed")
        send_telegram(f"❌ BTST Scanner Error (entry): {e}")
        return

    divergence = live_spot - ha_close
    log.info("Entry scan | spot=%.2f ha=%.2f div=%+.2f", live_spot, ha_close, divergence)

    if divergence >= DIVERGENCE_THRESHOLD:
        send_telegram(_signal_message("CE", now_ist, live_spot, ha_close, divergence))
    elif divergence <= -DIVERGENCE_THRESHOLD:
        send_telegram(_signal_message("PE", now_ist, live_spot, ha_close, divergence))
    else:
        send_telegram(_no_trade_message(now_ist, live_spot, ha_close, divergence))


def run_30m_exit_scan() -> None:
    """Monitor 30m Heikin-Ashi breakouts and send a status update."""
    now = dt.datetime.now(IST)
    now_time = now.strftime("%H:%M IST")

    # 1. Time cutoff check — evaluated before any network call.
    if now.time() >= HARD_EXIT_TIME:
        send_telegram(f"""⏰ TIME CUTOFF REACHED ({HARD_EXIT_TIME.strftime('%I:%M %p')} IST)
Asset: NIFTY 50

⚡ ACTION REQUIRED:
Square off ALL remaining open lots immediately!
Do not carry this position into a second night.""")
        return

    try:
        ha_df = calculate_30m_heikin_ashi()
    except Exception as e:
        log.exception("Exit scan failed")
        send_telegram(f"⚠️ BTST exit monitor could not read 30m Nifty data: {e}")
        return

    latest = ha_df.iloc[-1]
    prev = ha_df.iloc[-2]

    if latest["Is_Red"]:
        candle_color = "🔴 RED"
    elif latest["Is_Green"]:
        candle_color = "🟢 GREEN"
    else:
        candle_color = "⚪ FLAT"

    # 2. Heikin-Ashi breakout exit signals.
    if prev["Is_Red"] and latest["HA_Low"] < prev["HA_Low"]:
        send_telegram(f"""🛑 CALL (CE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: 30m HA Candle Low broken by Current HA Low

📊 HEIKIN-ASHI DATA
• Reference 30m Red HA Low: {prev['HA_Low']:.2f}
• Current 30m HA Low: {latest['HA_Low']:.2f} (Broken 👇)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Call (CE) lots!""")

    elif prev["Is_Green"] and latest["HA_High"] > prev["HA_High"]:
        send_telegram(f"""🛑 PUT (PE) EXIT SIGNAL TRIGGERED
Time: {now_time}
Reason: 30m HA Candle High broken by Current HA High

📊 HEIKIN-ASHI DATA
• Reference 30m Green HA High: {prev['HA_High']:.2f}
• Current 30m HA High: {latest['HA_High']:.2f} (Broken 👆)
• Spot Close: {latest['Close']:.2f}

⚡ ACTION REQUIRED: Exit ALL open Put (PE) lots!""")

    # 3. Routine status update.
    send_telegram(f"""⏱️ 30-MIN MARKET STATUS UPDATE
Time: {now_time}
Asset: NIFTY 50 (Spot)

📊 LATEST 30M HEIKIN-ASHI DATA
• Standard Spot Close: {latest['Close']:.2f}
• HA Candle Color: {candle_color}
• Current HA Open: {latest['HA_Open']:.2f}
• Current HA Close: {latest['HA_Close']:.2f}
• Current HA High: {latest['HA_High']:.2f}
• Current HA Low: {latest['HA_Low']:.2f}

📉 REFERENCE EXIT LEVELS
• Prev 30m Red HA Low: {prev['HA_Low']:.2f} (for CE Exit)
• Prev 30m Green HA High: {prev['HA_High']:.2f} (for PE Exit)

ℹ️ System Active.""")


def main() -> int:
    parser = argparse.ArgumentParser(description="NIFTY 50 BTST scanner")
    parser.add_argument("mode", nargs="?", default="entry", choices=["entry", "exit", "all"])
    args = parser.parse_args()

    if args.mode in ("entry", "all"):
        run_320_entry_scan()
    if args.mode in ("exit", "all"):
        run_30m_exit_scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
