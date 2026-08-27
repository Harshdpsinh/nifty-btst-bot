#!/usr/bin/env python3
"""Offline checks for the playbook guards (no network, no Telegram)."""

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

import pandas as pd
import pytz

import btst_engine as engine
import providers


IST = pytz.timezone("Asia/Kolkata")


def _ts(h, m, day=dt.date(2026, 8, 27)):
    return IST.localize(dt.datetime(day.year, day.month, day.day, h, m))


class BucketAndSplitTests(unittest.TestCase):
    def test_bucket_aligns_to_15_45(self):
        self.assertEqual(engine.nse_30m_bucket_start(_ts(10, 7)).minute, 45)
        self.assertEqual(engine.nse_30m_bucket_start(_ts(10, 7)).hour, 9)
        self.assertEqual(engine.nse_30m_bucket_start(_ts(10, 20)).minute, 15)
        self.assertEqual(engine.nse_30m_bucket_start(_ts(10, 20)).hour, 10)
        self.assertEqual(engine.nse_30m_bucket_start(_ts(9, 45)).minute, 45)

    def test_split_arms_0915_at_0945_even_if_api_omits_forming_bar(self):
        """Playbook: 09:45 first candle has closed, level is armed."""
        idx = pd.DatetimeIndex([_ts(9, 15)])
        df = pd.DataFrame(
            {
                "Open": [24100], "High": [24150], "Low": [24080], "Close": [24120],
                "HA_Open": [24110], "HA_Close": [24112], "HA_High": [24150], "HA_Low": [24080],
                "Is_Red": [True], "Is_Green": [False],
            },
            index=idx,
        )
        closed, forming = engine.split_closed_and_forming(df, _ts(9, 50))
        self.assertEqual(len(closed), 1)
        self.assertIsNone(forming)
        red, green = engine.sticky_refs(closed)
        self.assertIsNotNone(red)
        self.assertAlmostEqual(float(red["HA_Low"]), 24080)

    def test_split_uses_forming_bar_when_api_includes_it(self):
        idx = pd.DatetimeIndex([_ts(9, 15), _ts(9, 45)])
        df = pd.DataFrame(
            {
                "Open": [24100, 24120],
                "High": [24150, 24140],
                "Low": [24080, 24110],
                "Close": [24120, 24130],
                "HA_Open": [24110, 24111],
                "HA_Close": [24112, 24125],
                "HA_High": [24150, 24140],
                "HA_Low": [24080, 24110],
                "Is_Red": [True, False],
                "Is_Green": [False, True],
            },
            index=idx,
        )
        closed, forming = engine.split_closed_and_forming(df, _ts(9, 50))
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(forming)
        self.assertEqual(engine.ist_minute(forming.name), engine.ist_minute(_ts(9, 45)))


class PositionGuardTests(unittest.TestCase):
    def test_same_day_is_overnight_hold(self):
        pos = {"opened_date": "2026-08-27", "side": "CE"}
        self.assertTrue(engine.is_same_day_position(pos, dt.date(2026, 8, 27)))
        self.assertFalse(engine.is_leftover_position(pos, dt.date(2026, 8, 27)))

    def test_next_session_is_exit_day_not_leftover(self):
        pos = {"opened_date": "2026-08-26", "side": "PE"}  # Wed entry, Thu exit
        self.assertFalse(engine.is_same_day_position(pos, dt.date(2026, 8, 27)))
        self.assertFalse(engine.is_leftover_position(pos, dt.date(2026, 8, 27)))
        engine.mark_exit_session(pos, dt.date(2026, 8, 27))
        self.assertEqual(pos["exit_session_date"], "2026-08-27")
        self.assertFalse(engine.is_leftover_position(pos, dt.date(2026, 8, 27)))

    def test_second_session_is_leftover(self):
        pos = {
            "opened_date": "2026-08-26",
            "exit_session_date": "2026-08-27",
            "side": "PE",
        }
        self.assertTrue(engine.is_leftover_position(pos, dt.date(2026, 8, 28)))

    def test_friday_to_monday_is_one_exit_session(self):
        pos = {"opened_date": "2026-08-21", "side": "CE"}  # Friday
        monday = dt.date(2026, 8, 24)
        self.assertFalse(engine.is_leftover_position(pos, monday))
        engine.mark_exit_session(pos, monday)
        self.assertTrue(engine.is_leftover_position(pos, dt.date(2026, 8, 25)))


