# NIFTY 50 BTST Bot

Scans NIFTY 50 for a Buy-Today-Sell-Tomorrow options entry at ~3:20 PM IST and
monitors 30-minute Heikin-Ashi candles the next session for an exit. Alerts go
to Telegram.

## Strategy

**Entry** — at 3:20 PM IST, compare live spot against the forming daily
Heikin-Ashi close:

| Divergence (spot − HA close) | Action |
| --- | --- |
| `>= +11.0` pts | Buy Call (CE), premium ~Rs.100, NRML/CNC |
| `<= -11.0` pts | Buy Put (PE), premium ~Rs.100, NRML/CNC |
| between | No trade |

Execute between 3:21–3:28 PM IST.

**Which expiry to buy** — weekly NIFTY expiry is Tuesday. Never buy a
contract inside its own expiry week's Monday or Tuesday (too close: heavy
decay, no room for the move to play out overnight):

| Signal fires on | Buy this expiry |
| --- | --- |
| Monday or Tuesday | **Next** week's Tuesday (this week's is too close) |
| Wed / Thu / Fri | The nearest upcoming Tuesday |

If that Tuesday is the last one in its calendar month, it's the monthly
contract (NSE doesn't list a separate weekly that week) — the signal message
labels it `MONTHLY` instead of `WEEKLY` so there's no ambiguity. Pure date
arithmetic (`_next_option_expiry` in `btst_engine.py`), no options-chain
lookup involved — if NSE ever moves the weekly expiry weekday again (it has
before), `WEEKLY_EXPIRY_WEEKDAY` is the one constant to change.

**Exit** — next session, on 30m Heikin-Ashi candles built fresh for that
day only (never a continuation from the entry day or any earlier session).
The reference level is **sticky**: the most recently *closed* red (CE) /
green (PE) candle seen so far that day, updating forward each time a newer
one of that colour closes. A candle of the other colour closing in between
does **not** erase it — and it's always the *latest* one, never "first
seen" or "lowest/highest so far":

- Holding CE: exit when price breaks below the latest closed red candle's HA low.
- Holding PE: exit when price breaks above the latest closed green candle's HA high.
- Checked **tick-level, live** (Angel One only — see below) — fires the
  moment the still-forming candle's running low/high crosses the reference,
  without waiting for that candle to close. On other providers it's checked
  once per cron invocation (every 5-15 min) instead.
- Hard square-off at **3:13 PM IST** regardless. Never carry into a second night.

**Partial profit booking** (Angel One only, needs a live per-strike premium
Yahoo doesn't have): once the resolved contract's live premium reaches **2×
its actual entry premium**, the bot immediately books 50% of the position.
The remaining 50% keeps being watched by the exact same full-exit rule above,
unchanged. Both the multiplier (`PARTIAL_PROFIT_MULTIPLIER`) and the fraction
booked (`PARTIAL_PROFIT_FRACTION`) are constants in `btst_engine.py`.

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
| `angelone` | Free with an Angel One demat account | Official real-time NSE data via SmartAPI's `getCandleData`. Implemented in `providers.py` — see below to activate. |

### Activating `angelone`

1. Enable the SmartAPI add-on on your Angel One account and generate an API
   key at https://smartapi.angelbroking.com. This also gives you a TOTP
   secret for 2FA — save the secret itself, not a one-time code from it.
2. Add these four as repository **secrets** (Settings → Secrets and
   variables → Actions → Secrets): `ANGELONE_API_KEY`, `ANGELONE_CLIENT_ID`,
   `ANGELONE_PASSWORD`, `ANGELONE_TOTP_SECRET`. The workflow already
   forwards all of them — see the `Run BTST engine` step in
   `.github/workflows/btst_schedule.yml`.
3. Set the `DATA_PROVIDER` repository **variable** to `angelone`.
4. Verify before trusting it for a real trade: Actions → *Daily BTST Trading
   Engine* → **Run workflow** → mode `selftest`.

**This integration has not been exercised against a real Angel One
account** — only against SmartAPI's documented request/response shape,
validated with a mocked HTTP layer offline (34 checks across auth flow,
token caching, 401 → re-login, malformed/empty responses, interval mapping,
option-chain search, and batch quote parsing). Step 4 above is not optional
the first time you switch to it.

If prices ever look wrong after switching, the likely cause is
`ANGELONE_SYMBOL_TOKEN` — it defaults to `99926000` (the commonly published
NSE token for the "Nifty 50" index); `providers.py`'s `AngelOneProvider`
docstring explains how to look up the correct one from Angel One's scrip
master if that default is ever wrong for your account. That's a variable
change, not a code change.

Any other broker (Upstox, Fyers, Dhan, 5paisa, …) follows the same pattern:
add a class to `providers.py` implementing `daily_bars()` and
`intraday_bars()`, register it in `_PROVIDERS`, done. Nothing in
`btst_engine.py` needs to change. Partial-profit tracking and the tick-level
watcher additionally need `resolve_option_contract()`, `get_index_ltp()`,
and `get_option_ltp()` (see `providers.py`'s `AngelOneProvider` for the
reference implementation) — a broker without those just gets the full-exit
rule via cron, same as Yahoo.

## Tick-level exit monitoring (`watcher.py`)

On `DATA_PROVIDER=angelone`, exit monitoring is NOT handled by cron —
`btst_engine.py`'s `run_auto()` automatically steps aside (it detects
`PROVIDER.get_index_ltp`) so the two never race on the same position.
Instead, `watcher.py` is a **separate, persistent process** that:

- Loops every `BTST_WATCHER_POLL_SECONDS` (default 10s) during market hours,
  polling live LTP for the NIFTY index and (if applicable) the held option
  contract — far faster than cron's 5-15 min cadence.
- Reconstructs the still-forming 30m candle's Heikin-Ashi values in memory
  from those ticks, re-syncing against the authoritative `getCandleData`
  result at every real candle boundary (so tick-by-tick drift can never
  compound — the source of truth is always refreshed every 30 minutes).
- Fires the full-exit and partial-profit alerts the instant a break happens,
  not on the next cron wake-up.
- Persists all state through the same `state.json` as cron, coordinated by a
  file lock (`_locked_state()`) so a cron invocation and the watcher can
  never corrupt each other's writes even if they land in the same instant.
- On any provider other than Angel One, it just idles (logging why every so
  often) — safe to leave the systemd service enabled regardless.

Not meant to be run ad hoc — see "Running on your own server" below for the
systemd service that keeps it alive continuously. Verified with 33 offline
checks against a mocked provider: bucket-boundary math (NSE candles align to
`:15`/`:45`, not `:00`/`:30` — the session opens at 9:15), the live
accumulator's HA math cross-checked against the same tested batch formula
`btst_engine.py` itself uses, a full tick-by-tick sequence proving partial-
then-full-exit fires exactly once each with no duplicates, hard-cutoff
force-exit, and that a simulated process restart mid-day recovers cleanly
from state alone. Like the rest of the Angel One integration, it has not
been run against a real account — the same caution above applies.

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
scheduler off GitHub Actions entirely — see the next section.

## Running on your own server (Oracle Cloud, Raspberry Pi, etc.)

GitHub Actions cron cannot be fixed — only worked around. `deploy/oracle_vm_setup.sh`
sets the bot up on any Ubuntu/Debian box (Oracle Cloud's Always Free ARM VM,
a Raspberry Pi, a spare laptop — anything left on during market hours) using
real cron instead:

```bash
ssh your-vm
curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/oracle_vm_setup.sh | bash
```

This one-time script:
- Sets the VM's timezone to IST, so cron entries mean what they look like —
  no UTC conversion, unlike GitHub Actions.
- Clones the repo, creates a venv, installs dependencies.
- Creates `~/.btst.env` (chmod 600, **outside** the git working tree) with
  empty placeholders for your secrets.
- Creates `deploy/run_engine.sh`, which loads that env file and runs one
  engine cycle (the daily entry decision on every provider, plus the 30m
  exit rule as a Yahoo fallback).
- Installs a crontab entry running it every 5 minutes, 9:00–15:35 IST,
  Monday–Friday — real per-second cron, not a best-effort queue.
- Installs and enables `btst-watcher.service` (systemd) — the persistent
  tick-level exit monitor from the section above. Auto-restarts on crash,
  survives reboots, and is safe to leave enabled even if you're on
  `DATA_PROVIDER=yahoo` (it just idles).

After it finishes:

```bash
nano ~/.btst.env                              # fill in your real values
~/nifty-btst-bot/deploy/run_engine.sh         # test the entry/exit cron path once by hand
tail -30 ~/nifty-btst-bot/engine.log          # confirm it ran clean
```

Once a Telegram message arrives from that manual run, cron takes over
automatically — nothing else to do. Re-running the setup script later (to
pull code updates) is safe: it won't duplicate the crontab entry, restart
the watcher unnecessarily, or touch your existing `.btst.env`.

If you're on `DATA_PROVIDER=angelone`, after filling in the `ANGELONE_*`
secrets, restart the watcher so it picks them up (`EnvironmentFile` is only
read at service start, not live):

```bash
sudo systemctl restart btst-watcher
sudo systemctl status btst-watcher     # confirm it's active
tail -30 ~/nifty-btst-bot/watcher.log  # watch it tick
```

State (`~/.btst_state.json`) is kept outside the repo on purpose, so pulling
a code update can never conflict with or clobber your current position. Both
cron and the watcher read/write it, coordinated by a file lock so the two
processes can never corrupt each other's writes.

**Once this is confirmed working, disable the GitHub Actions schedule** so
you don't get duplicated notifications from both places: Settings →
Actions → the workflow → ⋯ → Disable workflow, or delete the `schedule:`
block in `.github/workflows/btst_schedule.yml` and keep `workflow_dispatch:`
for occasional manual runs (e.g. `selftest`).

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
- The engine notifies; it does not place orders — including partial-profit
  and full-exit alerts. You still execute every trade by hand.
- Partial-profit booking and tick-level exits only exist on
  `DATA_PROVIDER=angelone` and require `watcher.py` running continuously
  (the systemd service). On Yahoo, or if the watcher isn't running, you get
  the full-exit rule only, checked at cron's cadence — no partial booking.
- None of the Angel One integration (candles, option-chain resolution, live
  quotes, the watcher) has been run against a real account. Treat every
  first switch-over as something to watch closely, not something to trust
  unattended on day one.
