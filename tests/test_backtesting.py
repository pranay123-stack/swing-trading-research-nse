"""Backtest engine, walk-forward separation, metrics and look-ahead checks on real fixture data."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtesting.engine import generate_signals, simulate_portfolio, simulate_signals
from backtesting.performance import equity_metrics, trade_metrics
from backtesting.walk_forward import folds, walk_forward
from config import STRATEGY
from research.indicators import add_indicators
from research.stock_screener import load_research_data
from research.trade_generator import StockView, detect
from research import support_resistance as sr


@pytest.fixture(scope="module")
def bt(research_data):
    rd = research_data
    start, end = rd.calendar[260], rd.calendar[-20]
    sig = generate_signals(rd, start, end)
    iso = simulate_signals(rd, sig[sig["regime_ok"]])
    return rd, start, end, sig, iso


def test_signals_generated(bt):
    rd, start, end, sig, iso = bt
    assert len(sig) > 20
    assert sig["d"].between(start, end).all()
    assert {"LONG", "SHORT"} >= set(sig["side"])


def test_trades_start_after_signal(bt):
    rd, start, end, sig, iso = bt
    filled = iso[iso["entry_price"] > 0]
    assert len(filled) > 5
    assert (pd.to_datetime(filled["entry_d"]) > pd.to_datetime(filled["signal_d"])).all()
    closed = filled[filled["state"] == "CLOSED"]
    assert (pd.to_datetime(closed["exit_d"]) >= pd.to_datetime(closed["entry_d"])).all()
    assert (closed["bars_held"] <= STRATEGY.max_holding_days).all()


def test_costs_always_charged(bt):
    _, _, _, _, iso = bt
    f = iso[iso["entry_price"] > 0]
    assert (f["costs"] > 0).all()
    assert np.allclose(f["net"], f["gross"] - f["costs"], atol=0.05) or (f["net"] <= f["gross"]).all()


def test_signals_identical_with_truncated_history(research_data, candles, universe, tmp_path):
    """The strongest look-ahead check: signals on day d from the full dataset equal signals from data cut at d."""
    from database.repository import Repository
    rd = research_data
    d = rd.calendar[-150]
    full = generate_signals(rd, rd.calendar[-170], d)
    r = Repository(tmp_path / "cut.sqlite")
    for s, df in candles.items():
        r.upsert_candles(s, df.loc[:d], "kotak_neo_historical")
    cut = load_research_data(r, universe, list(rd.indices))
    trunc = generate_signals(cut, rd.calendar[-170], d)
    cols = ["d", "symbol", "side", "setup", "entry", "stop", "t1", "t2", "score", "regime"]
    pd.testing.assert_frame_equal(full[cols].reset_index(drop=True), trunc[cols].reset_index(drop=True))


def test_walk_forward_separation(bt):
    rd, start, end, sig, iso = bt
    closed = iso[iso["state"] == "CLOSED"]
    fs = folds(rd.calendar, start, train=120, test=40)
    assert len(fs) >= 3
    for (a, b, c, d) in fs:
        assert a <= b < c <= d
    for (_, b1, c1, d1), (_, b2, c2, d2) in zip(fs, fs[1:]):
        assert c2 > d1 - pd.Timedelta(days=1)  # test windows do not overlap
    flist, oos = walk_forward(closed, rd.calendar, start, train=120, test=40)
    for f in flist:
        assert f.train_end < f.test_start
    if len(oos):
        for f in flist:
            part = oos[(oos["signal_d"] >= f.test_start) & (oos["signal_d"] <= f.test_end)]
            assert (part["score"] >= f.threshold).all()


def test_portfolio_constraints(bt):
    rd, start, end, sig, iso = bt
    res = simulate_portfolio(rd, sig, start, end, min_score=0)
    t = res.trades
    assert len(res.equity) > 0
    if len(t):
        # never two overlapping positions in the same stock
        for sym, g in t.groupby("symbol"):
            g = g.sort_values("entry_d")
            ends = pd.to_datetime(g["exit_d"]).to_numpy()
            starts = pd.to_datetime(g["entry_d"]).to_numpy()
            assert (starts[1:] >= ends[:-1]).all()


def test_metrics_hand_check():
    t = pd.DataFrame({"entry_price": [1, 1, 1, 1], "net": [200.0, -100.0, 300.0, -100.0], "r": [2.0, -1.0, 3.0, -1.0],
                      "bars_held": [5, 3, 10, 2], "costs": [10.0] * 4, "exit_reason": ["T2", "STOP", "T2", "STOP"]})
    m = trade_metrics(t)
    assert m["trades"] == 4 and m["win_rate"] == 50
    assert m["profit_factor"] == pytest.approx(2.5)
    assert m["expectancy"] == pytest.approx(75) and m["expectancy_r"] == pytest.approx(0.75)
    assert m["avg_win"] == 250 and m["avg_loss"] == -100
    eq = pd.Series([100.0, 110, 99, 120], index=pd.date_range("2026-01-01", periods=4))
    e = equity_metrics(eq, 100)
    assert e["total_return_pct"] == pytest.approx(20) and e["max_drawdown_pct"] == pytest.approx(-10)


def test_equity_csv_has_named_columns(tmp_path):
    from backtesting.report import equity_frame
    eq = pd.Series([100.0, 110, 99], index=pd.date_range("2026-01-01", periods=3))
    p = tmp_path / "e.csv"
    equity_frame(eq).to_csv(p)
    assert p.read_text().splitlines()[0] == "date,equity_inr,daily_return_pct,drawdown_pct"
    back = pd.read_csv(p)
    assert back["drawdown_pct"].iloc[-1] == pytest.approx(-10.0)