class HeartbeatTests(unittest.TestCase):
    def test_fresh_and_stale(self):
        now = _ts(10, 0)
        fresh = {"watcher_heartbeat": (now - dt.timedelta(seconds=30)).isoformat()}
        stale = {"watcher_heartbeat": (now - dt.timedelta(seconds=200)).isoformat()}
        missing = {}
        self.assertTrue(engine.watcher_heartbeat_fresh(fresh, now))
        self.assertFalse(engine.watcher_heartbeat_fresh(stale, now))
        self.assertFalse(engine.watcher_heartbeat_fresh(missing, now))


class ProviderGuardTests(unittest.TestCase):
    def test_yahoo_rejected(self):
        with self.assertRaises(providers.ProviderError) as ctx:
            providers.get_provider("yahoo")
        self.assertIn("removed", str(ctx.exception).lower())

    def test_auth_error_detects_token_not_rate_limit(self):
        self.assertTrue(providers._is_auth_error_message("Invalid Token"))
        self.assertTrue(providers._is_auth_error_message("jwt expired"))
        self.assertFalse(providers._is_auth_error_message("Tokens max limit exceeded"))
        self.assertFalse(providers._is_auth_error_message("403 Forbidden"))
        self.assertFalse(providers._is_auth_error_message("rate limit"))

    def test_expiry_walkback_includes_monday_before_tuesday(self):
        tuesday = dt.date(2026, 9, 1)  # Tuesday
        dates = providers.AngelOneProvider._expiry_search_dates(tuesday)
        self.assertEqual(dates[0], tuesday)
        self.assertIn(dt.date(2026, 8, 31), dates)  # Monday

    def test_ltp_zero_would_be_dropped(self):
        quotes = {"1": 0.0, "2": 102.0, "3": 88.0}
        contracts = [
            {"tradingsymbol": "A", "symbol_token": "1"},
            {"tradingsymbol": "B", "symbol_token": "2"},
            {"tradingsymbol": "C", "symbol_token": "3"},
        ]
        priced = [
            (c, quotes[c["symbol_token"]])
            for c in contracts
            if c["symbol_token"] in quotes and quotes[c["symbol_token"]] > 0
        ]
        tokens = {c["symbol_token"] for c, _ in priced}
        self.assertNotIn("1", tokens)
        best, ltp = min(priced, key=lambda cp: abs(cp[1] - 100.0))
        self.assertEqual(best["symbol_token"], "2")


class TelegramGatingTests(unittest.TestCase):
    def test_exit_scan_does_not_clear_on_failed_send(self):
        state = {
            "position": {
                "side": "CE",
                "opened_date": "2026-08-26",
                "opened_at": "yesterday",
                "exit_session_date": "2026-08-26",
            }
        }
        fake_now = IST.localize(dt.datetime(2026, 8, 27, 10, 0))
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "send_telegram", return_value=False
        ), mock.patch.object(engine, "calculate_30m_heikin_ashi_for_day", side_effect=AssertionError("should not fetch")):
            engine.run_exit_scan(state)
        self.assertIsNotNone(state["position"])

    def test_stale_entry_inside_window_does_not_mark_day_done(self):
        state = {}
        fake_now = IST.localize(dt.datetime(2026, 8, 27, 15, 22))
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "get_live_daily_data", side_effect=engine.StaleDataError("lag")
        ), mock.patch.object(engine, "send_telegram", return_value=True):
            engine.run_entry_scan(state)
        self.assertIsNone(state.get("entry_scan_date"))

    def test_stale_entry_after_window_marks_day_done_if_delivered(self):
        state = {}
        fake_now = IST.localize(dt.datetime(2026, 8, 27, 15, 40))
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "get_live_daily_data", side_effect=engine.StaleDataError("holiday")
        ), mock.patch.object(engine, "send_telegram", return_value=True):
            engine.run_entry_scan(state)
        self.assertEqual(state.get("entry_scan_date"), "2026-08-27")


class ExpiryRuleTests(unittest.TestCase):
    def test_mon_tue_roll_forward(self):
        mon = dt.date(2026, 8, 24)
        tue = dt.date(2026, 8, 25)
        wed = dt.date(2026, 8, 26)
        d_mon, _ = engine._next_option_expiry(mon)
        d_tue, _ = engine._next_option_expiry(tue)
        d_wed, _ = engine._next_option_expiry(wed)
        self.assertEqual(d_mon, dt.date(2026, 9, 1))
        self.assertEqual(d_tue, dt.date(2026, 9, 1))
        self.assertEqual(d_wed, dt.date(2026, 9, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
