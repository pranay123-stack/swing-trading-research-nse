"""Read-only Kotak Neo Trade API client: scrip master, quotes and historical candles.

This client deliberately has NO order, modify, cancel, position or funds methods, and refuses to send a request to
any path outside MARKET_DATA_PATHS. That is the code-level guarantee that this project cannot place a real order.

Endpoints (official docs github.com/Kotak-Neo/Kotak-Neo/docs/market-data-apis, read 2026-11-25; all verified live
2026-11-25 with the NEO API dashboard token alone):
  GET <base>/script-details/1.0/masterscrip/file-paths
  GET <base>/script-details/1.0/quotes/neosymbol/<seg>|<pSymbol or index name>[,...][/<filter>]
  GET <base>/market-data/1.0/historical/details?neosymbol=<seg>|<token or index name>&fromdate&todate&interval
Login (only to discover <base> when KOTAK_BASE_URL is not pinned):
  POST https://mis.kotaksecurities.com/login/1.0/tradeApiLogin     (TOTP step)
  POST https://mis.kotaksecurities.com/login/1.0/tradeApiValidate  (MPIN step -> baseUrl)

Observed behaviour (live, 2026-11-25) the code relies on:
  * historical: indices are addressed by exact index name ("Nifty 50"), not token 26000 (422 "Invalid neosymbol").
  * historical: >180-day daily range -> 422; from-date older than 5 years -> 422.
  * historical: ~1 request/s sustained; faster gets 429 with no Retry-After header.
  * the current session's daily bar is returned while forming, stamped T09:15:00 instead of T00:00:00.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pyotp

from config import KOTAK, KotakConfig, assert_paper_mode

log = logging.getLogger(__name__)

# The only request paths this client will ever send. Anything else raises ForbiddenEndpoint before any I/O.
MARKET_DATA_PATHS = (
    re.compile(r"^/script-details/1\.0/masterscrip/file-paths$"),
    re.compile(r"^/script-details/1\.0/quotes/neosymbol/[^/]+(/(all|52W|scrip_details|circuit_limits|ohlc|oi|depth|ltp))?$"),
    re.compile(r"^/market-data/1\.0/historical/details$"),
)
LOGIN_PATHS = (
    re.compile(r"^/login/1\.0/tradeApiLogin$"),
    re.compile(r"^/login/1\.0/tradeApiValidate$"),
)
SCRIP_FILE_HOST = "lapi.kotaksecurities.com"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class KotakError(RuntimeError):
    pass


class ForbiddenEndpoint(KotakError):
    """A request was attempted to a path that is not a read-only market-data endpoint."""


class KotakAuthError(KotakError):
    pass


class KotakRateLimited(KotakError):
    pass


def redact(text: str) -> str:
    return _UUID.sub("<redacted>", text)


@dataclass
class ApiCallRecord:
    ts: float
    method: str
    path: str
    status: int
    ms: float
    error: str = ""


class _Throttle:
    def __init__(self, rate: float):
        self.min_gap = 1.0 / rate if rate > 0 else 0.0
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            gap = self.last + self.min_gap - now
            if gap > 0:
                time.sleep(gap)
            self.last = time.monotonic()


class KotakMarketDataClient:
    """Synchronous, throttled, retrying, read-only market-data client."""

    def __init__(self, cfg: KotakConfig = KOTAK, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        assert_paper_mode()
        if not cfg.access_token:
            raise KotakAuthError("KOTAK_ACCESS_TOKEN is not set (see .env.example)")
        self.cfg = cfg
        self.base_url = cfg.base_url
        if transport is None:
            # Force IPv4: Kotak hosts publish AAAA records and the account's IP whitelist is IPv4 (seen 2026-11-12).
            transport = httpx.HTTPTransport(local_address="0.0.0.0")
        self._http = httpx.Client(transport=transport, timeout=cfg.timeout_s)
        self._throttle = _Throttle(cfg.requests_per_second)
        self._sleep = sleep
        self.calls: list[ApiCallRecord] = []

    def close(self) -> None:
        self._http.close()

    # ----------------------------------------------------------------------------- guard
    @staticmethod
    def check_path(path: str, allowed=MARKET_DATA_PATHS) -> None:
        if not any(p.match(path) for p in allowed):
            raise ForbiddenEndpoint(f"refusing request to non-market-data path {path!r}")

    # ----------------------------------------------------------------------------- login (base URL discovery)
    def login(self, totp: str | None = None) -> str:
        """Two-step login, used only to learn baseUrl. Never called when KOTAK_BASE_URL is pinned."""
        c = self.cfg
        if not (c.mobile and c.ucc and c.mpin):
            raise KotakAuthError("KOTAK_MOBILE / KOTAK_UCC / KOTAK_MPIN are required to log in")
        if totp is None:
            if not c.totp_secret:
                raise KotakAuthError("no KOTAK_TOTP_SECRET; set KOTAK_BASE_URL or pass a 6-digit TOTP")
            totp = pyotp.TOTP(c.totp_secret).now()
        for p in ("/login/1.0/tradeApiLogin", "/login/1.0/tradeApiValidate"):
            self.check_path(p, LOGIN_PATHS)
        h = {"Authorization": c.access_token, "neo-fin-key": "neotradeapi", "Content-Type": "application/json"}
        r1 = self._http.post(f"{c.login_host}/login/1.0/tradeApiLogin", headers=h,
                             json={"mobileNumber": c.mobile, "ucc": c.ucc, "totp": totp})
        d1 = _login_data(r1, "tradeApiLogin")
        r2 = self._http.post(f"{c.login_host}/login/1.0/tradeApiValidate",
                             headers={**h, "sid": d1["sid"], "Auth": d1["token"]}, json={"mpin": c.mpin})
        d2 = _login_data(r2, "tradeApiValidate")
        self.base_url = str(d2["baseUrl"]).rstrip("/")
        log.info("kotak login ok, base_url=%s", self.base_url)
        return self.base_url

    def _ensure_base(self) -> str:
        if not self.base_url:
            self.login()
        return self.base_url

    # ----------------------------------------------------------------------------- transport
    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        self.check_path(path)
        url = f"{self._ensure_base()}{path}"
        headers = {"Authorization": self.cfg.access_token, "Content-Type": "application/json"}
        last_err = ""
        for attempt in range(self.cfg.max_retries + 1):
            self._throttle.wait()
            t0 = time.monotonic()
            try:
                resp = self._http.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{type(e).__name__}: {e}"
                self.calls.append(ApiCallRecord(time.time(), "GET", path, 0, (time.monotonic() - t0) * 1e3, last_err))
                self._sleep(min(2 ** attempt, 30))
                continue
            ms = (time.monotonic() - t0) * 1e3
            if resp.status_code == 429:
                self.calls.append(ApiCallRecord(time.time(), "GET", path, 429, ms, "rate limited"))
                last_err = "HTTP 429"
                self._sleep(min(1.5 * (attempt + 1), 15))
                continue
            if resp.status_code >= 500:
                self.calls.append(ApiCallRecord(time.time(), "GET", path, resp.status_code, ms, "server error"))
                last_err = f"HTTP {resp.status_code}"
                self._sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code in (401, 403):
                self.calls.append(ApiCallRecord(time.time(), "GET", path, resp.status_code, ms, "auth"))
                raise KotakAuthError(f"GET {path}: HTTP {resp.status_code} {redact(resp.text[:200])}")
            try:
                body = resp.json()
            except ValueError as e:
                self.calls.append(ApiCallRecord(time.time(), "GET", path, resp.status_code, ms, "unparseable"))
                raise KotakError(f"GET {path}: HTTP {resp.status_code}, unparseable body") from e
            err = _error_message(body) if resp.status_code >= 400 or _is_error(body) else ""
            self.calls.append(ApiCallRecord(time.time(), "GET", path, resp.status_code, ms, err))
            if err:
                raise KotakError(f"GET {path} {params or ''}: HTTP {resp.status_code} {redact(err)}")
            return body
        if last_err == "HTTP 429":
            raise KotakRateLimited(f"GET {path}: still rate limited after {self.cfg.max_retries} retries")
        raise KotakError(f"GET {path}: failed after retries ({last_err})")

    # ----------------------------------------------------------------------------- endpoints
    def scrip_master_paths(self) -> list[str]:
        body = self._get("/script-details/1.0/masterscrip/file-paths")
        return list(body.get("data", {}).get("filesPaths", []))

    def download_scrip_file(self, url: str) -> str:
        if httpx.URL(url).host != SCRIP_FILE_HOST:
            raise ForbiddenEndpoint(f"scrip master download must come from {SCRIP_FILE_HOST}: {url}")
        self._throttle.wait()
        resp = self._http.get(url, timeout=120)
        resp.raise_for_status()
        return resp.text

    def quotes(self, queries: list[str], filter_name: str = "all") -> list[dict[str, Any]]:
        """queries like 'nse_cm|2885' (pSymbol) or 'nse_cm|Nifty 50' (exact index name). Batched by 50."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(queries), 50):
            chunk = ",".join(queries[i:i + 50])
            body = self._get(f"/script-details/1.0/quotes/neosymbol/{chunk}/{filter_name}")
            rows = body if isinstance(body, list) else body.get("data", []) if isinstance(body, dict) else []
            out.extend(r for r in rows if isinstance(r, dict))
        return out

    def historical(self, neosymbol: str, from_date: date, to_date: date, interval: str = "D") -> list[list[Any]]:
        """One request. Caller is responsible for keeping the range within the documented per-interval limit."""
        body = self._get(
            "/market-data/1.0/historical/details",
            {"neosymbol": neosymbol, "fromdate": from_date.isoformat(), "todate": to_date.isoformat(), "interval": interval},
        )
        candles = body.get("data", {}).get("candles", []) if isinstance(body, dict) else []
        return [c for c in candles if isinstance(c, list) and len(c) >= 6]


def _is_error(body: Any) -> bool:
    if isinstance(body, dict):
        if str(body.get("status", "")).upper() == "ERROR" or body.get("stat") == "Not_Ok":
            return True
    return False


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        f = body.get("fault")
        if isinstance(f, dict):
            return f"{f.get('code')}: {f.get('message')}"
        return str(body.get("errMsg") or body.get("emsg") or body.get("message") or json.dumps(body)[:200])
    return str(body)[:200]


def _login_data(resp: httpx.Response, step: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as e:
        raise KotakAuthError(f"{step}: HTTP {resp.status_code}, unparseable body") from e
    data = body.get("data") if isinstance(body, dict) else None
    if resp.status_code != 200 or not data or not data.get("token"):
        raise KotakAuthError(f"{step} failed: HTTP {resp.status_code} {redact(json.dumps(body)[:300])}")
    return data
