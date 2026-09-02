#!/usr/bin/env python3
"""Phase 1 backtest + Phase 2 dry-run guards. Offline, no network, no orders."""

from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

os.environ.setdefault("ANGELONE_API_KEY", "test")
os.environ.setdefault("ANGELONE_CLIENT_ID", "test")
os.environ.setdefault("ANGELONE_PASSWORD", "test")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ["DATA_PROVIDER"] = "angelone"
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)
os.environ["BTST_LIVE_ORDERS"] = "0"
os.environ["BTST_LOTS"] = "1"
os.environ["BTST_LOT_SIZE"] = "25"
os.environ["BTST_MAX_PREMIUM"] = "150"

import pandas as pd
import pytz

import backtest_btst as bt
import btst_engine as engine
import execution


IST = pytz.timezone("Asia/Kolkata")


def _ts(h, m, day=dt.date(2026, 8, 27)):
    return IST.localize(dt.datetime(day.year, day.month, day.day, h, m))


class DivergenceTests(unittest.TestCase):
    def test_matches_playbook_formula(self):
        # spot 24530, OHL 24500/24550/24490 → (sum + spot)/4 = 24517.5, div +12.5
        self.assertAlmostEqual(
            engine.daily_divergence(24530, 24500, 24550, 24490), 12.5
        )

    def test_negative_divergence_is_put_side(self):
        div = engine.daily_divergence(24400, 24500, 24520, 24380)
        self.assertLess(div, -11)
        self.assertEqual(bt.entry_side(div), "PE")


class LiveFlagTests(unittest.TestCase):
    def test_only_string_one_enables(self):
        with mock.patch.dict(os.environ, {"BTST_LIVE_ORDERS": "0"}):
            self.assertFalse(execution.live_orders_enabled())
        with mock.patch.dict(os.environ, {"BTST_LIVE_ORDERS": ""}):
            self.assertFalse(execution.live_orders_enabled())
        with mock.patch.dict(os.environ, {"BTST_LIVE_ORDERS": "true"}):
            self.assertFalse(execution.live_orders_enabled())
        with mock.patch.dict(os.environ, {"BTST_LIVE_ORDERS": "1"}):
            self.assertTrue(execution.live_orders_enabled())

    def test_place_order_always_raises(self):
        intent = execution.OrderIntent(
            action_id="x", transaction="BUY", reason="entry",
            tradingsymbol="NIFTY", symbol_token="1", lots=1, quantity=25, price=100,
        )
        with self.assertRaises(RuntimeError):
            execution.place_order(intent)


class PartialLotTests(unittest.TestCase):
    def test_one_lot_cannot_halve(self):
        self.assertIsNone(execution.partial_sell_lots(1))

    def test_two_lots_sells_one(self):
        self.assertEqual(execution.partial_sell_lots(2), 1)

    def test_three_lots_floors(self):
        self.assertEqual(execution.partial_sell_lots(3), 1)


class LimitPriceTests(unittest.TestCase):
    def test_buy_aborts_over_cap(self):
        self.assertIsNone(execution.limit_price("BUY", 148.0, slip=5, cap=150))

    def test_buy_ok_under_cap(self):
        self.assertEqual(execution.limit_price("BUY", 100.0, slip=5, cap=150), 105.0)

    def test_sell_never_market(self):
        px = execution.limit_price("SELL", 100.0, slip=5, cap=150)
        self.assertEqual(px, 95.0)


class SubmitDryRunTests(unittest.TestCase):
    def test_records_and_never_places(self):
        state = {}
        sent = []
        intent = execution.make_buy_intent(
            "CE", "2026-09-02",
            {"tradingsymbol": "NIFTY02SEP2624500CE", "symbol_token": "99", "premium": 101.0},
        )
        with mock.patch.object(execution, "place_order", side_effect=AssertionError("no place")):
            ok = execution.submit(state, intent, lambda m: sent.append(m) or True, "🚨 SIGNAL")
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        self.assertIn("[DRY-RUN]", sent[0])
        self.assertIn("BUY", sent[0])
        self.assertEqual(state["submitted_actions"][0]["status"], "DRY_RUN")
        self.assertEqual(state["submitted_actions"][0]["action_id"], "entry:2026-09-02:CE")

    def test_duplicate_action_id_does_not_re_record(self):
        state = {}
        sent = []
        intent = execution.make_buy_intent(
            "CE", "2026-09-02",
            {"tradingsymbol": "X", "symbol_token": "1", "premium": 100.0},
        )
        notify = lambda m: sent.append(m) or True
        execution.submit(state, intent, notify, "first")
        execution.submit(state, intent, notify, "second")
        self.assertEqual(len(state["submitted_actions"]), 1)
        self.assertEqual(sent[1], "second")  # no second dry-run block

    def test_live_flag_still_does_not_place(self):
        state = {}
        sent = []
        intent = execution.make_buy_intent(
            "PE", "2026-09-02",
            {"tradingsymbol": "Y", "symbol_token": "2", "premium": 90.0},
        )
        with mock.patch.dict(os.environ, {"BTST_LIVE_ORDERS": "1"}):
            with mock.patch.object(execution, "place_order", side_effect=AssertionError("no place")):
                ok = execution.submit(state, intent, lambda m: sent.append(m) or True, "🚨")
        self.assertTrue(ok)
        self.assertIn("NOT implemented", sent[0])
        self.assertTrue(state["submitted_actions"][0]["status"].startswith("DRY_RUN"))

    def test_premium_cap_skips_buy(self):
        intent = execution.make_buy_intent(
            "CE", "2026-09-02",
            {"tradingsymbol": "Z", "symbol_token": "3", "premium": 200.0},
        )
        self.assertEqual(intent.transaction, "SKIP")
        self.assertIn("BTST_MAX_PREMIUM", intent.skip_reason)

    def test_partial_skip_on_one_lot(self):
        pos = {
            "side": "CE", "opened_date": "2026-09-01",
            "tradingsymbol": "NIFTYCE", "symbol_token": "5",
            "entry_premium": 100.0, "lots": 1, "lots_remaining": 1,
        }
        intent = execution.make_partial_intent(pos, 210.0, "2026-09-02")
        self.assertEqual(intent.transaction, "SKIP")
        self.assertIn("whole lot", intent.skip_reason)


