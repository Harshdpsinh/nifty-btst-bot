"""Dry-run order intents. Phase 2 — NEVER talks to Angel One placeOrder.

BTST_LIVE_ORDERS must be the string "1" to even *consider* live orders.
Live placement is Phase 3 and is not implemented: LIVE=1 still sends no
order, Telegram gets a warning, and the intent is recorded as DRY_RUN.

Env (all optional, defaults are safe):
  BTST_LIVE_ORDERS=0
  BTST_LOTS=1
  BTST_LOT_SIZE=25          # NFO qty = lots * lot_size; confirm on the contract
  BTST_MAX_PREMIUM=150      # skip auto-BUY if quoted premium exceeds this
  BTST_LIMIT_SLIPPAGE_PTS=5
  BTST_DAILY_MAX_ORDERS=4
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("btst.execution")

Notify = Callable[[str], bool]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def live_orders_enabled() -> bool:
    """True only for the exact string '1'. 'true' / 'yes' / empty = off."""
    return os.getenv("BTST_LIVE_ORDERS", "0").strip() == "1"


def configured_lots(position: dict | None = None) -> int:
    if position and position.get("lots"):
        try:
            return max(1, int(position["lots"]))
        except (TypeError, ValueError):
            pass
    return max(1, _env_int("BTST_LOTS", 1))


def lot_size() -> int:
    return max(1, _env_int("BTST_LOT_SIZE", 25))


def max_premium() -> float:
    return _env_float("BTST_MAX_PREMIUM", 150.0)


def slippage_pts() -> float:
    return max(0.05, _env_float("BTST_LIMIT_SLIPPAGE_PTS", 5.0))


def daily_max_orders() -> int:
    return max(1, _env_int("BTST_DAILY_MAX_ORDERS", 4))


def partial_sell_lots(total_lots: int) -> int | None:
    """50% as whole lots. None if a 1-lot position cannot be halved."""
    total_lots = int(total_lots)
    if total_lots < 2:
        return None
    return max(1, total_lots // 2)


def limit_price(transaction: str, ltp: float, slip: float | None = None,
                cap: float | None = None) -> float | None:
    """LIMIT only (NSE algo rule: no MARKET / IOC). None = abort the BUY."""
    slip = slippage_pts() if slip is None else slip
    cap = max_premium() if cap is None else cap
    ltp = float(ltp)
    if transaction == "BUY":
        px = ltp + slip
        if cap > 0 and px > cap:
            return None
        return round(px, 2)
    return round(max(ltp - slip, 0.05), 2)


@dataclass
class OrderIntent:
    action_id: str
    transaction: str          # BUY / SELL / SKIP
    reason: str               # entry / partial_2x / ha_exit / cutoff_1513 / leftover
    tradingsymbol: str
    symbol_token: str
    lots: int
    quantity: int
    price: float
    product: str = "NRML"
    variety: str = "NORMAL"
    ordertype: str = "LIMIT"
    exchange: str = "NFO"
    skip_reason: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "transaction": self.transaction,
            "reason": self.reason,
            "tradingsymbol": self.tradingsymbol,
            "symbol_token": self.symbol_token,
            "lots": self.lots,
            "quantity": self.quantity,
            "price": self.price,
            "product": self.product,
            "ordertype": self.ordertype,
            "exchange": self.exchange,
            "skip_reason": self.skip_reason,
        }


def place_order(_intent: OrderIntent) -> None:
    """Phase 3 stub. Must never be called in Phase 2."""
    raise RuntimeError(
        "Phase 3 live placeOrder is not implemented. No order was sent. "
        "Set BTST_LIVE_ORDERS=0 (the default)."
    )


def already_submitted(state: dict, action_id: str) -> bool:
    for row in state.get("submitted_actions") or []:
        if row.get("action_id") == action_id:
            return True
    return False


def record_action(state: dict, intent: OrderIntent, status: str = "DRY_RUN") -> None:
    rows = list(state.get("submitted_actions") or [])
    rows.append({
        **intent.as_dict(),
        "status": status,
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
    })
    state["submitted_actions"] = rows[-100:]


def _orders_today(state: dict, day: str) -> int:
    n = 0
    for row in state.get("submitted_actions") or []:
        if str(row.get("action_id", "")).find(day) >= 0 and row.get("transaction") != "SKIP":
            n += 1
    return n


def format_intent(intent: OrderIntent, live_refused: bool = False) -> str:
    head = "[DRY-RUN] ORDER INTENT — no order sent (BTST_LIVE_ORDERS is not 1)"
    if live_refused:
        head = (
            "⚠️ BTST_LIVE_ORDERS=1 but live placeOrder is NOT implemented.\n"
            "No order was sent. Treating as dry-run. Set BTST_LIVE_ORDERS=0."
        )
    if intent.transaction == "SKIP" or intent.skip_reason:
        return (
            f"{head}\n"
            f"Action: {intent.action_id}\n"
            f"SKIPPED {intent.reason}: {intent.skip_reason or 'see reason'}\n"
            f"You still click by hand if the signal above still applies."
        )
    return (
        f"{head}\n"
        f"Action: {intent.action_id}\n"
        f"{intent.transaction} {intent.lots} lot(s)  qty={intent.quantity}  "
        f"{intent.tradingsymbol or '(unresolved)'}\n"
        f"Exchange {intent.exchange}  {intent.product}  {intent.ordertype}  "
        f"@ {intent.price:.2f}\n"
        f"Reason: {intent.reason}\n"
        f"Token: {intent.symbol_token or 'n/a'}\n"
        f"You still click this by hand. Phase 3 (live) is not enabled."
    )


def make_buy_intent(side: str, day: str, contract: dict | None) -> OrderIntent:
    lots = configured_lots()
    qty = lots * lot_size()
    ts = (contract or {}).get("tradingsymbol") or f"NIFTY-{side}"
    token = str((contract or {}).get("symbol_token") or "")
    premium = float((contract or {}).get("premium") or 0.0)
    action_id = f"entry:{day}:{side}"
    if not token or premium <= 0:
        return OrderIntent(
            action_id=action_id, transaction="SKIP", reason="entry",
            tradingsymbol=ts, symbol_token=token, lots=lots, quantity=qty,
            price=0.0, skip_reason="no live contract/token — would not auto-send",
        )
    px = limit_price("BUY", premium)
    if px is None:
        return OrderIntent(
            action_id=action_id, transaction="SKIP", reason="entry",
            tradingsymbol=ts, symbol_token=token, lots=lots, quantity=qty,
            price=premium, skip_reason=(
                f"limit {premium + slippage_pts():.2f} exceeds "
                f"BTST_MAX_PREMIUM {max_premium():.0f}"
            ),
        )
    return OrderIntent(
        action_id=action_id, transaction="BUY", reason="entry",
        tradingsymbol=ts, symbol_token=token, lots=lots, quantity=qty, price=px,
    )


def make_sell_intent(position: dict, reason: str, lots: int | None = None,
                     ltp: float | None = None, day: str | None = None) -> OrderIntent:
    side = position.get("side") or "?"
    opened = str(position.get("opened_date") or "unknown")
    day = day or opened
    action_id = f"exit-{reason}:{opened}:{side}:{day}"
    total = configured_lots(position)
    sell_lots = total if lots is None else lots
    remaining = int(position.get("lots_remaining") or total)
    sell_lots = max(0, min(sell_lots, remaining))
    ts = position.get("tradingsymbol") or f"NIFTY-{side}"
    token = str(position.get("symbol_token") or "")
    premium = float(ltp if ltp is not None else position.get("entry_premium") or 0.0)
    px = limit_price("SELL", premium) if premium > 0 else 0.05
    skip = ""
    txn = "SELL"
    if sell_lots <= 0:
        txn, skip = "SKIP", "nothing left to sell"
    elif not token:
        skip = "no symbol_token on position — would not auto-send"
        txn = "SKIP"
    return OrderIntent(
        action_id=action_id, transaction=txn, reason=reason,
        tradingsymbol=ts, symbol_token=token, lots=sell_lots,
        quantity=sell_lots * lot_size(), price=px or 0.05, skip_reason=skip,
    )


def make_partial_intent(position: dict, ltp: float, day: str) -> OrderIntent:
    total = configured_lots(position)
    sell = partial_sell_lots(int(position.get("lots_remaining") or total))
    opened = str(position.get("opened_date") or "unknown")
    side = position.get("side") or "?"
    action_id = f"partial:{opened}:{side}"
    if sell is None:
        return OrderIntent(
            action_id=action_id, transaction="SKIP", reason="partial_2x",
            tradingsymbol=position.get("tradingsymbol") or f"NIFTY-{side}",
            symbol_token=str(position.get("symbol_token") or ""),
            lots=total, quantity=total * lot_size(), price=float(ltp),
            skip_reason=(
                f"BTST_LOTS={total} — 50% is not a whole lot. "
                "No partial. HA exit still watches all lots."
            ),
        )
    intent = make_sell_intent(position, reason="partial_2x", lots=sell, ltp=ltp, day=day)
    intent.action_id = action_id
    return intent


def submit(state: dict, intent: OrderIntent, notify: Notify,
           extra_message: str) -> bool:
    """Telegram extra_message + dry-run block. Record action_id on delivery.

    Never calls place_order. Returns notify success. Duplicate action_id
    sends extra_message only (no second dry-run block).
    """
    live_refused = live_orders_enabled()
    if live_refused:
        log.error("BTST_LIVE_ORDERS=1 but Phase 3 placeOrder is not implemented — dry-run only.")

    if already_submitted(state, intent.action_id):
        log.info("action %s already recorded — not repeating dry-run block.", intent.action_id)
        return notify(extra_message)

    day = dt.date.today().isoformat()
    if intent.transaction != "SKIP" and _orders_today(state, day) >= daily_max_orders():
        intent = OrderIntent(
            action_id=intent.action_id + ":capped",
            transaction="SKIP", reason=intent.reason,
            tradingsymbol=intent.tradingsymbol, symbol_token=intent.symbol_token,
            lots=intent.lots, quantity=intent.quantity, price=intent.price,
            skip_reason=f"BTST_DAILY_MAX_ORDERS={daily_max_orders()} reached",
        )

    text = extra_message + "\n\n" + format_intent(intent, live_refused=live_refused)
    if not notify(text):
        return False
    record_action(state, intent, status="DRY_RUN_REFUSED_LIVE" if live_refused else "DRY_RUN")
    log.info("dry-run recorded %s %s %s lots=%s",
             intent.transaction, intent.reason, intent.action_id, intent.lots)
    return True


def status_lines() -> str:
    return (
        f"• Dry-run execution: LIVE={os.getenv('BTST_LIVE_ORDERS', '0')!r} "
        f"(enabled={live_orders_enabled()}; Phase 3 placeOrder=NOT IMPLEMENTED)\n"
        f"• Lots: {configured_lots()} × lot_size {lot_size()} = qty {configured_lots() * lot_size()}\n"
        f"• Max premium / slippage: {max_premium():.0f} / {slippage_pts():.2f} pts"
    )
