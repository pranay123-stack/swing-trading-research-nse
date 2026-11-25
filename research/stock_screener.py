"""Universe screening: liquidity -> setup detection -> scoring -> validation -> diversified selection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from config import STRATEGY, UNIVERSE, StrategyConfig, UniverseConfig
from data.data_validator import validate
from data.instrument_master import Equity
from database.repository import Repository
from research import support_resistance as sr
from research.indicators import add_indicators
from research.market_regime import Regime, breadth_series, classify
from research.relative_strength import rs_table
from research.trade_generator import Setup, StockView, detect
from research.trade_scoring import ScoreCard, score

log = logging.getLogger(__name__)


@dataclass
class ResearchData:
    universe: dict[str, Equity]
    stocks: dict[str, pd.DataFrame]  # with indicators
    pivots: dict[str, list[sr.Pivot]]
    rs: dict[str, pd.DataFrame]
    indices: dict[str, pd.DataFrame]  # with indicators
    breadth50: pd.Series
    breadth200: pd.Series
    calendar: pd.DatetimeIndex
    validation: dict[str, dict] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)


def load_research_data(repo: Repository, universe: dict[str, Equity], index_names: list[str],
                       start: date | None = None) -> ResearchData:
    indices = {}
    for n in index_names:
        df = repo.candles(n, start)
        if len(df) >= 60:
            indices[n] = add_indicators(df)
    if "Nifty 50" not in indices:
        raise RuntimeError("NIFTY 50 history is missing; sync data first (python run_research.py)")
    cal = indices["Nifty 50"].index
    stocks, pivots, rs, val, excl = {}, {}, {}, {}, {}
    for sym, eq in universe.items():
        df = repo.candles(sym, start)
        rep = validate(sym, df, cal)
        val[sym] = rep.as_row()
        if not rep.ok:
            excl[sym] = "data: " + "; ".join(rep.errors)
            continue
        x = add_indicators(df)
        stocks[sym] = x
        pivots[sym] = sr.find_pivots(x, 5)
        sec = indices.get(eq.sector_index)
        rs[sym] = rs_table(x["close"], indices["Nifty 50"]["close"], sec["close"] if sec is not None else None)
    b50 = breadth_series(stocks, "ema50")
    b200 = breadth_series(stocks, "sma200")
    return ResearchData(universe, stocks, pivots, rs, indices, b50, b200, cal, val, excl)


def liquidity(df: pd.DataFrame, t: int, cfg: UniverseConfig = UNIVERSE) -> tuple[bool, str, dict]:
    w = df.iloc[max(0, t - 19):t + 1]
    m = {"price": float(df["close"].iloc[t]),
         "median_value_cr": float(w["traded_value_cr"].median()),
         "median_volume": float(w["volume"].median()),
         "bars": t + 1,
         "zero_vol_60": int((df["volume"].iloc[max(0, t - 59):t + 1] <= 0).sum())}
    if m["bars"] < cfg.min_history_bars:
        return False, f"only {m['bars']} bars of history", m
    if m["price"] < cfg.min_price:
        return False, f"price {m['price']:.2f} < {cfg.min_price}", m
    if m["median_value_cr"] < cfg.min_median_traded_value_cr:
        return False, f"median traded value Rs {m['median_value_cr']:.0f} cr < {cfg.min_median_traded_value_cr:.0f} cr", m
    if m["median_volume"] < cfg.min_median_volume:
        return False, f"median volume {m['median_volume']:,.0f} < {cfg.min_median_volume:,}", m
    if m["zero_vol_60"] > cfg.max_zero_volume_days_60:
        return False, f"{m['zero_vol_60']} zero-volume sessions in 60 (suspension / illiquidity)", m
    return True, "", m


@dataclass
class Candidate:
    setup: Setup
    card: ScoreCard
    regime: str
    liquidity: dict
    passed: bool
    reject_reason: str = ""
    sizing: dict | None = None
    events: dict | None = None

    @property
    def symbol(self) -> str:
        return self.setup.symbol

    def row(self) -> dict:
        s = self.setup
        return {"symbol": s.symbol, "side": s.side, "setup": s.setup, "score": round(self.card.total, 1),
                "passed": self.passed, "reject_reason": self.reject_reason or s.reject_reason,
                "close": s.close, "entry": s.entry, "stop": s.stop, "t1": s.t1, "t2": s.t2,
                "rr_t1": round(s.rr_t1, 2), "rr_t2": round(s.rr_t2, 2), "stop_atr": round(s.stop_atr, 2),
                "regime": self.regime, **{f"pts_{c.name}": c.points for c in self.card.components}}


def regime_at(rd: ResearchData, d: pd.Timestamp) -> Regime:
    return classify(rd.indices, d, rd.breadth50, rd.breadth200)


def sector_rs20(rd: ResearchData, eq: Equity, d: pd.Timestamp) -> float | None:
    sec = rd.indices.get(eq.sector_index)
    n = rd.indices["Nifty 50"]["close"].loc[:d]
    if sec is None or len(n) < 21:
        return None
    s = sec["close"].loc[:d]
    return float((s.iloc[-1] / s.iloc[-21]) / (n.iloc[-1] / n.iloc[-21]) * 100 - 100)


def screen_at(rd: ResearchData, d: pd.Timestamp, cfg: StrategyConfig = STRATEGY, regime: Regime | None = None,
              symbols: list[str] | None = None) -> tuple[Regime, list[Candidate], dict[str, str]]:
    """Evaluate every stock on session d using only data up to d."""
    regime = regime or regime_at(rd, d)
    cands: list[Candidate] = []
    skipped: dict[str, str] = {}
    for sym in symbols or rd.stocks:
        df = rd.stocks[sym]
        if d not in df.index:
            skipped[sym] = "no bar on this session"
            continue
        t = int(df.index.get_loc(d))
        ok, why, liq = liquidity(df, t)
        if not ok:
            skipped[sym] = "liquidity: " + why
            continue
        eq = rd.universe[sym]
        view = StockView(sym, df, rd.pivots[sym], t, eq.tick)
        found = detect(view, cfg)
        if not found:
            continue
        rs_row = rd.rs[sym].iloc[t]
        sec20 = sector_rs20(rd, eq, d)
        for s in found:
            card = score(s, df, t, rs_row, regime.label, sec20)
            reason = s.reject_reason
            if not reason and not regime.allows(s.side):
                reason = f"{s.side.lower()} ideas are not taken in a {regime.label} market"
            if not reason and card.total < cfg.min_score:
                reason = f"score {card.total:.0f} < {cfg.min_score:.0f}"
            cands.append(Candidate(s, card, regime.label, liq, passed=not reason, reject_reason=reason))
    cands.sort(key=lambda c: c.card.total, reverse=True)
    return regime, cands, skipped


def return_correlation(rd: ResearchData, a: str, b: str, d: pd.Timestamp, n: int = 60) -> float:
    ra = rd.stocks[a]["close"].loc[:d].pct_change().iloc[-n:]
    rb = rd.stocks[b]["close"].loc[:d].pct_change().iloc[-n:]
    j = pd.concat([ra, rb], axis=1).dropna()
    return float(j.corr().iloc[0, 1]) if len(j) > 20 else 0.0


def select(rd: ResearchData, cands: list[Candidate], d: pd.Timestamp, n: int = 3, cfg: StrategyConfig = STRATEGY,
           event_check=None, sizer=None) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Greedy best-score selection with one idea per stock, per-sector cap, correlation cap and event-risk check.
    Returns (selected, [(symbol, why skipped)]). Fewer than n is a valid outcome."""
    chosen: list[Candidate] = []
    notes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in cands:
        if len(chosen) >= n:
            break
        if not c.passed:
            continue
        if c.symbol in seen:
            continue
        sector = rd.universe[c.symbol].industry
        if sum(1 for x in chosen if rd.universe[x.symbol].industry == sector) >= cfg.max_ideas_per_sector:
            notes.append((c.symbol, f"sector cap: already holding a {sector} idea"))
            continue
        corr = [(x.symbol, return_correlation(rd, c.symbol, x.symbol, d)) for x in chosen]
        hi = [(s, r) for s, r in corr if r > cfg.max_pair_correlation]
        if hi:
            notes.append((c.symbol, f"60-day return correlation {hi[0][1]:.2f} with {hi[0][0]}"))
            continue
        if sizer is not None:
            sz = sizer(c)
            c.sizing = sz.as_dict()
            if not sz.executable:
                notes.append((c.symbol, f"{c.setup.side} {c.setup.setup} not executable: {sz.note}"))
                continue
        if event_check is not None:
            ec = event_check(c)
            c.events = {"summary": ec.summary(), "events": ec.events, "all": ec.all_events,
                        "sources_ok": ec.sources_ok, "sources_failed": ec.sources_failed,
                        "window": [str(ec.window_start), str(ec.window_end)]}
            if ec.hard_conflict:
                c.passed = False
                c.reject_reason = "event risk: " + ec.summary()
                notes.append((c.symbol, c.reject_reason))
                continue
        seen.add(c.symbol)
        chosen.append(c)
    return chosen, notes
