"""Persistent tick-level exit monitor (Angel One).

Owns Day-2 exits while its heartbeat is fresh. Cron covers exits if this
process dies. Same-day (Day-1) positions are never exited — hold overnight.
A position that already had its one exit session and is still open is a
leftover: square off immediately, never a second night.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
import sys
import time

import btst_engine as engine

log = logging.getLogger("btst.watcher")

POLL_SECONDS = float(os.getenv("BTST_WATCHER_POLL_SECONDS", "10"))
IDLE_SLEEP_SECONDS = 1800
CAPABILITY_LOG_EVERY = 180
CUTOFF_RETRY_SECONDS = 60

# A few minutes past HARD_EXIT_TIME so we observe the 15:13 cutoff ourselves.
WATCH_UNTIL = dt.time(15, 20)
# Leftover square-off can fire at the open; HA arms at 09:45.
WATCH_FROM = engine.LEFTOVER_WATCH_FROM


def _capable() -> bool:
    return hasattr(engine.PROVIDER, "get_index_ltp") and hasattr(engine.PROVIDER, "get_option_ltp")


@dataclasses.dataclass
class _CandleAccumulator:
    """Live OHLC for the still-forming 30m bucket. bucket_start is the NSE
    bucket (09:45, 10:15, …), never 'whatever the API last returned'.
    """
    bucket_start: dt.datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_ha_open: float
    prev_ha_close: float

    def tick(self, ltp: float) -> None:
        if self.open is None:
            self.open = self.high = self.low = self.close = ltp
            return
        self.high = max(self.high, ltp)
        self.low = min(self.low, ltp)
        self.close = ltp

    def live_ha(self) -> tuple[float, float, float, float] | None:
        if self.open is None:
            return None
        ha_open = (self.prev_ha_open + self.prev_ha_close) / 2.0
        ha_close = (self.open + self.high + self.low + self.close) / 4.0
        ha_high = max(self.high, ha_open, ha_close)
        ha_low = min(self.low, ha_open, ha_close)
        return ha_open, ha_close, ha_high, ha_low


def bootstrap(today: dt.date, now: dt.datetime) -> tuple[_CandleAccumulator, dict]:
    """Rebuild today's HA refs from closed bars only; seed the forming bucket
    from the API bar if present, otherwise from live LTP.

    Does NOT treat the last history bar as forming just because it is last —
    that delayed the 09:45 arm by a full candle and re-hit getCandleData
    every tick when the forming bar was absent.
    """
    ha_df, prev_session_row = engine.calculate_30m_heikin_ashi_day_and_prev(today)
    current_bucket = engine.nse_30m_bucket_start(now)
    closed, forming_row = engine.split_closed_and_forming(ha_df, now)

    refs: dict = {"red": None, "green": None}
    ref_red, ref_green = engine.sticky_refs(closed)
    refs["red"] = ref_red
    refs["green"] = ref_green

    if len(closed) > 0:
        seed_open = float(closed.iloc[-1]["HA_Open"])
        seed_close = float(closed.iloc[-1]["HA_Close"])
    elif prev_session_row is not None:
        seed_open = float(prev_session_row["HA_Open"])
        seed_close = float(prev_session_row["HA_Close"])
    else:
        seed_row = forming_row if forming_row is not None else ha_df.iloc[-1]
        seed_open = (float(seed_row["Open"]) + float(seed_row["Close"])) / 2.0
        seed_close = seed_open

    if forming_row is not None:
        acc = _CandleAccumulator(
            bucket_start=current_bucket,
            open=float(forming_row["Open"]), high=float(forming_row["High"]),
            low=float(forming_row["Low"]), close=float(forming_row["Close"]),
            prev_ha_open=seed_open, prev_ha_close=seed_close,
        )
    else:
        ltp = None
        try:
            ltp = engine.PROVIDER.get_index_ltp()
        except Exception as e:
            log.warning("No forming bar in history and LTP seed failed: %s", e)
        acc = _CandleAccumulator(
            bucket_start=current_bucket,
            open=ltp, high=ltp, low=ltp, close=ltp,
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
    if position.get("partial_booked") or "symbol_token" not in position:
        return False
    if "entry_premium" not in position:
        return False
    try:
        ltp = engine.PROVIDER.get_option_ltp(position["symbol_token"])
    except Exception as e:
        log.warning("Partial-profit LTP check failed (will retry next tick): %s", e)
        return False
    target = position["entry_premium"] * engine.PARTIAL_PROFIT_MULTIPLIER
    if ltp >= target:
        if not engine.send_telegram(_partial_profit_message(position, ltp, engine._stamp(now))):
            log.error("Partial-profit alert UNDELIVERED — will retry next tick.")
            return False
        position["partial_booked"] = True
        log.info("Partial profit booked: %s ltp=%.2f target=%.2f", position["side"], ltp, target)
        return True
    return False


def _handle_full_exit(state: dict, position: dict, acc: _CandleAccumulator, refs: dict,
                       now: dt.datetime) -> bool:
    live = acc.live_ha()
    if live is None:
        return False
    ha_open, ha_close, ha_high, ha_low = live
    side = position["side"]
    now_time = engine._stamp(now)

    if side == "CE" and refs["red"] is not None and ha_low < refs["red"]["HA_Low"]:
        if not engine.send_telegram(_full_exit_message(
                position, "CE", now_time, refs["red"], "HA_Low", ha_low, "👇")):
            log.error("CE EXIT alert UNDELIVERED — position kept open, retrying next tick.")
            return False
        state["position"] = None
        log.info("Full CE exit fired: live_ha_low=%.2f ref=%.2f", ha_low, refs["red"]["HA_Low"])
        return True

    if side == "PE" and refs["green"] is not None and ha_high > refs["green"]["HA_High"]:
        if not engine.send_telegram(_full_exit_message(
                position, "PE", now_time, refs["green"], "HA_High", ha_high, "👆")):
            log.error("PE EXIT alert UNDELIVERED — position kept open, retrying next tick.")
            return False
        state["position"] = None
        log.info("Full PE exit fired: live_ha_high=%.2f ref=%.2f", ha_high, refs["green"]["HA_High"])
        return True

    return False


def _hard_cutoff(state: dict, position: dict, now: dt.datetime) -> bool:
    if now.time() < engine.HARD_EXIT_TIME:
        return False
    if engine.is_same_day_position(position, now.date()):
        return False
    if not engine.send_telegram(engine.cutoff_message(position)):
        log.error("TIME CUTOFF alert UNDELIVERED — position kept, retrying.")
        return True
    state["position"] = None
    log.info("Hard cutoff square-off fired.")
    return True


def _leftover_exit(state: dict, position: dict, now: dt.datetime) -> bool:
    if not engine.is_leftover_position(position, now.date()):
        return False
    if not engine.send_telegram(engine.leftover_message(position, engine._stamp(now))):
        log.error("LEFTOVER alert UNDELIVERED — position kept, retrying.")
        return True
    state["position"] = None
    log.info("Leftover square-off fired.")
    return True


ALERT_AFTER_FAILURES = 3
ALERT_COOLDOWN_SECONDS = 1800
BOOTSTRAP_BACKOFF_SECONDS = (30, 60, 120, 300, 600)


@dataclasses.dataclass
class _HealthTracker:
    consecutive_failures: int = 0
    last_alert: dt.datetime | None = None
    next_bootstrap_at: dt.datetime | None = None


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
monitoring is NOT running right now. Check watcher.log on the VM.

Cron will cover 30-minute exits while the heartbeat is stale.""")
    health.last_alert = now


