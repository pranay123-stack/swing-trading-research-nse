"""Daily OHLCV from the Kotak Neo historical API, cached in SQLite.

Primary and only source: Kotak Neo `market-data/1.0/historical/details` (interval D). No other provider is used for
prices, and a symbol whose data cannot be fetched is excluded rather than filled from elsewhere.

Rules:
  * Requests are split into <=179-day windows (the API rejects >180 days for D) within the 5-year lookback limit.
  * A bar dated today is dropped until SESSION_FINAL_AFTER (15:45 IST): before that it is still forming.
    Kotak marks forming bars with a T09:15 timestamp instead of T00:00, which is also checked.
  * Incremental refresh re-fetches the last few weeks. If the overlapping closes disagree with the cache (a split or
    bonus re-adjusted history), the whole symbol is re-downloaded so the series is never mixed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd

from config import IST, KOTAK, SESSION_FINAL_AFTER
from data.kotak_client import KotakError, KotakMarketDataClient
from database.repository import Repository

log = logging.getLogger(__name__)
SOURCE = "kotak_neo_historical"


def now_ist() -> datetime:
    return datetime.now(IST)


def session_is_final(day: date, now: datetime | None = None) -> bool:
    now = now or now_ist()
    if day < now.date():
        return True
    if day > now.date():
        return False
    return (now.hour, now.minute) >= SESSION_FINAL_AFTER


def parse_candles(rows: list[list], now: datetime | None = None) -> pd.DataFrame:
    """Kotak candle rows [ts, o, h, l, c, v, (oi)] -> DataFrame indexed by session date (IST)."""
    recs = []
    for r in rows:
        ts = datetime.fromisoformat(str(r[0]).replace("+0530", "+05:30")).astimezone(IST)
        d = ts.date()
        if not session_is_final(d, now):
            continue  # today's bar before the close is still forming (Kotak stamps it T09:15)
        recs.append({"d": pd.Timestamp(d), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                     "close": float(r[4]), "volume": float(r[5])})
    if not recs:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], name="d"))
    df = pd.DataFrame(recs).drop_duplicates("d", keep="last").set_index("d").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def windows(start: date, end: date, span_days: int = KOTAK.max_daily_window_days) -> list[tuple[date, date]]:
    out = []
    s = start
    while s <= end:
        e = min(s + timedelta(days=span_days - 1), end)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


class HistoricalData:
    def __init__(self, client: KotakMarketDataClient | None, repo: Repository):
        self.client = client
        self.repo = repo
        self.errors: dict[str, str] = {}

    def earliest_allowed(self, today: date | None = None) -> date:
        today = today or now_ist().date()
        return today.replace(year=today.year - KOTAK.max_lookback_years) + timedelta(days=2)

    def _fetch_range(self, neosymbol: str, start: date, end: date) -> pd.DataFrame:
        parts = []
        for s, e in windows(start, end):
            rows = self.client.historical(neosymbol, s, e, "D")
            parts.append(parse_candles(rows))
        parts = [p for p in parts if not p.empty]
        if not parts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.concat(parts)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def sync(self, symbol: str, neosymbol: str, start: date, end: date | None = None) -> int:
        """Bring the cache for `symbol` up to date over [start, end]. Returns bars written."""
        if self.client is None:
            raise KotakError("no Kotak client: cannot fetch market data")
        end = end or now_ist().date()
        start = max(start, self.earliest_allowed(end))
        cached = self.repo.candles(symbol)
        try:
            if cached.empty or cached.index[0].date() > start + timedelta(days=7):
                df = self._fetch_range(neosymbol, start, end)
                self.repo.upsert_candles(symbol, df, SOURCE)
                self.repo.log_fetch(symbol, start, end, len(df))
                return len(df)
            last = cached.index[-1].date()
            inc_start = last - timedelta(days=21)
            df = self._fetch_range(neosymbol, inc_start, end)
            overlap = cached.index.intersection(df.index)
            if len(overlap):
                diff = (cached.loc[overlap, "close"] - df.loc[overlap, "close"]).abs() / df.loc[overlap, "close"]
                if float(diff.max()) > 0.005:
                    log.warning("%s: cached history disagrees with fresh data (max %.2f%%) -> full re-download",
                                symbol, 100 * float(diff.max()))
                    with self.repo.tx() as c:
                        c.execute("DELETE FROM candles WHERE symbol=?", (symbol,))
                    df = self._fetch_range(neosymbol, start, end)
            self.repo.upsert_candles(symbol, df, SOURCE)
            self.repo.log_fetch(symbol, inc_start, end, len(df))
            return len(df)
        except KotakError as e:
            self.errors[symbol] = str(e)
            log.error("%s: %s", symbol, e)
            return 0
        finally:
            self.repo.log_api_calls(self.client.calls)
            self.client.calls.clear()

    def sync_many(self, items: Iterable[tuple[str, str]], start: date, end: date | None = None, progress=None) -> dict[str, int]:
        out = {}
        items = list(items)
        for i, (sym, neo) in enumerate(items):
            out[sym] = self.sync(sym, neo, start, end)
            if progress:
                progress(i + 1, len(items), sym, out[sym])
        return out

    def load(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        return self.repo.candles(symbol, start, end)
