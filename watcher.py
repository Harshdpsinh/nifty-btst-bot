"""Persistent tick-level exit monitor for Angel One positions.

Cron continues to own the daily entry decision (btst_engine.py auto/entry,
unchanged, on every provider). Once a position is open on DATA_PROVIDER=
angelone, THIS process exclusively owns exit monitoring for it: partial-
profit booking and the 30m Heikin-Ashi full-exit breakout, checked on a
tight polling loop (a few seconds) rather than cron's 5-minute cadence, so
a break is caught within seconds, not minutes. btst_engine.py's run_auto()
steps aside automatically whenever this provider is active (it checks for
PROVIDER.get_index_ltp), so the two processes never both write the same
position.

Exit rule (identical to btst_engine.run_exit_scan, just checked live instead
of once per cron invocation): the reference is the most recently CLOSED red
(holding CE) / green (holding PE) 30m candle of TODAY only, updating forward
each time a newer one of that colour closes. A candle of the other colour
closing in between does not erase it.

Partial profit: once the ACTUAL entry premium of the resolved option
contract doubles (PARTIAL_PROFIT_MULTIPLIER in btst_engine.py), book
PARTIAL_PROFIT_FRACTION of the position immediately and mark it booked.
The remainder continues to be watched by the exact same full-exit rule,
unchanged.

Requires DATA_PROVIDER=angelone (needs live per-tick LTP for both the NIFTY
index and the specific option contract held -- Yahoo has neither). On any
other provider this process just idles, logging why every so often, so it's
safe to enable the systemd service unconditionally regardless of which
provider happens to be configured.

Run continuously (see deploy/btst-watcher.service):
    python watcher.py

Not meant to be invoked ad hoc like btst_engine.py -- during market hours on
Angel One it never returns on its own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import os
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl; VM deploy is Linux-only
    fcntl = None

import btst_engine as engine

log = logging.getLogger("btst.watcher")

POLL_SECONDS = float(os.getenv("BTST_WATCHER_POLL_SECONDS", "10"))
IDLE_SLEEP_SECONDS = 1800          # outside market hours, weekends, unsupported provider
CAPABILITY_LOG_EVERY = 180         # how many idle iterations between "still idle" log lines
LOCK_PATH = engine.STATE_PATH.with_suffix(".lock")

# A few minutes past HARD_EXIT_TIME (15:13), so a watcher that's actively
# ticking is guaranteed to observe and fire the cutoff square-off itself,
# then go idle for the rest of the day rather than polling pointlessly
# through the evening.
WATCH_UNTIL = dt.time(15, 20)


def _capable() -> bool:
    return hasattr(engine.PROVIDER, "get_index_ltp") and hasattr(engine.PROVIDER, "get_option_ltp")


@contextlib.contextmanager
def _locked_state():
    """File-locked read-modify-write of the shared state file, so this
    long-running process and the cron-invoked entry script can never
    corrupt each other's writes even if they land in the same instant.
    Blocks (does not busy-wait) until the lock is free.
    """
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, "w") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            state = engine.load_state()
            before = engine.json.dumps(state, sort_keys=True)
            yield state
            if engine.json.dumps(state, sort_keys=True) != before:
                engine.save_state(state)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


@dataclasses.dataclass
class _CandleAccumulator:
    """Live-updating OHLC for the still-forming 30m bucket, ticked forward
    from index LTP polls instead of re-requesting candle history every few
    seconds. `open` is fixed once at bucket start; high/low/close update
    each tick. prev_ha_open/prev_ha_close come from the last authoritative
    CLOSED candle (from calculate_30m_heikin_ashi_for_day) and seed this
    bucket's HA_Open via the same recursive formula used everywhere else in
    this codebase.
    """
    bucket_start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    prev_ha_open: float
    prev_ha_close: float

    def tick(self, ltp: float) -> None:
        self.high = max(self.high, ltp)
        self.low = min(self.low, ltp)
        self.close = ltp

    def live_ha(self) -> tuple[float, float, float, float]:
        """(ha_open, ha_close, ha_high, ha_low) for this still-forming bucket."""
        ha_open = (self.prev_ha_open + self.prev_ha_close) / 2.0
        ha_close = (self.open + self.high + self.low + self.close) / 4.0
        ha_high = max(self.high, ha_open, ha_close)
        ha_low = min(self.low, ha_open, ha_close)
        return ha_open, ha_close, ha_high, ha_low


def _bucket_start(now: dt.datetime) -> dt.datetime:
    """Start of the current 30m bucket. NSE candles align to HH:15/HH:45
    (the session opens at 9:15), not HH:00/HH:30 — e.g. 10:07 -> 09:45,
    10:20 -> 10:15, 10:50 -> 10:45.
    """
    minute = now.minute
    if minute >= 45:
        return now.replace(minute=45, second=0, microsecond=0)
    if minute >= 15:
        return now.replace(minute=15, second=0, microsecond=0)
    # Between HH:00 and HH:15 -- still part of the previous hour's :45 bucket.
    return now.replace(minute=45, second=0, microsecond=0) - dt.timedelta(hours=1)


def bootstrap(today: dt.date) -> tuple[_CandleAccumulator, dict]:
    """(Re-)establish today's HA state from the authoritative candle
    history — the single source of truth is always
    calculate_30m_heikin_ashi_for_day(), never pure in-memory drift.
    Called at the start of each market-hours session and on every detected
    bucket rollover. Returns (accumulator for the still-forming bucket,
    {"red": most_recent_closed_red_row_or_None, "green": ...}).
    """
    ha_df = engine.calculate_30m_heikin_ashi_for_day(today)
    latest = ha_df.iloc[-1]
    closed = ha_df.iloc[:-1]

    refs: dict = {"red": None, "green": None}
    for _, row in closed.iterrows():
        if row["Is_Red"]:
            refs["red"] = row
        elif row["Is_Green"]:
            refs["green"] = row

    if len(closed) > 0:
        seed_open = float(closed.iloc[-1]["HA_Open"])
        seed_close = float(closed.iloc[-1]["HA_Close"])
    else:
        # First candle of the day: same seed rule _heikin_ashi() itself uses
        # for its own first row -- there is no "previous candle" yet.
        seed_open = (float(latest["Open"]) + float(latest["Close"])) / 2.0
        seed_close = seed_open

    acc = _CandleAccumulator(
        bucket_start=ha_df.index[-1].to_pydatetime(),
        open=float(latest["Open"]), high=float(latest["High"]),
        low=float(latest["Low"]), close=float(latest["Close"]),
        prev_ha_open=seed_open, prev_ha_close=seed_close,
    )
    return acc, refs


def _status_message(now_time: str, position: dict | None, ha_open: float, ha_close: float,
                     ha_high: float, ha_low: float, close: float, refs: dict) -> str:
    if ha_close < ha_open:
        candle_color = "🔴 RED"
    elif ha_close > ha_open:
        candle_color = "🟢 GREEN"
    else:
        candle_color = "⚪ FLAT"

    ref_lines = []
    if refs["red"] is not None:
        ref_lines.append(
            f"• ARMED (CE exit): latest red HA Low "
            f"({refs['red'].name.strftime('%H:%M')}) {refs['red']['HA_Low']:.2f}"
        )
    if refs["green"] is not None:
        ref_lines.append(
            f"• ARMED (PE exit): latest green HA High "
            f"({refs['green'].name.strftime('%H:%M')}) {refs['green']['HA_High']:.2f}"
        )
    if not ref_lines:
        ref_lines.append("• No exit level armed yet today (no red or green candle has closed yet)")
    ref_block = "\n".join(ref_lines)

    if position:
        extra = ""
        if position.get("partial_booked"):
            extra = "\n📌 Partial profit already booked — watching the remainder."
        pos_line = (
            f"📌 OPEN POSITION: {position['side']} @ spot "
            f"{position.get('entry_spot', 0):.2f} (opened {position.get('opened_date')}){extra}"
        )
    else:
        pos_line = "📌 OPEN POSITION: none — monitoring only"

    return f"""⏱️ 30-MIN MARKET STATUS UPDATE (tick-level watcher)