def _record_success(health: _HealthTracker, now: dt.datetime) -> None:
    if health.consecutive_failures >= ALERT_AFTER_FAILURES:
        engine.send_telegram(f"""✅ BTST WATCHER RECOVERED
Time: {engine._stamp(now)}

Exit monitoring is working again after {health.consecutive_failures} \
failed attempts.""")
    health.consecutive_failures = 0
    health.last_alert = None
    health.next_bootstrap_at = None


def _touch_heartbeat(state: dict, now: dt.datetime) -> None:
    state["watcher_heartbeat"] = now.isoformat()


def _run_one_tick(acc: _CandleAccumulator | None, refs: dict | None,
                   health: _HealthTracker) -> tuple[_CandleAccumulator | None, dict | None]:
    now = engine._now()
    today = now.date()

    with engine.locked_state() as state:
        _touch_heartbeat(state, now)
        position = state.get("position")

        if position and _leftover_exit(state, position, now):
            return acc, refs

        if position and engine.is_same_day_position(position, today):
            log.info("Same-day position — overnight hold, watcher not exiting.")
            return acc, refs

        # Today is this position's one exit session. Stamp it now, before the
        # cutoff and before any candle fetch — stamping later meant a stale
        # feed or an undelivered cutoff left exit_session_date empty and the
        # leftover guard let the trade run a second night.
        if position:
            engine.mark_exit_session(position, today)

        if position and _hard_cutoff(state, position, now):
            return acc, refs

        if now.time() < engine.EXIT_MONITOR_FROM:
            return acc, refs

        current_bucket = engine.nse_30m_bucket_start(now)
        rolled_over = (
            acc is None
            or engine.ist_minute(current_bucket) != engine.ist_minute(acc.bucket_start)
        )
        if rolled_over:
            if health.next_bootstrap_at is not None and now < health.next_bootstrap_at:
                return acc, refs
            try:
                acc, refs = bootstrap(today, now)
            except engine.StaleDataError as e:
                log.warning("Bootstrap skipped (backing off): %s", e)
                health.next_bootstrap_at = now + dt.timedelta(
                    seconds=BOOTSTRAP_BACKOFF_SECONDS[0])
                return acc, refs
            except Exception as e:
                idx = min(health.consecutive_failures, len(BOOTSTRAP_BACKOFF_SECONDS) - 1)
                delay = BOOTSTRAP_BACKOFF_SECONDS[idx]
                health.next_bootstrap_at = now + dt.timedelta(seconds=delay)
                log.warning("Bootstrap failed (retrying in %ds): %s", delay, e)
                _record_failure(health, f"Bootstrap: {type(e).__name__}: {e}", now)
                return acc, refs
            _record_success(health, now)

            bucket_key = acc.bucket_start.isoformat()
            live = acc.live_ha()
            if (live is not None
                    and state.get("watcher_last_status_bucket") != bucket_key
                    and (position or engine.STATUS_WHEN_FLAT)):
                ha_open, ha_close, ha_high, ha_low = live
                if engine.send_telegram(_status_message(
                        now.strftime("%H:%M IST"), position, ha_open, ha_close, ha_high, ha_low,
                        acc.close if acc.close is not None else 0.0, refs)):
                    state["watcher_last_status_bucket"] = bucket_key
                else:
                    log.error("Status update UNDELIVERED — will retry next tick.")

        if not position:
            return acc, refs

        if acc is None:
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