class WalkExitTests(unittest.TestCase):
    def _day(self, rows):
        idx = [r[0] for r in rows]
        data = {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "HA_Open": [r[5] for r in rows],
            "HA_Close": [r[6] for r in rows],
            "HA_High": [r[7] for r in rows],
            "HA_Low": [r[8] for r in rows],
            "Is_Red": [r[9] for r in rows],
            "Is_Green": [not r[9] for r in rows],
        }
        return pd.DataFrame(data, index=pd.DatetimeIndex(idx))

    def test_ce_exits_on_second_bar_after_red_arms(self):
        df = self._day([
            (_ts(9, 15), 100, 101, 90, 91, 100, 95, 101, 90, True),
            (_ts(9, 45), 91, 92, 80, 82, 97, 86, 97, 80, True),
        ])
        reason, when, px = bt.walk_exits("CE", df)
        self.assertEqual(reason, "ha_ce")
        self.assertEqual(when.hour, 9)
        self.assertEqual(when.minute, 45)
        self.assertEqual(px, 82)

    def test_same_first_bar_does_not_exit_against_empty_refs(self):
        df = self._day([
            (_ts(9, 15), 100, 101, 90, 91, 100, 95, 101, 90, True),
            (_ts(9, 45), 91, 100, 91, 99, 97, 98, 100, 91, False),  # green, no CE break
            (_ts(14, 45), 99, 100, 98, 99, 98, 99, 100, 98, False),
        ])
        reason, when, px = bt.walk_exits("CE", df)
        self.assertEqual(reason, "cutoff_1513")
        self.assertEqual(when.hour, 15)
        self.assertEqual(when.minute, 13)
        self.assertEqual(px, 99)

    def test_pe_exits_on_green_high_break(self):
        df = self._day([
            (_ts(9, 15), 100, 110, 99, 108, 100, 105, 110, 99, False),
            (_ts(9, 45), 108, 125, 107, 120, 103, 115, 125, 107, False),
        ])
        reason, when, px = bt.walk_exits("PE", df)
        self.assertEqual(reason, "ha_pe")
        self.assertEqual(px, 120)


class FixtureBacktestTests(unittest.TestCase):
    def test_synthetic_ce_ha_exit_next_session(self):
        daily, intra = bt.synthetic_fixture()
        result = bt.run_backtest(daily, intra)
        self.assertGreaterEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertEqual(t.side, "CE")
        self.assertEqual(t.entry_date, "2026-08-26")
        self.assertEqual(t.exit_date, "2026-08-27")
        self.assertNotEqual(t.entry_date, t.exit_date)
        self.assertEqual(t.exit_reason, "ha_ce")

    def test_flat_day_is_not_a_trade(self):
        daily, intra = bt.synthetic_fixture()
        # flatten Wednesday
        daily.iloc[0, daily.columns.get_loc("Close")] = daily.iloc[0]["Open"]
        daily.iloc[0, daily.columns.get_loc("High")] = daily.iloc[0]["Open"] + 1
        daily.iloc[0, daily.columns.get_loc("Low")] = daily.iloc[0]["Open"] - 1
        result = bt.run_backtest(daily, intra)
        self.assertTrue(all(t.entry_date != "2026-08-26" for t in result.trades))

    def test_mon_tue_roll_used_for_expiry_tag(self):
        mon = dt.date(2026, 8, 24)
        d, _ = engine._next_option_expiry(mon)
        self.assertEqual(d, dt.date(2026, 9, 1))


class EntryScanDryRunTests(unittest.TestCase):
    def test_entry_records_dry_run_and_position(self):
        state = {}
        fake_now = IST.localize(dt.datetime(2026, 9, 2, 15, 22))  # Wednesday
        contract = {
            "tradingsymbol": "NIFTY08SEP2624500CE",
            "symbol_token": "111",
            "premium": 102.0,
            "expiry_date": "2026-09-08",
        }
        sent = []
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "get_live_daily_data", return_value=(24530.0, 24512.5)
        ), mock.patch.object(engine, "_resolve_contract", return_value=contract), mock.patch.object(
            engine, "send_telegram", side_effect=lambda m: sent.append(m) or True
        ), mock.patch.object(execution, "place_order", side_effect=AssertionError("no place")):
            engine.run_entry_scan(state)
        self.assertIsNotNone(state.get("position"))
        self.assertEqual(state["position"]["side"], "CE")
        self.assertEqual(state["position"]["lots"], 1)
        self.assertTrue(any("[DRY-RUN]" in m for m in sent))
        self.assertTrue(any(a["reason"] == "entry" for a in state["submitted_actions"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
