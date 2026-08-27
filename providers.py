"""Market data: Angel One SmartAPI only.

Yahoo was removed — a 15-minute delayed unofficial feed cannot be trusted
for an 11-point NIFTY divergence. DATA_PROVIDER is ignored except that
`yahoo` fails loudly so a leftover env var is obvious.

Required env vars (also GitHub Actions secrets / ~/.btst.env on the VM):
  ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD, ANGELONE_TOTP_SECRET

Optional:
  ANGELONE_SYMBOL_TOKEN  — NSE token for Nifty 50 (default 99926000)
  ANGELONE_BASE_URL      — default https://apiconnect.angelone.in
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


def _is_auth_error_message(message: str) -> bool:
    """True only for session/token failures — not for 403 rate limits, etc."""
    m = (message or "").lower()
    needles = (
        "invalid token",
        "token expired",
        "jwt",
        "unauthorized",
        "unauthorised",
        "session expired",
        "please login",
        "not logged in",
        "invalid session",
        "access denied",
    )
    return any(n in m for n in needles)


class AngelOneProvider(MarketDataProvider):
    """Official real-time NSE data via SmartAPI.

    Required env vars / repository secrets:
      ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD, ANGELONE_TOTP_SECRET

    Optional:
      ANGELONE_SYMBOL_TOKEN  — default \"99926000\" (NSE Nifty 50)
      ANGELONE_BASE_URL      — default https://apiconnect.angelone.in
    """

    name = "angelone"

    EXCHANGE = "NSE"
    DEFAULT_SYMBOL_TOKEN = "99926000"
    RETRIES = 3
    BACKOFF_SEC = 2
    REQUEST_TIMEOUT = 15

    _INTERVAL_MAP = {
        1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
        10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE",
        60: "ONE_HOUR",
    }

    def __init__(self) -> None:
        import pyotp  # imported lazily so tests can construct without the package if needed
        self._pyotp = pyotp

        required = ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID",
                    "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET")
        missing = [v for v in required if not os.getenv(v)]
        if missing:
            raise ProviderError(
                "AngelOneProvider is missing: " + ", ".join(missing) + ". "
                "Generate an API key at https://smartapi.angelbroking.com "
                "(enable the SmartAPI add-on on your Angel One account first), "
                "then set these as env vars or in ~/.btst.env on the VM."
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

    def _post(self, path: str, payload: dict) -> dict:
        """POST with retry, lazy login, and re-login only on real auth failures."""
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                if self._jwt_token is None:
                    self._login()
                resp = requests.post(url, json=payload, headers=self._headers(authed=True),
                                      timeout=self.REQUEST_TIMEOUT)
                if resp.status_code == 401:
                    self._jwt_token = None
                    self._login()
                    resp = requests.post(url, json=payload, headers=self._headers(authed=True),
                                          timeout=self.REQUEST_TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
                if not body.get("status"):
                    msg = str(body.get("message", body))
                    # Only drop the cached JWT on actual session death. A 403 /
                    # rate-limit / "status:false" on getCandleData used to clear
                    # the token and re-login with password+TOTP every retry,
                    # which turns a candle-quota problem into a login lockout.
                    if _is_auth_error_message(msg):
                        self._jwt_token = None
                    raise ProviderError(f"Angel One request to {path} failed: {msg}")
                return body
            except Exception as e:
                last_err = e
            log.warning("Angel One request attempt %d/%d failed (%s): %s",
                        attempt, self.RETRIES, path, last_err)
            if attempt < self.RETRIES:
                time.sleep(self.BACKOFF_SEC * attempt)
        raise ProviderError(f"Angel One request to {path} failed: {last_err}")

    def _get_candles(self, interval_code: str, from_date: dt.datetime,
                      to_date: dt.datetime) -> pd.DataFrame:
        payload = {
            "exchange": self.EXCHANGE,
            "symboltoken": self.symbol_token,
            "interval": interval_code,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        body = self._post("/rest/secure/angelbroking/historical/v1/getCandleData", payload)
        rows = body.get("data") or []
        if not rows:
            raise ProviderError("Angel One returned no candles for the requested range")
        df = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime")[["Open", "High", "Low", "Close"]].astype(float)
        return _localize(df)

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

    OPTIONS_EXCHANGE = "NFO"
    UNDERLYING = "NIFTY"
    MAX_QUOTE_TOKENS_PER_REQUEST = 50

    def search_option_chain(self, expiry: dt.date) -> list[dict]:
        """Every NFO option contract (both CE and PE, all strikes) for
        UNDERLYING's `expiry`. Angel One's NFO tradingsymbol format is
        <UNDERLYING><DDMMMYY><STRIKE><CE|PE>, e.g. \"NIFTY04AUG2624300CE\".
        """
        expiry_tag = expiry.strftime("%d%b%y").upper()  # e.g. "04AUG26"
        body = self._post(
            "/rest/secure/angelbroking/order/v1/searchScrip",
            {"exchange": self.OPTIONS_EXCHANGE, "searchscrip": f"{self.UNDERLYING}{expiry_tag}"},
        )
        matches = body.get("data") or []
        contracts = []
        for m in matches:
            ts = m.get("tradingsymbol") or m.get("tradingSymbol") or ""
            token = m.get("symboltoken") or m.get("symbolToken")
            if not ts or token is None:
                continue
            if expiry_tag not in ts or not (ts.endswith("CE") or ts.endswith("PE")):
                continue
            contracts.append({"tradingsymbol": ts, "symbol_token": str(token)})
        if not contracts:
            raise ProviderError(
                f"No {self.UNDERLYING} option contracts found for expiry {expiry} "
                f"(searched for '{self.UNDERLYING}{expiry_tag}')"
            )
        return contracts

    def get_quotes(self, tokens: list[str], exchange: str | None = None) -> dict[str, float]:
        """Batch LTP lookup -> {symbol_token: ltp}. Chunks large lists."""
        exchange = exchange or self.OPTIONS_EXCHANGE
        tokens = list(tokens)
        out: dict[str, float] = {}
        for i in range(0, len(tokens), self.MAX_QUOTE_TOKENS_PER_REQUEST):
            chunk = tokens[i:i + self.MAX_QUOTE_TOKENS_PER_REQUEST]
            body = self._post(
                "/rest/secure/angelbroking/market/v1/quote/",
                {"mode": "LTP", "exchangeTokens": {exchange: chunk}},
            )
            fetched = (body.get("data") or {}).get("fetched") or []
            for row in fetched:
                token = row.get("symbolToken") or row.get("symboltoken")
                ltp = row.get("ltp")
                if token is not None and ltp is not None:
                    out[str(token)] = float(ltp)
        return out

    @staticmethod
    def _expiry_search_dates(expiry: dt.date) -> list[dt.date]:
        """Tuesday expiry, then previous weekdays (holiday walk-back)."""
        out = [expiry]
        d = expiry
        for _ in range(4):
            d -= dt.timedelta(days=1)
            if d.weekday() < 5:
                out.append(d)
        return out

    def resolve_option_contract(self, side: str, expiry: dt.date, target_premium: float) -> dict:
        """Find the strike whose live premium is closest to `target_premium`
        for `side` (\"CE\"/\"PE\") at `expiry`. Walks back weekdays if that
        Tuesday is a holiday (NSE then expires the previous session).
        Ignores LTP <= 0 (untraded / stale prints).
        Returns {tradingsymbol, symbol_token, premium, expiry_date}.
        """
        side = side.upper()
        if side not in ("CE", "PE"):
            raise ProviderError(f"resolve_option_contract: side must be CE or PE, got {side!r}")

        last_err: Exception | None = None
        for candidate in self._expiry_search_dates(expiry):
            try:
                contracts = [
                    c for c in self.search_option_chain(candidate)
                    if c["tradingsymbol"].endswith(side)
                ]
            except ProviderError as e:
                last_err = e
                log.info("No chain for %s (%s) — trying previous session.", candidate, e)
                continue
            if not contracts:
                last_err = ProviderError(
                    f"No {self.UNDERLYING} {side} contracts found for expiry {candidate}"
                )
                continue

            quotes = self.get_quotes([c["symbol_token"] for c in contracts])
            priced = [
                (c, quotes[c["symbol_token"]])
                for c in contracts
                if c["symbol_token"] in quotes and quotes[c["symbol_token"]] > 0
            ]
            if not priced:
                last_err = ProviderError(
                    f"Angel One returned no live (LTP>0) quotes for any {self.UNDERLYING} {side} "
                    f"contract at expiry {candidate}"
                )
                continue

            best_contract, best_ltp = min(priced, key=lambda cp: abs(cp[1] - target_premium))
            result = {
                "tradingsymbol": best_contract["tradingsymbol"],
                "symbol_token": best_contract["symbol_token"],
                "premium": best_ltp,
                "expiry_date": candidate.isoformat(),
            }
            if candidate != expiry:
                log.warning("Expiry %s has no chain; using %s (holiday walk-back).",
                            expiry, candidate)
            return result

        raise last_err or ProviderError(
            f"Could not resolve a {side} contract near {target_premium} for expiry {expiry}"
        )

    def get_index_ltp(self) -> float:
        quotes = self.get_quotes([self.symbol_token], exchange=self.EXCHANGE)
        if self.symbol_token not in quotes:
            raise ProviderError("Angel One quote response did not include the NIFTY index LTP")
        return quotes[self.symbol_token]

    def get_option_ltp(self, symbol_token: str) -> float:
        quotes = self.get_quotes([symbol_token])
        if symbol_token not in quotes:
            raise ProviderError(f"Angel One quote response did not include LTP for token {symbol_token}")
        return quotes[symbol_token]


def get_provider(name: str | None = None) -> MarketDataProvider:
    """Instantiate Angel One. `yahoo` is rejected so a leftover env var fails loud."""
    key = (name or os.getenv("DATA_PROVIDER", "angelone")).strip().lower()
    if key in ("", "angelone"):
        return AngelOneProvider()
    if key == "yahoo":
        raise ProviderError(
            "The Yahoo Finance provider was removed. An unofficial ~15 min delayed "
            "feed cannot drive an 11-point NIFTY entry. Set DATA_PROVIDER=angelone "
            "in ~/.btst.env (and restart btst-watcher)."
        )
    raise ProviderError(
        f"Unknown DATA_PROVIDER '{key}'. Only 'angelone' is supported."
    )
