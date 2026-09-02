#!/usr/bin/env python3
"""Phase 1 — offline / CSV / optional Angel One backtest of the BTST playbook.

Index-point proxy only (not option premium P&L). Mid-candle HA breaks cannot
be reconstructed from 30m OHLC; this uses closed-bar HA High/Low as bounds.

Usage:
    python backtest_btst.py                         # synthetic fixture (always works)
    python backtest_btst.py --csv-daily d.csv --csv-30m m.csv
    python backtest_btst.py --live --lookback-days 20   # Angel One, short window

CSV columns: Datetime, Open, High, Low, Close  (IST naive or tz-aware)
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass

# Fixture/CSV must run without Angel One secrets. --live uses real env if present.
os.environ.setdefault("DATA_PROVIDER", "angelone")
os.environ.setdefault("ANGELONE_API_KEY", "backtest-offline")
os.environ.setdefault("ANGELONE_CLIENT_ID", "backtest-offline")
os.environ.setdefault("ANGELONE_PASSWORD", "backtest-offline")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

import pandas as pd
import pytz

import btst_engine as engine

IST = pytz.timezone("Asia/Kolkata")
log = logging.getLogger("btst.backtest")


def entry_side(div: float, threshold: float = engine.DIVERGENCE_THRESHOLD) -> str | None:
    if div >= threshold:
        return "CE"
    if div <= -threshold:
        return "PE"
    return None


def _aware(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize(IST)
    return t.tz_convert(IST)


def _ensure_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        if "Datetime" in out.columns:
            out["Datetime"] = pd.to_datetime(out["Datetime"])
            out = out.set_index("Datetime")
        else:
            raise ValueError("Need a Datetime index or Datetime column")
    out.index = [_aware(i) for i in out.index]
    out.index = pd.DatetimeIndex(out.index)
    cols = {c: c.title() if c.lower() in ("open", "high", "low", "close") else c
            for c in out.columns}
    out = out.rename(columns=cols)
    for c in ("Open", "High", "Low", "Close"):
        if c not in out.columns:
            raise ValueError(f"Missing column {c}")
        out[c] = out[c].astype(float)
    return out.sort_index()


@dataclass
class Trade:
    entry_date: str
    exit_date: str
    side: str
    expiry: str
    expiry_label: str
    entry_spot: float
    exit_spot: float
    exit_reason: str          # ha_ce / ha_pe / cutoff_1513 / no_day2_bars
    exit_time: str
    overnight_gap: float      # Day2 09:15 Open − Day1 close (index pts)
    pnl_pts: float            # CE: exit-entry; PE: entry-exit. INDEX PROXY
    mae_open_to_0945: float   # worst signed move 09:15→09:45 against the side


@dataclass
class BacktestResult:
    trades: list
    skipped_flat: int
    skipped_no_day2: int
    notes: list


def walk_exits(side: str, day_ha: pd.DataFrame) -> tuple[str, pd.Timestamp, float]:
    """Day-2 only. Sticky refs from closed bars; HA break on the next bar; 15:13 cutoff.

    `day_ha` must already have HA_* and Is_Red / Is_Green.
    Returns (reason, timestamp, exit_spot).
    """
    if day_ha is None or day_ha.empty:
        raise ValueError("no day-2 bars")

    ref_red = None
    ref_green = None
    last_bar = None
    last_idx = None

    for idx, bar in day_ha.iterrows():
        start = engine.ist_minute(idx)
        last_bar, last_idx = bar, start
        # Ignore bars that start at/after 15:15 — cutoff already fired.
        if start.hour > 15 or (start.hour == 15 and start.minute >= 15):
            break

        # Forming/this bar vs refs from earlier closed bars of TODAY.
        if side == "CE" and ref_red is not None and bar["HA_Low"] < ref_red["HA_Low"]:
            return "ha_ce", start, float(bar["Close"])
        if side == "PE" and ref_green is not None and bar["HA_High"] > ref_green["HA_High"]:
            return "ha_pe", start, float(bar["Close"])

        if bar["Is_Red"]:
            ref_red = bar
        elif bar["Is_Green"]:
            ref_green = bar

        # 14:45 bar is still forming at 15:13 — force cutoff after checking it.
        if start.hour == 14 and start.minute == 45:
            return "cutoff_1513", start.replace(hour=15, minute=13), float(bar["Close"])

    spot = float(last_bar["Close"]) if last_bar is not None else float("nan")
    ts = last_idx if last_idx is not None else engine.ist_minute(day_ha.index[-1])
    return "cutoff_1513", ts, spot


def _next_session(dates: list[dt.date], today: dt.date) -> dt.date | None:
    for d in dates:
        if d > today:
            return d
    return None


def _mae_open_to_0945(side: str, entry_spot: float, day_ha: pd.DataFrame) -> float:
    """Worst index move against the position from 09:15 open through 09:45 bar."""
    if day_ha.empty:
        return 0.0
    window = []
    for idx, bar in day_ha.iterrows():
        start = engine.ist_minute(idx)
        if start.hour == 9 and start.minute in (15, 45):
            window.append(bar)
        if start.hour == 9 and start.minute > 45:
            break
        if start.hour >= 10:
            break
    if not window:
        return 0.0
    if side == "CE":
        worst = min(float(b["Low"]) for b in window)
        return worst - entry_spot   # negative = pain
    worst = max(float(b["High"]) for b in window)
    return entry_spot - worst


def run_backtest(daily: pd.DataFrame, intra: pd.DataFrame) -> BacktestResult:
    daily = _ensure_ohlc(daily)
    intra = _ensure_ohlc(intra)
    intra_ha = engine._heikin_ashi(intra)

    dates = sorted({_aware(i).date() for i in daily.index if _aware(i).weekday() < 5})
    trades: list[Trade] = []
    skipped_flat = 0
    skipped_no_day2 = 0
    notes: list[str] = []

    for d in dates:
        rows = daily[[_aware(i).date() == d for i in daily.index]]
        if rows.empty:
            continue
        bar = rows.iloc[-1]
        div = engine.daily_divergence(bar["Close"], bar["Open"], bar["High"], bar["Low"])
        side = entry_side(div)
        if side is None:
            skipped_flat += 1
            continue

        d2 = _next_session(dates, d)
        if d2 is None:
            skipped_no_day2 += 1
            notes.append(f"{d.isoformat()} {side} — no next session in sample")
            continue

        day2 = intra_ha[[_aware(i).date() == d2 for i in intra_ha.index]]
        expiry, label = engine._next_option_expiry(d)
        entry_spot = float(bar["Close"])
        if day2.empty:
            skipped_no_day2 += 1
            notes.append(f"{d.isoformat()} {side} — no 30m bars on {d2}")
            continue

        first_open = float(day2.iloc[0]["Open"])
        reason, when, exit_spot = walk_exits(side, day2)
        pnl = (exit_spot - entry_spot) if side == "CE" else (entry_spot - exit_spot)
        trades.append(Trade(
            entry_date=d.isoformat(),
            exit_date=d2.isoformat(),
            side=side,
            expiry=expiry.isoformat(),
            expiry_label=label,
            entry_spot=round(entry_spot, 2),
            exit_spot=round(exit_spot, 2),
            exit_reason=reason,
            exit_time=when.strftime("%Y-%m-%d %H:%M"),
            overnight_gap=round(first_open - entry_spot, 2),
            pnl_pts=round(pnl, 2),
            mae_open_to_0945=round(_mae_open_to_0945(side, entry_spot, day2), 2),
        ))

    return BacktestResult(trades=trades, skipped_flat=skipped_flat,
                          skipped_no_day2=skipped_no_day2, notes=notes)


def summarise(result: BacktestResult) -> str:
    trades = result.trades
    lines = [
        "BTST BACKTEST (index-point proxy — not option premium P&L)",
        f"Trades: {len(trades)}   flat days skipped: {result.skipped_flat}   "
        f"no-Day2 skipped: {result.skipped_no_day2}",
    ]
    if not trades:
        lines.append("No trades in sample.")
        if result.notes:
            lines.extend(result.notes[:8])
        return "\n".join(lines)

    wins = [t for t in trades if t.pnl_pts > 0]
    losses = [t for t in trades if t.pnl_pts <= 0]
    ce = sum(1 for t in trades if t.side == "CE")
    pe = len(trades) - ce
    ha = sum(1 for t in trades if t.exit_reason.startswith("ha_"))
    cut = sum(1 for t in trades if t.exit_reason == "cutoff_1513")
    avg_win = (sum(t.pnl_pts for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pts for t in losses) / len(losses)) if losses else 0.0
    expect = sum(t.pnl_pts for t in trades) / len(trades)
    # consecutive losses
    streak = best = 0
    for t in trades:
        if t.pnl_pts <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    two_nights = any(t.entry_date == t.exit_date for t in trades)
    lines += [
        f"CE/PE: {ce}/{pe}",
        f"Win rate: {len(wins) / len(trades):.1%}   expectancy: {expect:+.2f} pts/trade",
        f"Avg win: {avg_win:+.2f}   avg loss: {avg_loss:+.2f}   max loss streak: {best}",
        f"HA exits: {ha}   15:13 cutoffs: {cut}",
        f"Mean overnight gap: {sum(t.overnight_gap for t in trades) / len(trades):+.2f} pts",
        f"Mean MAE 09:15–09:45 (signed vs side): "
        f"{sum(t.mae_open_to_0945 for t in trades) / len(trades):+.2f} pts",
        f"Same-day exit leaked: {'YES — BUG' if two_nights else 'no (playbook held)'}",
        "",
        f"{'entry':<12} {'side':<4} {'exit':<12} {'reason':<14} {'pnl':>8} {'gap':>8}",
    ]
    for t in trades:
        lines.append(
            f"{t.entry_date:<12} {t.side:<4} {t.exit_date:<12} {t.exit_reason:<14} "
            f"{t.pnl_pts:>+8.1f} {t.overnight_gap:>+8.1f}"
        )
    lines += [
        "",
        "Caveats: official daily close stands in for 15:21 live spot; 30m OHLC cannot",
        "replay mid-candle HA breaks; option premium / theta / slippage are NOT in pnl_pts.",
    ]
    return "\n".join(lines)


def synthetic_fixture() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two sessions: Wed CE trigger, Thu 09:15 red then 09:45 breaks it."""
    def day_bars(day: dt.date, o, h, l, c):
        ts = IST.localize(dt.datetime(day.year, day.month, day.day))
        return pd.DataFrame(
            {"Open": [o], "High": [h], "Low": [l], "Close": [c]},
            index=[ts],
        )

    wed, thu, fri = dt.date(2026, 8, 26), dt.date(2026, 8, 27), dt.date(2026, 8, 28)
    # Wed close-as-proxy: div = 24530 - (24500+24550+24490+24530)/4 = +17.5 CE
    daily = pd.concat([
        day_bars(wed, 24500, 24550, 24490, 24530),
        day_bars(thu, 24520, 24540, 24400, 24420),
        day_bars(fri, 24420, 24450, 24400, 24430),
    ])

    def m30(day, hm, o, h, l, c):
        hh, mm = hm
        ts = IST.localize(dt.datetime(day.year, day.month, day.day, hh, mm))
        return {"Datetime": ts, "Open": o, "High": h, "Low": l, "Close": c}

    # Seed a prior day so HA recursion is defined, then Thu session.
    rows = []
    tue = dt.date(2026, 8, 25)
    rows.append(m30(tue, (15, 15), 24480, 24500, 24470, 24490))
    # Thu 09:15 strongly down so HA is red (HA_Close < HA_Open from tue seed)
    rows.append(m30(thu, (9, 15), 24520, 24520, 24400, 24410))
    # Thu 09:45 continues lower — HA_Low undercuts 09:15
    rows.append(m30(thu, (9, 45), 24410, 24420, 24350, 24360))
    rows.append(m30(thu, (10, 15), 24400, 24420, 24395, 24410))
    intra = pd.DataFrame(rows).set_index("Datetime")
    return daily, intra


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    return _ensure_ohlc(df)


