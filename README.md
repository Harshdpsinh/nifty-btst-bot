# NIFTY 50 BTST Bot

Scans NIFTY 50 for a Buy-Today-Sell-Tomorrow options entry at ~3:20 PM IST and
monitors 30-minute Heikin-Ashi candles the next session for an exit. Alerts go
to Telegram.

## Strategy (unchanged)

**Entry** — at 3:20 PM IST, compare live spot against the forming daily
Heikin-Ashi close:

| Divergence (spot − HA close) | Action |
| --- | --- |
| `>= +11.0` pts | Buy Call (CE), premium ~Rs.100, NRML/CNC |
| `<= -11.0` pts | Buy Put (PE), premium ~Rs.100, NRML/CNC |
| between | No trade |

Execute between 3:21–3:28 PM IST.

**Exit** — next session, on 30m Heikin-Ashi candles:

- Holding CE: exit when the current HA low breaks below the previous **red**
  candle's HA low.
- Holding PE: exit when the current HA high breaks above the previous **green**
  candle's HA high.
- Hard square-off at **3:13 PM IST** regardless. Never carry into a second night.

All strategy constants live in the `STRATEGY` block at the top of
`btst_engine.py`.

## Setup

Add two repository secrets (Settings → Secrets and variables → Actions →
Secrets):

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` — your chat/channel ID

Verify the plumbing end to end: Actions → *Daily BTST Trading Engine* → **Run
workflow** → mode `selftest`. A message should arrive in Telegram within a
minute. If it doesn't, the run log says exactly which half failed.

## Data provider

Market data is read through a swappable provider (`providers.py`), selected
by the `DATA_PROVIDER` **repository variable** (Settings → Secrets and
variables → Actions → **Variables**, not Secrets) — switching feeds never
needs a code change or a push.

| `DATA_PROVIDER` | Cost | Notes |
| --- | --- | --- |
| `yahoo` (default) | Free, no account | Unofficial, ~15 min delayed. Fine to explore the strategy; risky for the live entry decision since the divergence threshold is only 11 points. |
| `angelone` | Free with an Angel One demat account | Official real-time NSE data via SmartAPI. **Stub only right now** — `AngelOneProvider` in `providers.py` raises `NotImplementedError` until credentials are supplied and its two data methods are filled in. |

To activate `angelone`: enable the SmartAPI add-on on your Angel One account,
generate an API key at https://smartapi.angelbroking.com, add
`ANGELONE_API_KEY`, `ANGELONE_CLIENT_ID`, `ANGELONE_PASSWORD`,
`ANGELONE_TOTP_SECRET` as repository **secrets**, then set the
`DATA_PROVIDER` variable to `angelone`. The workflow already forwards all of
these — see the `Run BTST engine` step in
`.github/workflows/btst_schedule.yml`.

Any other broker (Upstox, Fyers, Dhan, 5paisa, …) follows the same pattern:
add a class to `providers.py` implementing `daily_bars()` and
`intraday_bars()`, register it in `_PROVIDERS`, done. Nothing in
`btst_engine.py` needs to change.

## Running it manually

```bash
python btst_engine.py auto        # decide from the IST clock (what CI runs)
python btst_engine.py entry       # entry scan
python btst_engine.py exit        # 30m exit monitor
python btst_engine.py selftest    # verify credentials + data feed
python btst_engine.py exit --force  # ignore clock gating and dedup
```

Exit code is non-zero if a scan aborted or a Telegram send failed, so a silent
bot shows up as a red run rather than a green one.

## State

`state.json` is committed back to `main` by the workflow after each run. It
holds the open position, the date of the last entry scan, and the last candle
reported — which is what makes redundant wake-ups idempotent and lets a BTST
position survive overnight between the entry run and the next day's exit runs.

The `Persist state` step needs `permissions: contents: write`, which is already
set in the workflow.

To clear a stuck position, edit `state.json` on `main` and set
`"position": null`.

## ⚠️ Scheduling reliability — read this

**GitHub Actions cannot reliably hit a 7-minute trading window.** Its scheduled
queue is explicitly best-effort: runs get delayed under load and are sometimes
dropped outright. Measured on this repo on 2026-07-29:

| Cron (UTC) | Expected IST | Actually ran |
| --- | --- | --- |
| `03:45` | 09:15 | dropped |
| `04:45` | 10:15 | dropped |
| `05:45` | 11:15 | 06:18 UTC (+33 min) |
| `06:45` | 12:15 | dropped |
| `07:45` | 13:15 | dropped |
| `08:45` | 14:15 | 09:28 UTC (+43 min) |
| `09:45` | 15:15 | 11:48 UTC (+2h03) |
| `09:50` (entry) | 15:20 | 11:49 UTC (**+1h59 → 17:20 IST**) |

The entry scan ran nearly two hours after the market closed and computed its
divergence from the settled close.

This repo mitigates it two ways, but does not — cannot — fully solve it:

1. **Redundant scheduling.** The exit monitor is scheduled every 15 minutes so
   each 30m candle gets two chances; the entry scan gets four attempts across
   15:18–15:27 IST plus a 15:35 catch-all.
2. **Clock-gated logic.** `btst_engine.py` ignores which cron woke it and reads
   the real IST clock. A run that arrives after 3:28 PM sends a *missed window*
   notice with the numbers marked non-actionable, and records no position. It
   will never tell you to buy something two hours late again.

If you need the entry to actually land inside 3:21–3:28 PM every day, move the
scheduler off GitHub Actions — a small always-on VPS with real cron, or any
scheduler with an SLA, calling `python btst_engine.py entry`. The engine itself
needs no changes for that.

## Cost

The repo is private, so Actions minutes are metered (2000/month on the free
tier, billed rounded up to the whole minute per job). The schedule fires ~29
jobs per trading day ≈ **640 minutes/month**. If that gets tight, change the
exit cron from `*/15 4-9 * * 1-5` to `*/30 4-9 * * 1-5` — you lose the
per-candle redundancy but halve the cost.

## Known limitations

- Data comes from Yahoo Finance via `yfinance`, which is unofficial, delayed,
  and occasionally wrong. Do not treat it as a broker feed.
- NSE holidays are detected from the data (the daily candle's date), not a
  calendar, so a badly lagging feed looks the same as a holiday.
- The engine notifies; it does not place orders.
