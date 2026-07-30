"""Market data providers behind a single interface.

Switching data sources is meant to be a one-line config change, not a code
rewrite: set the DATA_PROVIDER env var (or the DATA_PROVIDER repository
variable in GitHub Actions settings — no push required) to one of the keys
in _PROVIDERS below.

    DATA_PROVIDER=yahoo      (default; free, no account, ~15 min delayed)
    DATA_PROVIDER=angelone   (free with an Angel One account, official
                              real-time NSE data via SmartAPI)

Every provider returns the same shape: a pandas DataFrame with columns
Open/High/Low/Close, indexed by tz-aware IST timestamps, newest bar last
(the newest bar may still be "forming" — that's the live price the entry
scan relies on). All Heikin-Ashi math, staleness checks and divergence
calculations live in btst_engine.py and are identical regardless of which
provider produced the bars.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
import os
import time

import pandas as pd
import pytz
import requests

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
    SmartAPI (https://smartapi.angelbroking.com), using the getCandleData
    historical endpoint for both the daily and 30m candles.

    Required env vars / repository secrets:
      ANGELONE_API_KEY       — generated on the SmartAPI portal after
                                enabling the SmartAPI add-on on your account
      ANGELONE_CLIENT_ID     — your Angel One client code (login ID)
      ANGELONE_PASSWORD      — your account password/PIN
      ANGELONE_TOTP_SECRET   — the base32 TOTP secret shown when you set up
                                2FA for SmartAPI (NOT your regular OTP app
                                code — SmartAPI needs the secret itself so it
                                can generate its own code on every login)

    Optional overrides:
      ANGELONE_SYMBOL_TOKEN  — NSE instrument token for the NIFTY 50 index.
                                Defaults to "99926000" (the commonly
                                published token for exchange=NSE,
                                symbol="Nifty 50"). If prices ever look like
                                they're for the wrong instrument, look up the
                                correct token in Angel One's scrip master
                                (https://margincalculator.angelbroking.com/
                                OpenAPI_File/files/OpenAPIScripMaster.json —
                                filter exch_seg=="NSE" and symbol=="Nifty 50")
                                and set this instead of touching code.
      ANGELONE_BASE_URL      — defaults to https://apiconnect.angelone.in

    Important caveat: unlike YahooProvider, this has not been exercised
    against a real Angel One account (no test credentials were available
    when this was written) — only against the documented SmartAPI request/
    response shape and a mocked HTTP layer (see the offline test suite).
    Run `python btst_engine.py selftest` with DATA_PROVIDER=angelone before
    trusting it for a live trade. If SmartAPI's historical endpoint doesn't
    include the still-forming candle for your account/plan, the engine's
    existing staleness checks will raise StaleDataError and notify you
    rather than silently trading on stale data — they don't require any
    Angel-One-specific handling.
    """

    name = "angelone"

    EXCHANGE = "NSE"
    DEFAULT_SYMBOL_TOKEN = "99926000"  # NSE:"Nifty 50" — verify if data looks wrong
    RETRIES = 3
    BACKOFF_SEC = 2
    REQUEST_TIMEOUT = 15

    _INTERVAL_MAP = {
        1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
        10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE",
        60: "ONE_HOUR",
    }

    def __init__(self) -> None:
        import pyotp  # imported lazily so YahooProvider users don't need it
        self._pyotp = pyotp

        required = ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID",
                    "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET")
        missing = [v for v in required if not os.getenv(v)]
        if missing:
            raise ProviderError(
                "AngelOneProvider is missing: " + ", ".join(missing) + ". "
                "Generate an API key at https://smartapi.angelbroking.com "
                "(enable the SmartAPI add-on on your Angel One account first), "
                "then set these as env vars or GitHub Actions secrets."
            )

        self.base_url = os.getenv("ANGELONE_BASE_URL", "https://apiconnect.angelone.in")
        self.api_key = os.environ["ANGELONE_API_KEY"]
        self.client_id = os.environ["ANGELONE_CLIENT_ID"]
        self.password = os.environ["ANGELONE_PASSWORD"]
        self.totp_secret = os.environ["ANGELONE_TOTP_SECRET"]
        self.symbol_token = os.getenv("ANGELONE_SYMBOL_TOKEN", self.DEFAULT_SYMBOL_TOKEN)
        self._jwt_token: str | None = None

    def _headers(self, authed: bool) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
        }
        if authed:
            headers["Authorization"] = f"Bearer {self._jwt_token}"
        return headers

    def _login(self) -> None:
        totp = self._pyotp.TOTP(self.totp_secret).now()
        resp = requests.post(
            f"{self.base_url}/rest/auth/angelbroking/user/v1/loginByPassword",
            json={"clientcode": self.client_id, "password": self.password, "totp": totp},
            headers=self._headers(authed=False),
            timeout=self.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("status"):
            raise ProviderError(f"Angel One login failed: {body.get('message', body)}")
        self._jwt_token = body["data"]["jwtToken"]
        log.info("Angel One SmartAPI login OK.")

    def _get_candles(self, interval_code: str, from_date: dt.datetime,
                      to_date: dt.datetime) -> pd.DataFrame:
        payload = {
            "exchange": self.EXCHANGE,
            "symboltoken": self.symbol_token,
            "interval": interval_code,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        url = f"{self.base_url}/rest/secure/angelbroking/historical/v1/getCandleData"

        last_err: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                if self._jwt_token is None:
                    self._login()
                resp = requests.post(url, json=payload, headers=self._headers(authed=True),
                                      timeout=self.REQUEST_TIMEOUT)
                if resp.status_code == 401:
                    # Token expired or rejected — force a fresh login, retry once more.
                    self._jwt_token = None
                    self._login()
                    resp = requests.post(url, json=payload, headers=self._headers(authed=True),
                                          timeout=self.REQUEST_TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("status"):
                    raise ProviderError(f"Angel One getCandleData failed: {body.get('message', body)}")
                rows = body.get("data") or []
                if not rows:
                    raise ProviderError("Angel One returned no candles for the requested range")
                df = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
                df["Datetime"] = pd.to_datetime(df["Datetime"])
                df = df.set_index("Datetime")[["Open", "High", "Low", "Close"]].astype(float)
                return _localize(df)
            except Exception as e:
                last_err = e
            log.warning("Angel One candle fetch attempt %d/%d failed: %s",
                        attempt, self.RETRIES, last_err)
            if attempt < self.RETRIES:
                time.sleep(self.BACKOFF_SEC * attempt)
        raise ProviderError(f"Failed to fetch Angel One candles ({interval_code}): {last_err}")

    def daily_bars(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        now = dt.datetime.now(IST)
        from_date = now - dt.timedelta(days=lookback_days * 2)  # buffer for weekends/holidays
        return self._get_candles("ONE_DAY", from_date, now)

    def intraday_bars(self, symbol: str, interval_minutes: int, lookback_days: int) -> pd.DataFrame:
        interval_code = self._INTERVAL_MAP.get(interval_minutes)
        if interval_code is None:
            raise ProviderError(
                f"AngelOneProvider has no interval mapping for {interval_minutes}m "
                f"(supported: {sorted(self._INTERVAL_MAP)})"
            )
        now = dt.datetime.now(IST)
        from_date = now - dt.timedelta(days=lookback_days)
        return self._get_candles(interval_code, from_date, now)


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