def _should_stay_awake(now: dt.datetime, state: dict) -> bool:
    """Market hours, or an open position that still needs cutoff/leftover retries."""
    if now.weekday() >= 5:
        return False
    position = state.get("position")
    if position and not engine.is_same_day_position(position, now.date()):
        if engine.is_leftover_position(position, now.date()):
            return True
        if now.time() >= engine.HARD_EXIT_TIME:
            return True
    return WATCH_FROM <= now.time() < WATCH_UNTIL


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
        with engine.locked_state() as peek:
            stay = _should_stay_awake(now, peek)
            # Heartbeat even while deciding, so cron knows we're alive during hours.
            if stay:
                peek["watcher_heartbeat"] = now.isoformat()

        if not (_capable() and stay):
            acc = None
            health = _HealthTracker()
            nxt = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if now >= nxt:
                nxt += dt.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
            sleep_for = min(IDLE_SLEEP_SECONDS, max(10.0, (nxt - now).total_seconds()))
            if idle_iterations % CAPABILITY_LOG_EVERY == 0:
                reason = "provider has no live LTP support" if not _capable() else "outside market hours"
                log.info("Idle (%s) — sleeping %.0fs.", reason, sleep_for)
            idle_iterations += 1
            time.sleep(sleep_for)
            continue

        idle_iterations = 0
        try:
            acc, refs = _run_one_tick(acc, refs, health)
        except Exception as e:
            log.exception("Unhandled error in watcher tick — continuing.")
            _record_failure(health, f"Tick loop: {type(e).__name__}: {e}", now)

        # After 15:13 with an undelivered cutoff, don't hammer every 10s all evening.
        if now.time() >= engine.HARD_EXIT_TIME:
            sleep_for = CUTOFF_RETRY_SECONDS
        else:
            sleep_for = POLL_SECONDS
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
