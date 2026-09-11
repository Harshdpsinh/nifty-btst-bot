#!/usr/bin/env python3
"""2× from 09:15 and 09:45 status without today's 30m bar. Offline."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import pathlib
import tempfile
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
os.environ["BTST_LOTS"] = "2"
os.environ["BTST_LOT_SIZE"] = "65"
os.environ["BTST_EXECUTION"] = os.environ.get("BTST_EXECUTION", "shadow")

import pandas as pd
import pytz

import btst_engine as engine
import watcher


IST = pytz.timezone("Asia/Kolkata")


def _ts(h, m, day=dt.date(2026, 8, 27)):
    return IST.localize(dt.datetime(day.year, day.month, day.day, h, m))


def _overnight_pos(**extra):
    pos = {
        "side": "PE",
        "opened_date": "2026-08-26",
        "tradingsymbol": "NIFTY15SEP2623400PE",
        "symbol_token": "123",
        "entry_premium": 91.55,
        "lots": 2,
        "lots_remaining": 2,
        "lot_size": 65,
        "partial_booked": False,
    }
    pos.update(extra)
    return pos


class OpenLagTests(unittest.TestCase):
    def _yesterday_bars(self):
        day = dt.date(2026, 8, 26)
        idx = pd.DatetimeIndex([_ts(14, 45, day), _ts(15, 15, day)])
        return pd.DataFrame(
            {
                "Open": [24000.0, 24100.0],
                "High": [24100.0, 24150.0],
                "Low": [23900.0, 24080.0],
                "Close": [24050.0, 24120.0],
            },
            index=idx,
        )

    def test_empty_today_before_10_returns_prev(self):
        fake_now = _ts(9, 46)
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine.PROVIDER, "intraday_bars", return_value=self._yesterday_bars()
        ):
            day_df, prev_row = engine.calculate_30m_heikin_ashi_day_and_prev(dt.date(2026, 8, 27))
        self.assertTrue(day_df.empty)
        self.assertIsNotNone(prev_row)

    def test_empty_today_after_10_raises(self):
        fake_now = _ts(10, 5)
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine.PROVIDER, "intraday_bars", return_value=self._yesterday_bars()
        ):
            with self.assertRaises(engine.StaleDataError):
                engine.calculate_30m_heikin_ashi_day_and_prev(dt.date(2026, 8, 27))


class PartialFromOpenTests(unittest.TestCase):
    def test_partial_at_0920_does_not_need_candles(self):
        pos = _overnight_pos()
        state = {"position": pos}
        fake_now = _ts(9, 20)
        sent = []
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine.PROVIDER, "get_option_ltp", return_value=186.45
        ), mock.patch.object(
            engine, "send_telegram", side_effect=lambda m: sent.append(m) or True
        ), mock.patch.object(
            engine, "calculate_30m_heikin_ashi_for_day",
            side_effect=AssertionError("HA too early"),
        ):
            engine.run_exit_scan(state)
        self.assertTrue(state["position"]["partial_booked"])
        self.assertTrue(any("PARTIAL PROFIT" in m for m in sent))
        self.assertTrue(any("09:15" in m for m in sent))
        self.assertEqual(state["position"]["exit_session_date"], "2026-08-27")

    def test_partial_not_before_open(self):
        pos = _overnight_pos()
        fake_now = _ts(9, 10)
        with mock.patch.object(engine.PROVIDER, "get_option_ltp", return_value=200.0):
            self.assertFalse(engine.handle_partial_profit({"position": pos}, pos, fake_now))
        self.assertFalse(pos["partial_booked"])

    def test_same_day_no_partial(self):
        pos = _overnight_pos(opened_date="2026-08-27")
        fake_now = _ts(9, 20)
        with mock.patch.object(engine.PROVIDER, "get_option_ltp", return_value=200.0):
            self.assertFalse(engine.handle_partial_profit({}, pos, fake_now))
        self.assertFalse(pos["partial_booked"])

    def test_below_2x_does_not_fire(self):
        pos = _overnight_pos()
        fake_now = _ts(9, 20)
        with mock.patch.object(engine.PROVIDER, "get_option_ltp", return_value=100.0):
            self.assertFalse(engine.handle_partial_profit({"position": pos}, pos, fake_now))
        self.assertFalse(pos["partial_booked"])


class WatcherOpenTickTests(unittest.TestCase):
    def test_ha_still_blocked_before_0945(self):
        pos = _overnight_pos()
        state = {"position": pos}
        fake_now = _ts(9, 20)
        health = watcher._HealthTracker()

        @contextlib.contextmanager
        def fake_lock():
            yield state

        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "locked_state", fake_lock
        ), mock.patch.object(
            engine.PROVIDER, "get_option_ltp", return_value=186.45
        ), mock.patch.object(
            engine, "send_telegram", return_value=True
        ), mock.patch.object(
            watcher, "bootstrap", side_effect=AssertionError("HA at 09:20")
        ):
            acc, _refs = watcher._run_one_tick(None, None, health)
        self.assertTrue(state["position"]["partial_booked"])
        self.assertIsNone(acc)
        self.assertIsNotNone(state.get("watcher_heartbeat"))

    def test_status_at_0945_without_today_bar(self):
        state = {"position": _overnight_pos(partial_booked=True)}
        fake_now = _ts(9, 45)
        health = watcher._HealthTracker()
        prev = pd.Series(
            {"HA_Open": 24100.0, "HA_Close": 24090.0, "Open": 24100.0, "Close": 24090.0}
        )
        empty = pd.DataFrame()

        @contextlib.contextmanager
        def fake_lock():
            yield state

        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "locked_state", fake_lock
        ), mock.patch.object(
            engine, "calculate_30m_heikin_ashi_day_and_prev",
            return_value=(empty, prev),
        ), mock.patch.object(
            engine.PROVIDER, "get_index_ltp", return_value=23250.0
        ), mock.patch.object(
            engine.PROVIDER, "get_option_ltp", return_value=186.45
        ), mock.patch.object(engine, "send_telegram", return_value=True) as tg:
            acc, refs = watcher._run_one_tick(None, None, health)
        self.assertIsNotNone(acc)
        self.assertIsNotNone(acc.live_ha())
        self.assertIsNone(refs["red"])
        self.assertTrue(state.get("watcher_last_status_bucket"))
        self.assertTrue(tg.called)


class CronFallbackTests(unittest.TestCase):
    def test_watcher_down_0920_runs_exit_scan(self):
        state = {
            "position": _overnight_pos(),
            "watcher_heartbeat": None,
        }
        fake_now = _ts(9, 20)
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "send_telegram", return_value=True
        ), mock.patch.object(engine, "run_exit_scan") as scan:
            engine.run_auto(state)
        scan.assert_called_once()

    def test_watcher_down_0920_flat_does_not_scan(self):
        state = {"position": None, "watcher_heartbeat": None}
        fake_now = _ts(9, 20)
        with mock.patch.object(engine, "_now", return_value=fake_now), mock.patch.object(
            engine, "send_telegram", return_value=True
        ), mock.patch.object(engine, "run_exit_scan") as scan:
            engine.run_auto(state)
        scan.assert_not_called()


class CorruptStateTests(unittest.TestCase):
    def test_unreadable_state_refuses_to_wipe(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "state.json"
            path.write_text("{not json")
            with mock.patch.object(engine, "STATE_PATH", path):
                with self.assertRaises(engine.CorruptStateError):
                    engine.load_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