Time: {now_time}
Asset: NIFTY 50 (Spot)

{pos_line}

📊 LATEST 30M HEIKIN-ASHI DATA
• Standard Spot Close: {close:.2f}
• HA Candle Color: {candle_color}
• Current HA Open: {ha_open:.2f}
• Current HA Close: {ha_close:.2f}
• Current HA High: {ha_high:.2f}
• Current HA Low: {ha_low:.2f}

📉 REFERENCE EXIT LEVEL(S) — today only
{ref_block}

ℹ️ System Active."""


def _partial_profit_message(position: dict, ltp: float, now_time: str) -> str:
    lots_note = f"{engine.PARTIAL_PROFIT_FRACTION * 100:.0f}%"
    return f"""💰 PARTIAL PROFIT — BOOK {lots_note} NOW
Time: {now_time}
Contract: {position.get('tradingsymbol', position['side'])}

Entry Premium: {position['entry_premium']:.2f}
Current Premium: {ltp:.2f} ({engine.PARTIAL_PROFIT_MULTIPLIER:.0f}x entry reached)

⚡ ACTION REQUIRED: Book {lots_note} of your {position['side']} lots now.
The remainder stays open — the full-exit rule keeps watching it unchanged."""


def _full_exit_message(position: dict, side: str, now_time: str, ref_row, ref_field: str,
                        live_val: float, arrow: str) -> str:
    label = "CALL (CE)" if side == "CE" else "PUT (PE)"
    partial_note = (
        "\n(Partial profit was already booked earlier — this closes the remainder.)"
        if position.get("partial_booked") else ""
    )
    return f"""🛑 {label} EXIT SIGNAL TRIGGERED (live, tick-level)
