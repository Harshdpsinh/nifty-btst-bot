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

**Exit** — next session, on 30m Heikin-Ashi candles, compared against a
**fixed reference: the entry day's own closing 30m candle** — not whatever
candle happens to precede the current one. That reference never changes
during the exit day, no matter how many candles pass:

- Holding CE: exit when the current HA low breaks below the entry day's
  closing candle's HA low (only armed if that candle was **red**).
- Holding PE: exit when the current HA high breaks above the entry day's
  closing candle's HA high (only armed if that candle was **green**).
- Hard square-off at **3:13 PM IST** regardless. Never carry into a second night.

(Earlier versions of this bot compared against the immediately-preceding
candle, which drifted through the day instead of staying pinned to the
entry day — fixed, with a regression test proving the two approaches
produce different answers on the same data.)

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
validated with a mocked HTTP layer offline (23 checks: auth flow, token
caching, 401 → re-login, malformed/empty responses, interval mapping). Step
4 above is not optional the first time you switch to it.

If prices ever look wrong after switching, the likely cause is
`ANGELONE_SYMBOL_TOKEN` — it defaults to `99926000` (the commonly published
NSE token for the "Nifty 50" index); `providers.py`'s `AngelOneProvider`
docstring explains how to look up the correct one from Angel One's scrip
master if that default is ever wrong for your account. That's a variable
change, not a code change.

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
  engine cycle.
- Installs a crontab entry running it every 5 minutes, 9:00–15:35 IST,
  Monday–Friday — real per-second cron, not a best-effort queue.

After it finishes:

```bash
nano ~/.btst.env                              # fill in your real values
~/nifty-btst-bot/deploy/run_engine.sh         # test it once by hand
tail -30 ~/nifty-btst-bot/engine.log          # confirm it ran clean
```

Once a Telegram message arrives from that manual run, cron takes over
automatically — nothing else to do. Re-running the setup script later (to
pull code updates) is safe: it won't duplicate the crontab entry or touch
your existing `.btst.env`.

State (`~/.btst_state.json`) is kept outside the repo on purpose, so pulling
a code update can never conflict with or clobber your current position.

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
- The engine notifies; it does not place orders.
