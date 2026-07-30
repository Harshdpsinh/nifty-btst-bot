"""Market data providers behind a single interface.

Switching data sources is meant to be a one-line config change, not a code
rewrite: set the DATA_PROVIDER env var (or the DATA_PROVIDER repository
variable in GitHub Actions settings — no push required) to one of the keys
in _PROVIDERS below.

    DATA_PROVIDER=yahoo      (default; free, no account, ~15 min delayed)
    DATA_PROVIDER=angelone   (free with an Angel One account, official
                              real-time NSE data — stub below, needs API
                              credentials filled in before use)

Every provider returns the same shape: a pandas DataFrame with columns
Open/High/Low/Close, indexed by tz-aware IST timestamps, newest bar last
(the newest bar may still be "forming" — that's the live price the entry
scan relies on). All Heikin-Ashi math, staleness checks and divergence
calculations live in btst_engine.py and are identical regardless of which
provider produced the bars.
"""

from __future__ import annotations

import abc
import logging
import os
import time

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")
log = logging.getLogger("btst.providers")


class ProviderError(RuntimeError):
    """A provider failed to return usable data."""


class MarketDataProvider(abc.ABC):
    """One live index feed. Implementations must be free of strategy logic."""

    name: str = "unnamed"

    @abc.abstractmethod
    def daily_bars(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        """OHLC daily bars including today's forming candle, IST-indexed."""

    @abc.abstractmethod
    def intraday_bars(self, symbol: str, interval_minutes: int, lookback_days: int) -> pd.DataFrame:
        """OHLC intraday bars, IST-indexed, newest bar last (may still be forming)."""


def _localize(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df


class YahooProvider(MarketDataProvider):
    """Free, no account required. Unofficial and roughly 15 min delayed for
    NSE data — fine for exploring the strategy, not for the live entry
    decision where the divergence threshold is only 11 points.
    """

    name = "yahoo"
    RETRIES = 3
    BACKOFF_SEC = 2

    def __init__(self) -> None:
        import yfinance as yf  # imported lazily so other providers don't need it
        self._yf = yf

    def _download(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        last_err: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                df = self._yf.download(
                    symbol, period=period, interval=interval,
                    progress=False, auto_adjust=False,
                )
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    return _localize(df)
                last_err = ProviderError("Yahoo Finance returned an empty frame")
            except Exception as e:  # network/parse errors from yfinance
                last_err = e
            log.warning("Yahoo download attempt %d/%d failed: %s",
                        attempt, self.RETRIES, last_err)
            if attempt < self.RETRIES:
                time.sleep(self.BACKOFF_SEC * attempt)
        raise ProviderError(f"Failed to fetch {symbol} {interval} from Yahoo: {last_err}")

    def daily_bars(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        return self._download(symbol, f"{lookback_days}d", "1d")

    def intraday_bars(self, symbol: str, interval_minutes: int, lookback_days: int) -> pd.DataFrame:
        return self._download(symbol, f"{lookback_days}d", f"{interval_minutes}m")


class AngelOneProvider(MarketDataProvider):
    """Free with an Angel One demat account. Official real-time NSE data via
    SmartAPI. NOT WIRED UP YET — this is a stub so the rest of the engine
    already has somewhere to plug it in.

    To activate:
      1. Enable the SmartAPI add-on on your Angel One account and generate
         an API key at https://smartapi.angelbroking.com.
      2. Provide these as env vars / GitHub Actions secrets:
         ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD (or PIN),
         ANGELONE_TOTP_SECRET (for the 2FA login SmartAPI requires).
      3. Fill in __init__ (login + session token) and the two data methods
         using SmartAPI's getCandleData endpoint, mapping its response into
         the same Open/High/Low/Close, IST-indexed DataFrame shape used by
         YahooProvider above. Everything else in btst_engine.py is already
         provider-agnostic and needs no changes.
    """

    name = "angelone"

    def __init__(self) -> None:
        raise NotImplementedError(
            "AngelOneProvider is a stub — see the class docstring for what's "
            "needed to activate it. Until then, use DATA_PROVIDER=yahoo."
        )

    def daily_bars(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        raise NotImplementedError

    def intraday_bars(self, symbol: str, interval_minutes: int, lookback_days: int) -> pd.DataFrame:
        raise NotImplementedError


_PROVIDERS: dict[str, type[MarketDataProvider]] = {
    "yahoo": YahooProvider,
    "angelone": AngelOneProvider,
}


def get_provider(name: str | None = None) -> MarketDataProvider:
    """Instantiate the configured provider. Defaults to the DATA_PROVIDER
    env var, falling back to 'yahoo' if unset.
    """
    key = (name or os.getenv("DATA_PROVIDER", "yahoo")).strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ProviderError(
            f"Unknown DATA_PROVIDER '{key}'. Available: {sorted(_PROVIDERS)}"
        )
    return cls()