Time: {now_time}
Reason: Latest {'red' if side == 'CE' else 'green'} 30m HA {'Low' if side == 'CE' else 'High'} \
(today) broken by current live HA {'Low' if side == 'CE' else 'High'} — fired mid-candle, not \
waiting for the candle to close.

📊 HEIKIN-ASHI DATA
• Reference ({ref_row.name.strftime('%H:%M')}): {ref_row[ref_field]:.2f}
• Current live HA {'Low' if side == 'CE' else 'High'}: {live_val:.2f} (Broken {arrow})
{partial_note}
⚡ ACTION REQUIRED: Exit ALL remaining open {label} lots!"""


def _handle_partial_profit(position: dict, now: dt.datetime) -> bool:
    """Returns True if position/state should be treated as modified."""
    if position.get("partial_booked") or "symbol_token" not in position:
        return False
    try:
        ltp = engine.PROVIDER.get_option_ltp(position["symbol_token"])
    except Exception as e:
        log.warning("Partial-profit LTP check failed (will retry next tick): %s", e)
        return False
    target = position["entry_premium"] * engine.PARTIAL_PROFIT_MULTIPLIER
    if ltp >= target:
        engine.send_telegram(_partial_profit_message(position, ltp, engine._stamp(now)))
        position["partial_booked"] = True
        log.info("Partial profit booked: %s ltp=%.2f target=%.2f", position["side"], ltp, target)
        return True
    return False


def _handle_full_exit(state: dict, position: dict, acc: _CandleAccumulator, refs: dict,
                       now: dt.datetime) -> bool:
    """Returns True if the position was closed (state modified)."""
    ha_open, ha_close, ha_high, ha_low = acc.live_ha()
    side = position["side"]
    now_time = engine._stamp(now)

    if side == "CE" and refs["red"] is not None and ha_low < refs["red"]["HA_Low"]:
        engine.send_telegram(_full_exit_message(
            position, "CE", now_time, refs["red"], "HA_Low", ha_low, "👇"))
        state["position"] = None
        log.info("Full CE exit fired: live_ha_low=%.2f ref=%.2f", ha_low, refs["red"]["HA_Low"])
        return True

    if side == "PE" and refs["green"] is not None and ha_high > refs["green"]["HA_High"]:
        engine.send_telegram(_full_exit_message(
            position, "PE", now_time, refs["green"], "HA_High", ha_high, "👆"))
        state["position"] = None
        log.info("Full PE exit fired: live_ha_high=%.2f ref=%.2f", ha_high, refs["green"]["HA_High"])
        return True

    return False


def _hard_cutoff(state: dict, position: dict, now: dt.datetime) -> bool:
    if now.time() < engine.HARD_EXIT_TIME:
        return False
    engine.send_telegram(f"""⏰ TIME CUTOFF REACHED ({engine.HARD_EXIT_TIME.strftime('%I:%M %p')} IST)
Asset: NIFTY 50
Open position: {position['side']} opened {position.get('opened_at', 'unknown')}

⚡ ACTION REQUIRED:
Square off ALL remaining open lots immediately!
Do not carry this position into a second night.""")
    state["position"] = None
    log.info("Hard cutoff square-off fired.")
    return True


# A stuck watcher that fails silently for days is exactly what happened in
# production (a stale Angel One session token, retried forever with no
# alert). These two thresholds turn sustained failures into a Telegram
# alert instead of a silent log line nobody's watching.
ALERT_AFTER_FAILURES = 3          # consecutive failures before the first alert
ALERT_COOLDOWN_SECONDS = 1800     # don't re-alert more often than this while still stuck


@dataclasses.dataclass
class _HealthTracker:
    consecutive_failures: int = 0
    last_alert: dt.datetime | None = None


def _record_failure(health: _HealthTracker, reason: str, now: dt.datetime) -> None:
    health.consecutive_failures += 1
    if health.consecutive_failures < ALERT_AFTER_FAILURES:
        return
    if health.last_alert is not None and (now - health.last_alert).total_seconds() < ALERT_COOLDOWN_SECONDS:
        return
    engine.send_telegram(f"""⚠️ BTST WATCHER STUCK
