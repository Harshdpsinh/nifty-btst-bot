# NIFTY 50 BTST Bot

Buy near the close, hold **one night**, exit on the next session’s own
30-minute Heikin-Ashi candles. Alerts go to Telegram. **Angel One SmartAPI
only** (free with a demat account). The bot notifies — it does not place
orders.

## Playbook (what the code enforces)

| Phase | When | Rule |
| --- | --- | --- |
| Entry | Day 1 · 15:18–15:28 IST | `divergence = spot − (O+H+L+spot)/4`. ≥ +11 buy CE, ≤ −11 buy PE, else no trade. Strike nearest live premium ₹100 (LTP > 0). NRML/CNC. |
| Overnight | Day 1 close → Day 2 open | Hold. **No same-day HA exit.** |
| Exit | Day 2 · 09:45–15:13 | Sticky latest **closed** red (CE) / green (PE) of **today only**. Break fires live, mid-candle, every ~10s. |
| Partial | Day 2, Angel One | Option LTP hits 2× **fill** (or quoted premium if you haven’t set fill) → book 50%. Remainder keeps the HA exit. |
| Hard cutoff | Day 2 · 15:13 | Square off. No second night. If Telegram fails, the bot **retries** and will leftover-alert the next session rather than go silent. |
| Leftover | Any later session | Position that already had its one exit day and is still open → ⏰ SQUARE OFF NOW. |

Expiry: weekly Tuesday. Mon/Tue entries roll to next week. If that Tuesday is a
holiday, the chain lookup walks back to the previous weekday.

## Update the Oracle VM (do this to go live today)

State lives in `~/.btst_state.json` — **outside** the repo — so a pull cannot
wipe an open position.

**This chat cannot SSH your VM.** Open Oracle Cloud Shell / PuTTY / your
terminal, SSH in, then paste **one** line:

```bash
curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/update_from_main.sh | bash
```

That pulls `main`, forces `DATA_PROVIDER=angelone`, installs deps, restarts
`btst-watcher`, and runs `selftest`. It never deletes `~/.btst_state.json`.

Manual equivalent:

```bash
ssh your-vm
cd ~/nifty-btst-bot
git pull --ff-only
grep DATA_PROVIDER ~/.btst.env    # must be angelone
.venv/bin/pip install -r requirements.txt
sudo systemctl restart btst-watcher
sudo systemctl status btst-watcher
~/nifty-btst-bot/.venv/bin/python ~/nifty-btst-bot/btst_engine.py selftest
```
If you bought today and your fill ≠ the quoted premium in the 🚨 message:

```bash
cd ~/nifty-btst-bot && .venv/bin/python btst_engine.py fill 108.50
```

GitHub Actions has **no schedule**. Do not re-enable it — the VM is the live
path. `workflow_dispatch` / `selftest` is optional.

## What changed vs the old bot (playbook bugs)

1. **No Yahoo.** An unofficial 15-minute delayed feed cannot drive an 11-point trigger.
2. **No same-day exit** after the 15:20 buy (overnight hold).
3. **Leftover square-off** if a position survives its one exit session (never two nights).
4. **Watcher down ≠ silence.** Heartbeat every tick; if it’s stale >2 min, cron covers 30m exits and Telegram-alerts you.
5. **09:45 arm is real.** Closed bars are those with start < current NSE 30m bucket. Missing forming candle from `getCandleData` no longer delays the first level or re-hits historical every 10s.
6. **Failed Telegram no longer clears the position** on the cron path either.
7. **Stale feed inside 15:18–15:28 retries**; it does not burn the whole day.
8. **LTP 0 strikes are ignored.** JWT is cleared only on real auth errors, not on candle 403s.
9. File lock + atomic `state.json` write shared by cron and watcher.

Offline checks: `python test_playbook_guards.py` (16 tests).

## Setup (new VM)

```bash
ssh your-vm
curl -fsSL https://raw.githubusercontent.com/Harshdpsinh/nifty-btst-bot/main/deploy/oracle_vm_setup.sh | bash
nano ~/.btst.env          # TELEGRAM_* and ANGELONE_*
sudo systemctl restart btst-watcher
```

Secrets (never commit these):

- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `ANGELONE_API_KEY` / `ANGELONE_CLIENT_ID` / `ANGELONE_PASSWORD` / `ANGELONE_TOTP_SECRET`

`ANGELONE_SYMBOL_TOKEN` defaults to `99926000` (Nifty 50). Override only if prices look like the wrong instrument.

## Manual commands

```bash
python btst_engine.py auto         # what cron runs
python btst_engine.py entry
python btst_engine.py exit
python btst_engine.py selftest
python btst_engine.py fill 108.50  # actual fill for 2× partial
python btst_engine.py exit --force
```

Non-zero exit if a scan aborted or Telegram failed.

## Limits

- It never places an order. Every buy / partial / exit is you, by hand.
- It only tracks positions it opened. Manual trades are invisible.
- Holidays are inferred from the feed, not a calendar. A lagging feed looks like a holiday — inside the entry window it now retries instead of giving up.
- Silence outside 09:15–15:20 IST weekdays is normal. Run `selftest` to prove it’s alive.

This documents what the code does. It is not trading advice.
