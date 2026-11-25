"""Data layer: Kotak client (against recorded real responses), candle parsing, windows, instrument master, validation,
SQLite persistence."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from config import IST
from data.data_validator import gap_report, validate
from data.historical_data import HistoricalData, parse_candles, session_is_final, windows
from data.instrument_master import nse_epoch_to_date
from data.kotak_client import KotakError, KotakMarketDataClient, KotakRateLimited, redact
from tests.conftest import kotak_fixture, load_candles, replay_transport

HIST = "/market-data/1.0/historical/details"


def client(kcfg, routes, seen=None):
    return KotakMarketDataClient(kcfg, transport=replay_transport(routes, seen), sleep=lambda s: None)


# ------------------------------------------------------------------------------------------ client
def test_historical_parses_recorded_response(kcfg):
    c = client(kcfg, {HIST: [kotak_fixture("historical_reliance")]})
    rows = c.historical("nse_cm|2885", date(2026, 6, 1), date(2026, 11, 25))
    assert len(rows) > 100
    assert rows[0][0].startswith("2026-06-01")
    df = parse_candles(rows, now=datetime(2026, 11, 25, 10, 30, tzinfo=IST))
    assert df.index.is_monotonic_increasing and not df.index.duplicated().any()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()


def test_index_history_by_name(kcfg):
    seen = []
    c = client(kcfg, {HIST: [kotak_fixture("historical_nifty50")]}, seen)
    rows = c.historical("nse_cm|Nifty 50", date(2026, 6, 1), date(2026, 11, 25))
    assert rows and seen[0].url.params["neosymbol"] == "nse_cm|Nifty 50"
    assert seen[0].headers["Authorization"] == "test-token"


def test_rate_limit_is_retried(kcfg):
    c = client(kcfg, {HIST: [kotak_fixture("historical_429"), kotak_fixture("historical_429"), kotak_fixture("historical_reliance")]})
    assert c.historical("nse_cm|2885", date(2026, 6, 1), date(2026, 11, 25))
    assert [x.status for x in c.calls] == [429, 429, 200]


def test_rate_limit_gives_up(kcfg):
    c = client(kcfg, {HIST: [kotak_fixture("historical_429")]})
    with pytest.raises(KotakRateLimited):
        c.historical("nse_cm|2885", date(2026, 6, 1), date(2026, 11, 25))


def test_api_error_is_raised_not_swallowed(kcfg):
    c = client(kcfg, {HIST: [kotak_fixture("historical_range_error")]})
    with pytest.raises(KotakError, match="180 days"):
        c.historical("nse_cm|2885", date(2025, 12, 1), date(2026, 6, 1))


def test_quotes_and_scrip_paths(kcfg):
    c = client(kcfg, {"/script-details/1.0/quotes/neosymbol/nse_cm|2885,nse_cm|Nifty 50/all": [kotak_fixture("quotes_all")],
                      "/script-details/1.0/masterscrip/file-paths": [kotak_fixture("scrip_paths")]})
    q = c.quotes(["nse_cm|2885", "nse_cm|Nifty 50"])
    assert {r["exchange_token"] for r in q} == {"2885", "Nifty 50"}
    assert all(float(r["ltp"]) > 0 for r in q)
    paths = c.scrip_master_paths()
    assert any(p.endswith("nse_cm-v1.csv") for p in paths)


def test_redact_hides_uuid_tokens():
    assert "<redacted>" in redact("key 12345678-1234-1234-1234-123456789abc rejected")


def test_missing_token_refuses_to_build(kcfg):
    from dataclasses import replace

    from data.kotak_client import KotakAuthError
    with pytest.raises(KotakAuthError):
        KotakMarketDataClient(replace(kcfg, access_token=""))


# ------------------------------------------------------------------------------------------ parsing / windows
def test_forming_bar_dropped_until_close():
    rows = [["2026-11-23T00:00:00+0530", 1381, 1390, 1346.9, 1351, 15392522],
            ["2026-11-25T09:15:00+0530", 1359.8, 1394.2, 1357, 1380.5, 7042884]]  # real rows from the recorded response
    morning = parse_candles(rows, now=datetime(2026, 11, 25, 10, 25, tzinfo=IST))
    evening = parse_candles(rows, now=datetime(2026, 11, 25, 16, 0, tzinfo=IST))
    assert list(morning.index.date) == [date(2026, 11, 23)]
    assert list(evening.index.date) == [date(2026, 11, 23), date(2026, 11, 25)]
    assert session_is_final(date(2026, 11, 24), datetime(2026, 11, 25, 9, 0, tzinfo=IST))


def test_windows_respect_limit_and_cover_range():
    w = windows(date(2023, 1, 2), date(2026, 11, 25))
    assert w[0][0] == date(2023, 1, 2) and w[-1][1] == date(2026, 11, 25)
    for a, b in w:
        assert (b - a).days + 1 <= 179
    for (a1, b1), (a2, b2) in zip(w, w[1:]):
        assert (a2 - b1).days == 1


def test_sync_fetches_windows_and_stores(kcfg, repo):
    seen = []
    c = client(kcfg, {HIST: [kotak_fixture("historical_reliance")]}, seen)
    hd = HistoricalData(c, repo)
    n = hd.sync("RELIANCE", "nse_cm|2885", date(2026, 6, 1), date(2026, 11, 25))
    assert n > 100 and len(seen) == 1
    df = repo.candles("RELIANCE")
    assert df.index[0] == pd.Timestamp("2026-06-01")
    assert repo.api_log().iloc[0]["status"] == 200


def test_incremental_sync_detects_readjusted_history(kcfg, repo):
    """If cached closes disagree with fresh data (a split/bonus re-adjusted the series), the symbol is re-downloaded."""
    real = load_candles("RELIANCE").loc["2026-06-01":"2026-11-23"]
    halved = real.copy()
    halved[["open", "high", "low", "close"]] *= 2  # what a cache would look like before a 1:1 bonus adjustment
    repo.upsert_candles("RELIANCE", halved, "test")
    c = client(kcfg, {HIST: [kotak_fixture("historical_reliance")]})
    HistoricalData(c, repo).sync("RELIANCE", "nse_cm|2885", date(2026, 6, 1), date(2026, 11, 25))
    df = repo.candles("RELIANCE")
    assert abs(df.loc["2026-06-01", "close"] - real.loc["2026-06-01", "close"]) < 1e-6


# ------------------------------------------------------------------------------------------ instrument master
def test_nse_epoch_conversion_matches_contract_names(universe):
    rel = universe["RELIANCE"]
    by = {f["trading_symbol"]: f["expiry"] for f in rel.futures}
    # RELIANCE26DECFUT / RELIANCE27JANFUT: expiry month in the name must match the converted epoch
    assert by["RELIANCE26DECFUT"].year == 2026 and by["RELIANCE26DECFUT"].month == 12
    assert by["RELIANCE27JANFUT"] == date(2027, 1, 25)
    assert nse_epoch_to_date(0) is None


def test_universe_matches_scrip_master(universe):
    rel = universe["RELIANCE"]
    assert rel.token == "2885" and rel.trading_symbol == "RELIANCE-EQ"
    assert rel.tick == pytest.approx(0.10)
    assert rel.industry == "Oil Gas & Consumable Fuels" and rel.sector_index == "NIFTY OIL AND GAS"
    assert universe["HDFCBANK"].sector_index == "Nifty Bank"
    assert rel.nearest_future(date(2026, 11, 25)) is not None


# ------------------------------------------------------------------------------------------ validation
def test_real_data_validates_clean(candles):
    cal = candles["Nifty 50"].index
    for s in ("RELIANCE", "TCS", "HDFCBANK"):
        rep = validate(s, candles[s], cal)
        assert rep.ok, rep.errors
        assert rep.bad_ohlc == 0 and rep.duplicates == 0


def test_validator_catches_corruption(candles):
    cal = candles["Nifty 50"].index
    df = candles["TCS"].copy()
    df.iloc[100, df.columns.get_loc("high")] = df.iloc[100]["low"] - 1  # high below low
    df = df.drop(df.index[200:230])  # 30 missing sessions
    rep = validate("TCS", df, cal)
    assert not rep.ok
    assert rep.bad_ohlc == 1 and len(rep.missing_sessions) == 30


def test_validator_flags_stale(candles):
    cal = candles["Nifty 50"].index
    rep = validate("TCS", candles["TCS"].iloc[:-5], cal)
    assert rep.stale_days == 5 and not rep.ok


def test_gap_report(candles):
    g = gap_report(candles["TATASTEEL"], 2.0)
    assert (g["gap_pct"].abs() >= 2.0).all()


# ------------------------------------------------------------------------------------------ persistence
def test_repository_roundtrip(repo, candles):
    df = candles["INFY"].iloc[-50:]
    repo.upsert_candles("INFY", df, "kotak_neo_historical")
    repo.upsert_candles("INFY", df, "kotak_neo_historical")  # idempotent
    back = repo.candles("INFY")
    assert len(back) == 50
    pd.testing.assert_frame_equal(back, df.astype(float), check_freq=False, check_names=False)
    cov = repo.candle_coverage()
    assert cov.iloc[0]["n"] == 50 and cov.iloc[0]["last_d"] == str(df.index[-1].date())