Time: {engine._stamp(now)}

The tick-level watcher has failed {health.consecutive_failures} times in a row:
{reason}

It keeps retrying automatically, and this alert repeats every \
{ALERT_COOLDOWN_SECONDS // 60} min while it stays broken — but exit \
monitoring is NOT running right now. Check watcher.log on the VM.""")
    health.last_alert = now


def _record_success(health: _HealthTracker, now: dt.datetime) -> None:
    if health.consecutive_failures >= ALERT_AFTER_FAILURES:
        engine.send_telegram(f"""✅ BTST WATCHER RECOVERED
Time: {engine._stamp(now)}

Exit monitoring is working again after {health.consecutive_failures} \
failed attempts.""")
    health.consecutive_failures = 0
    health.last_alert = None


def _run_one_tick(acc: _CandleAccumulator | None, refs: dict | None,
                   health: _HealthTracker) -> tuple[_CandleAccumulator, dict]:
    """last_status_bucket is deliberately NOT passed as an in-memory
    parameter — it's read from and written to state["watcher_last_status_bucket"]
    instead, so a crash-loop within the same candle can't resend duplicate
    status messages (the in-memory equivalent would reset to None on every
    restart; the persisted one survives it).
    """
    now = engine._now()

    with _locked_state() as state:
        position = state.get("position")

        if position and _hard_cutoff(state, position, now):
            return acc, refs

        current_bucket = _bucket_start(now)
        rolled_over = acc is None or current_bucket != acc.bucket_start
        if rolled_over:
            try:
                acc, refs = bootstrap(now.date())
            except engine.StaleDataError as e:
                # Holiday / market not open long enough yet for a candle —
                # expected and quiet, doesn't count as a failure.
                log.warning("Bootstrap skipped (will retry next tick): %s", e)
                return acc, refs
            except Exception as e:
                log.warning("Bootstrap failed (will retry next tick): %s", e)
                _record_failure(health, f"Bootstrap: {type(e).__name__}: {e}", now)
                return acc, refs
            _record_success(health, now)

            bucket_key = acc.bucket_start.isoformat()
            if state.get("watcher_last_status_bucket") != bucket_key and (position or engine.STATUS_WHEN_FLAT):
                ha_open, ha_close, ha_high, ha_low = acc.live_ha()
                engine.send_telegram(_status_message(
                    now.strftime("%H:%M IST"), position, ha_open, ha_close, ha_high, ha_low,
                    acc.close, refs,
                ))
                state["watcher_last_status_bucket"] = bucket_key

        if not position:
            return acc, refs

        try:
            ltp = engine.PROVIDER.get_index_ltp()
        except Exception as e:
            log.warning("Index LTP tick failed (will retry next tick): %s", e)
            _record_failure(health, f"Index LTP: {type(e).__name__}: {e}", now)
            return acc, refs
        _record_success(health, now)
        acc.tick(ltp)

        _handle_partial_profit(position, now)
        _handle_full_exit(state, position, acc, refs, now)

    return acc, refs


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    log.info("watcher starting; provider=%s poll=%.0fs", engine.PROVIDER.name, POLL_SECONDS)

    acc: _CandleAccumulator | None = None
    refs: dict | None = None
    health = _HealthTracker()
    idle_iterations = 0

    while True:
        now = engine._now()
        in_window = (
            now.weekday() < 5
            and engine.EXIT_MONITOR_FROM <= now.time() < WATCH_UNTIL
        )
        if not (_capable() and in_window):
            acc = None  # force a fresh bootstrap whenever we re-enter the window
            health = _HealthTracker()  # a stale failure streak shouldn't carry into tomorrow
            if idle_iterations % CAPABILITY_LOG_EVERY == 0:
                reason = "provider has no live LTP support" if not _capable() else "outside market hours"
                log.info("Idle (%s) — sleeping %ds.", reason, IDLE_SLEEP_SECONDS)
            idle_iterations += 1
            time.sleep(IDLE_SLEEP_SECONDS)
            continue

        idle_iterations = 0
        try:
            acc, refs = _run_one_tick(acc, refs, health)
        except Exception as e:
            log.exception("Unhandled error in watcher tick — continuing.")
            _record_failure(health, f"Tick loop: {type(e).__name__}: {e}", now)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
