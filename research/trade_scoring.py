"""Transparent 100-point score.

  Trend structure 20 | Momentum 15 | Volume 15 | Relative strength 15 | Price action 15 | Risk/reward 10 | Regime 10

Each component returns its points and the reasons behind them ("+3 close above EMA20", "-0 RSI 78 overbought").
Scores are never adjusted to reach a quota of ideas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.market_regime import REGIME_POINTS
from research.trade_generator import Setup

WEIGHTS = {"Trend structure": 20, "Momentum": 15, "Volume confirmation": 15, "Relative strength": 15,
           "Price action": 15, "Risk/reward": 10, "Market regime alignment": 10}


@dataclass
class Component:
    name: str
    points: float
    max_points: float
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)

    def add(self, ok: bool, pts: float, yes: str, no: str) -> None:
        if ok:
            self.points += pts
            self.pros.append(f"+{pts:g} {yes}")
        else:
            self.cons.append(f"0/{pts:g} {no}")


@dataclass
class ScoreCard:
    total: float
    components: list[Component]

    def as_dict(self) -> dict:
        return {"total": round(self.total, 1), "components": [
            {"name": c.name, "points": round(c.points, 1), "max": c.max_points, "pros": c.pros, "cons": c.cons}
            for c in self.components]}


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def score(s: Setup, df: pd.DataFrame, t: int, rs: pd.Series, regime_label: str,
          sector_rs20: float | None = None) -> ScoreCard:
    """`df` has indicator columns; `rs` is the row of the relative-strength table at t."""
    r = df.iloc[t]
    L = s.side == "LONG"
    sgn = 1 if L else -1
    c, e20, e50, s200 = _f(r.close), _f(r.ema20), _f(r.ema50), _f(r.sma200)

    # ---------------------------------------------------------------- trend (20)
    tr = Component("Trend structure", 0, 20)
    tr.add(sgn * (c - e20) > 0, 3, f"close {'above' if L else 'below'} EMA20", f"close on the wrong side of EMA20")
    tr.add(sgn * (e20 - e50) > 0, 4, f"EMA20 {'above' if L else 'below'} EMA50", "EMA20/EMA50 not aligned with the trade")
    tr.add(sgn * (e50 - s200) > 0, 4, f"EMA50 {'above' if L else 'below'} SMA200 (primary trend aligned)", "EMA50 vs SMA200 against the trade")
    tr.add(sgn * (c - s200) > 0, 3, f"price {'above' if L else 'below'} SMA200", "price on the wrong side of SMA200")
    sl = _f(df["ema50"].iloc[t] / df["ema50"].iloc[t - 20] - 1) * 100
    tr.add(sgn * sl > 0, 3, f"EMA50 sloping {'up' if L else 'down'} ({sl:+.1f}% / 20d)", f"EMA50 slope {sl:+.1f}% against the trade")
    adx, pdi, mdi = _f(r.adx), _f(r.plus_di), _f(r.minus_di)
    tr.add(adx >= 20 and sgn * (pdi - mdi) > 0, 3, f"ADX {adx:.0f} with {'+DI' if L else '-DI'} dominant",
           f"ADX {adx:.0f} / DI not confirming a directional trend")

    # ---------------------------------------------------------------- momentum (15)
    mo = Component("Momentum", 0, 15)
    rsi = _f(r.rsi)
    pull = s.setup.startswith(("Pullback", "Support", "Bearish trend continuation (EMA", "Resistance"))
    if L:
        good = (40 <= rsi <= 62) if pull else (52 <= rsi <= 72)
        mo.add(good, 6, f"RSI {rsi:.0f} in the {'reset' if pull else 'momentum'} zone for this setup",
               f"RSI {rsi:.0f} outside the {'40-62 reset' if pull else '52-72 momentum'} zone" + (" (overbought)" if rsi > 72 else ""))
    else:
        good = (38 <= rsi <= 60) if pull else (28 <= rsi <= 48)
        mo.add(good, 6, f"RSI {rsi:.0f} in the {'rejection' if pull else 'weakness'} zone for this setup",
               f"RSI {rsi:.0f} outside the {'38-60' if pull else '28-48'} zone" + (" (oversold)" if rsi < 28 else ""))
    mh, mh1 = _f(r.macd_hist), _f(df["macd_hist"].iloc[t - 1])
    mo.add(sgn * (mh - mh1) > 0, 5, f"MACD histogram turning {'up' if L else 'down'} ({mh1:+.2f} -> {mh:+.2f})",
           f"MACD histogram moving against the trade ({mh1:+.2f} -> {mh:+.2f})")
    roc = _f(r.roc10)
    mo.add(sgn * roc > 0 if not pull else sgn * _f(r.roc20) > -5, 4,
           f"10-day rate of change {roc:+.1f}%" if not pull else f"20-day ROC {_f(r.roc20):+.1f}% (trend intact through the pullback)",
           f"rate of change against the trade (10d {roc:+.1f}%, 20d {_f(r.roc20):+.1f}%)")

    # ---------------------------------------------------------------- volume (15)
    vo = Component("Volume confirmation", 0, 15)
    rvol = _f(r.rvol)
    need = 1.5 if s.setup in ("Breakout", "Breakdown") else 1.1
    vo.add(rvol >= need, 7, f"signal-day volume {rvol:.1f}x the 20-day average", f"signal-day volume only {rvol:.1f}x average (want >= {need})")
    obv, obv_e = _f(r.obv), _f(r.obv_ema20)
    vo.add(sgn * (obv - obv_e) > 0, 4, f"OBV {'above' if L else 'below'} its 20-day EMA (accumulation)" if L else "OBV below its 20-day EMA (distribution)",
           "OBV trend not confirming")
    from research.pattern_detection import up_down_volume_ratio
    udr = up_down_volume_ratio(df, t, 20)
    vo.add(udr > 1.1 if L else udr < 0.9, 4, f"up/down volume ratio {udr:.2f} over 20 sessions",
           f"up/down volume ratio {udr:.2f} does not favour the trade")

    # ---------------------------------------------------------------- relative strength (15)
    rsc = Component("Relative strength", 0, 15)
    x20, x60 = _f(rs.get("rs_nifty20")), _f(rs.get("rs_nifty60"))
    rsc.add(sgn * x20 > 0, 5, f"{x20:+.1f}% vs NIFTY 50 over 20 sessions", f"{x20:+.1f}% vs NIFTY 50 over 20 sessions")
    rsc.add(sgn * x60 > 0, 4, f"{x60:+.1f}% vs NIFTY 50 over 60 sessions", f"{x60:+.1f}% vs NIFTY 50 over 60 sessions")
    xs = _f(rs.get("rs_sector20"))
    rsc.add(not np.isnan(xs) and sgn * xs > 0, 3, f"{xs:+.1f}% vs its sector index (20d)", f"{xs:+.1f}% vs its sector index (20d)")
    sec = _f(sector_rs20) if sector_rs20 is not None else float("nan")
    rsc.add(not np.isnan(sec) and sgn * sec > 0, 3, f"sector {'outperforming' if L else 'underperforming'} NIFTY ({sec:+.1f}%, 20d)",
            f"sector {sec:+.1f}% vs NIFTY (20d) works against the trade")

    # ---------------------------------------------------------------- price action (15)
    pa = Component("Price action", 0, 15)
    cl = _f(s.features.get("close_location"))
    pa.add(cl >= 0.6 if L else cl <= 0.4, 4, f"closed in the {'upper' if L else 'lower'} part of the day's range ({cl:.2f})",
           f"weak close location {cl:.2f}")
    from research.pattern_detection import BEARISH, BULLISH
    cand = set(s.features.get("candles", []))
    hit = cand & (BULLISH if L else BEARISH)
    pa.add(bool(hit), 4, f"confirming candle: {', '.join(sorted(hit))}", "no confirming candlestick pattern")
    pa.add(s.stop_atr <= 2.0, 4, f"tight structure: stop {s.stop_atr:.1f} ATR from entry", f"loose structure: stop {s.stop_atr:.1f} ATR from entry")
    ext = sgn * (s.entry - e20) / s.atr
    pa.add(ext <= 2.0, 3, f"entry {ext:.1f} ATR from EMA20 (not extended)", f"entry {ext:.1f} ATR from EMA20 (extended)")

    # ---------------------------------------------------------------- risk/reward (10)
    rr = Component("Risk/reward", 0, 10)
    q = s.rr_t2
    pts = 10 if q >= 3 else 8 if q >= 2.5 else 6 if q >= 2 else 3 if q >= 1.5 else 0
    rr.points = pts
    (rr.pros if pts >= 6 else rr.cons).append(f"{pts}/10 reward:risk {s.rr_t1:.2f} to T1, {s.rr_t2:.2f} to T2")
    if "projection" in s.t2_basis:
        rr.points = max(0, rr.points - 2)
        rr.cons.append("-2 T2 is a volatility projection, not a chart level")

    # ---------------------------------------------------------------- regime (10)
    rg = Component("Market regime alignment", 0, 10)
    rg.points = REGIME_POINTS[s.side].get(regime_label, 5)
    (rg.pros if rg.points >= 5 else rg.cons).append(f"{rg.points}/10 {s.side.lower()} idea in a {regime_label} market")

    comps = [tr, mo, vo, rsc, pa, rr, rg]
    return ScoreCard(sum(c.points for c in comps), comps)