def load_live(lookback_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if os.getenv("ANGELONE_API_KEY") in (None, "", "backtest-offline"):
        raise SystemExit("--live needs real ANGELONE_* in the environment (not the offline stub).")
    daily = engine.PROVIDER.daily_bars(engine.SYMBOL, lookback_days)
    intra = engine.PROVIDER.intraday_bars(
        engine.SYMBOL, engine.INTRADAY_INTERVAL_MIN, min(lookback_days, 10)
    )
    return daily, intra


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    p = argparse.ArgumentParser(description="NIFTY BTST playbook backtest (index-point proxy)")
    p.add_argument("--csv-daily")
    p.add_argument("--csv-30m")
    p.add_argument("--live", action="store_true", help="fetch Angel One (short lookback)")
    p.add_argument("--lookback-days", type=int, default=20)
    args = p.parse_args(argv)

    if args.csv_daily and args.csv_30m:
        daily, intra = load_csv(args.csv_daily), load_csv(args.csv_30m)
        source = "csv"
    elif args.live:
        daily, intra = load_live(args.lookback_days)
        source = "angelone"
    else:
        daily, intra = synthetic_fixture()
        source = "synthetic fixture"
        print("No CSV / --live given — running the built-in two-day fixture.\n")

    result = run_backtest(daily, intra)
    print(f"Source: {source}\n")
    print(summarise(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
