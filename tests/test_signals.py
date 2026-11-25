"""Setup detection, levels, scoring, regime and selection, on real candles."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from config import STRATEGY
from research import pattern_detection as pat
from research import support_resistance as sr
from research.indicators import add_indicators
from research.market_regime import classify, label_for
from research.stock_screener import screen_at, select
from research.trade_generator import StockView, detect, round_tick
from research.trade_scoring import WEIGHTS


@pytest.fixture(scope="module")
def all_setups(research_data):
    """Every setup the detector finds on every session of the 10 fixture stocks."""
    out = []
    for sym, df in research_data.stocks.items():
        for t in range(215, len(df)):
            out += detect(StockView(sym, df, research_data.pivots[sym], t, research_data.universe[sym].tick))
    return out


def test_detector_finds_both_sides(all_setups):
    sides = {s.side for s in all_setups}
    names = {s.setup for s in all_setups}
    assert sides == {"LONG", "SHORT"}
    assert len(names) >= 5, names


def test_trade_geometry_invariants(all_setups):
    for s in all_setups:
        if s.side == "LONG":
            assert s.stop < s.entry <= s.t1 <= s.t2, s
            assert s.stop < s.invalidation <= s.entry or s.stop <= s.entry - STRATEGY.min_stop_atr * s.atr + 0.1
            assert s.entry > s.close - 1e-9  # a buy-stop is above the signal close
        else:
            assert s.stop > s.entry >= s.t1 >= s.t2, s
            assert s.entry < s.close + 1e-9
        assert s.risk >= STRATEGY.min_stop_atr * s.atr - 2 * s.tick  # never a stop inside 1 ATR of noise
        assert abs(s.t2 - s.entry) <= 5.0 * s.atr + 2 * s.tick  # targets capped at reachable distance


def test_rejections_are_explained_and_consistent(all_setups):
    for s in all_setups:
        if s.reject_reason:
            continue
        assert s.rr_t1 >= STRATEGY.min_rr_t1 - 1e-9 and s.rr_t2 >= STRATEGY.min_rr_t2 - 1e-9
        assert s.stop_atr <= STRATEGY.max_stop_atr + 1e-9
    rejected = [s for s in all_setups if s.reject_reason]
    assert rejected and all(len(s.reject_reason) > 10 for s in rejected)


def test_levels_and_entries_on_tick_grid(all_setups):
    for s in all_setups[:200]:
        for x in (s.entry, s.stop, s.t1, s.t2):
            assert abs(round(x / s.tick) * s.tick - x) < 1e-6


def test_detector_is_causal(research_data):
    """A setup on day t is identical whether or not data after t exists."""
    checked = 0
    for sym in ("RELIANCE", "TATASTEEL", "ICICIBANK", "HINDALCO"):
        full = research_data.stocks[sym]
        piv_full = research_data.pivots[sym]
        for t in range(400, len(full) - 30, 37):
            trunc = add_indicators(full[["open", "high", "low", "close", "volume"]].iloc[:t + 1])
            a = [x.to_dict() for x in detect(StockView(sym, full, piv_full, t))]
            b = [x.to_dict() for x in detect(StockView(sym, trunc, sr.find_pivots(trunc), t))]
            for x in a + b:
                x.pop("features", None)
            assert a == b, (sym, full.index[t])
            checked += 1
    assert checked > 40


def test_pivot_confirmation_lag(candles):
    df = candles["INFY"]
    piv = sr.find_pivots(df, 5)
    t = 600
    known = sr.known_pivots(piv, t, 5)
    assert all(p.i + 5 <= t for p in known)
    # pivots from truncated data at t (without the 5 confirmation bars) are a superset-consistent subset
    trunc = sr.find_pivots(df.iloc[:t + 1], 5)
    assert {(p.i, p.kind) for p in known} <= {(p.i, p.kind) for p in trunc}


def test_cluster_levels():
    from research.support_resistance import Pivot, cluster_levels
    d = pd.Timestamp("2026-01-01")
    lv = cluster_levels([Pivot(1, d, 100, "H"), Pivot(2, d, 100.4, "L"), Pivot(3, d, 110, "H")], tolerance=1.0)
    assert len(lv) == 2 and lv[0].touches == 2 and lv[0].role_flip


def test_candle_patterns_real_bar(candles):
    df = candles["RELIANCE"]
    seen = set()
    for t in range(1, len(df)):
        seen |= set(pat.candle_patterns(df, t))
    assert {"bullish_engulfing", "bearish_engulfing", "hammer", "inside_bar", "doji"} <= seen


def test_round_tick():
    assert round_tick(100.03, 0.05, "up") == 100.05
    assert round_tick(100.03, 0.05, "down") == 100.0
    assert round_tick(100.07, 0.1) == 100.1


def test_scores_bounded_and_explained(research_data):
    reg, cands, _ = screen_at(research_data, research_data.calendar[-60], replace(STRATEGY, min_score=-1))
    found = 0
    for d in research_data.calendar[-120::5]:
        reg, cands, _ = screen_at(research_data, d, replace(STRATEGY, min_score=-1))
        for c in cands:
            found += 1
            assert 0 <= c.card.total <= 100
            assert sum(x.points for x in c.card.components) == pytest.approx(c.card.total)
            for comp in c.card.components:
                assert 0 <= comp.points <= comp.max_points == WEIGHTS[comp.name]
                assert comp.pros or comp.cons
    assert found > 0
    assert sum(WEIGHTS.values()) == 100


def test_regime_labels():
    assert label_for(0.9) == "Strong Bullish" and label_for(0.3) == "Bullish" and label_for(0.0) == "Sideways"
    assert label_for(-0.3) == "Bearish" and label_for(-0.8) == "Strong Bearish"


def test_regime_uses_only_past(research_data):
    d = research_data.calendar[-100]
    a = classify(research_data.indices, d, research_data.breadth50, research_data.breadth200)
    trimmed = {k: v.loc[:d] for k, v in research_data.indices.items()}
    b = classify(trimmed, d, research_data.breadth50.loc[:d], research_data.breadth200.loc[:d])
    assert (a.label, a.score) == (b.label, b.score)


def test_regime_blocks_counter_trend(research_data):
    from research.market_regime import Regime
    assert not Regime("Strong Bullish", 0.9).allows("SHORT")
    assert not Regime("Strong Bearish", -0.9).allows("LONG")
    assert Regime("Sideways", 0).allows("LONG") and Regime("Sideways", 0).allows("SHORT")


def test_selection_respects_sector_and_returns_shortfall(research_data):
    rd = research_data
    best = None
    for d in rd.calendar[-400:]:
        _, cands, _ = screen_at(rd, d, replace(STRATEGY, min_score=-1))
        for c in cands:
            c.passed = not c.setup.reject_reason
        if sum(c.passed for c in cands) >= 2:
            best = (d, cands)
            break
    assert best, "expected a session with 2+ valid setups in 400 sessions"
    d, cands = best
    chosen, notes = select(rd, cands, d, n=3)
    inds = [rd.universe[c.symbol].industry for c in chosen]
    assert len(inds) == len(set(inds))
    assert len({c.symbol for c in chosen}) == len(chosen)
    chosen_n, _ = select(rd, cands, d, n=50)
    assert len(chosen_n) < 50  # never manufactures ideas to fill a quota
