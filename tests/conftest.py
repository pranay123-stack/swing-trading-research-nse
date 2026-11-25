"""Shared fixtures. All market data here is REAL (exported from Kotak Neo responses); see tests/fixtures/make_fixtures.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pandas as pd
import pytest

from config import KotakConfig
from data.cache import DailyFileCache
from data.instrument_master import InstrumentMaster, read_constituents
from database.repository import Repository

FIX = Path(__file__).parent / "fixtures"
STOCKS = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "LT", "TATASTEEL", "HINDALCO", "SBIN", "BHARTIARTL"]
INDICES = ["Nifty 50", "Nifty Bank", "NIFTY MIDCAP 100", "INDIA VIX", "Nifty IT", "Nifty Fin Service", "Nifty Metal",
           "NIFTY OIL AND GAS", "Nifty Infra", "Nifty 500"]


def load_candles(name: str) -> pd.DataFrame:
    df = pd.read_csv(FIX / "candles" / f"{name.replace(' ', '_')}.csv", parse_dates=["d"]).set_index("d")
    return df[["open", "high", "low", "close", "volume"]]


def kotak_fixture(name: str) -> dict:
    return json.loads((FIX / "kotak" / f"{name}.json").read_text())


@pytest.fixture(scope="session")
def candles() -> dict[str, pd.DataFrame]:
    return {s: load_candles(s) for s in STOCKS + INDICES}


@pytest.fixture
def repo(tmp_path) -> Repository:
    r = Repository(tmp_path / "t.sqlite")
    yield r
    r.close()


@pytest.fixture(scope="session")
def scrip_cache(tmp_path_factory) -> DailyFileCache:
    root = tmp_path_factory.mktemp("cache")
    d = root / "scrip" / "2026-11-25"
    d.mkdir(parents=True)
    for f in ("nse_cm-v1.csv", "nse_fo.csv"):
        shutil.copy(FIX / "scrip" / f, d / f)
    return DailyFileCache(root, "scrip")


@pytest.fixture(scope="session")
def universe(scrip_cache):
    cons = {k: v for k, v in read_constituents().items() if k in STOCKS}
    return InstrumentMaster(None, scrip_cache).build_universe(cons)


@pytest.fixture(scope="session")
def research_data(tmp_path_factory, candles, universe):
    from research.stock_screener import load_research_data

    r = Repository(tmp_path_factory.mktemp("rd") / "rd.sqlite")
    for s, df in candles.items():
        r.upsert_candles(s, df, "kotak_neo_historical")
    return load_research_data(r, universe, INDICES)


def replay_transport(routes: dict[str, list[dict]], seen: list | None = None) -> httpx.MockTransport:
    """routes: path -> list of recorded {status_code, body}; served in order (last one repeats)."""
    counters: dict[str, int] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        path = req.url.path
        if path not in routes:
            return httpx.Response(404, json={"error": "unrecorded path " + path})
        i = counters.get(path, 0)
        counters[path] = i + 1
        rec = routes[path][min(i, len(routes[path]) - 1)]
        return httpx.Response(rec["status_code"], json=rec["body"])

    return httpx.MockTransport(handler)


@pytest.fixture
def kcfg() -> KotakConfig:
    return KotakConfig(access_token="test-token", base_url="https://e22.kotaksecurities.com", requests_per_second=1000,
                       max_retries=3)
