"""Market regime from NIFTY 50, NIFTY BANK, NIFTY MIDCAP 100, universe breadth and INDIA VIX.

Every component contributes -2..+2 points with a one-line reason. The sum is scaled to -1..+1 and mapped:
  >= +0.55 Strong Bullish | >= +0.20 Bullish | > -0.20 Sideways | > -0.55 Bearish | else Strong Bearish
The thresholds are fixed by design (not fitted) and are the same in the backtest and in the live run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LABELS = ("Strong Bearish", "Bearish", "Sideways", "Bullish", "Strong Bullish")

# How compatible each direction is with each regime (used by the scorer, 0..10 points).
REGIME_POINTS = {
    "LONG": {"Strong Bullish": 10, "Bullish": 8, "Sideways": 5, "Bearish": 2, "Strong Bearish": 0},
    "SHORT": {"Strong Bullish": 0, "Bullish": 2, "Sideways": 5, "Bearish": 8, "Strong Bearish": 10},
}


@dataclass
class Regime:
    label: str
    score: float  # -1..+1
    components: list[tuple[str, int, str]] = field(default_factory=list)  # (name, points, reason)
    breadth_ema50: float = float("nan")
    breadth_sma200: float = float("nan")
    vix: float = float("nan")

    def allows(self, side: str) -> bool:
        """Longs are not taken in Strong Bearish; shorts are not taken in Strong Bullish."""
        return not ((side == "LONG" and self.label == "Strong Bearish") or (side == "SHORT" and self.label == "Strong Bullish"))


def label_for(score: float) -> str:
    if score >= 0.55:
        return "Strong Bullish"
    if score >= 0.20:
        return "Bullish"
    if score > -0.20:
        return "Sideways"
    if score > -0.55:
        return "Bearish"
    return "Strong Bearish"


def _trend_points(name: str, x: pd.DataFrame, full: bool) -> list[tuple[str, int, str]]:
    r = x.iloc[-1]
    out = []
    c, e20, e50, s200 = float(r.close), float(r.ema20), float(r.ema50), float(r.sma200)
    slope50 = float(x["ema50"].iloc[-1] / x["ema50"].iloc[-21] - 1) * 100 if len(x) > 21 else 0.0
    if full:
        p = (1 if c > e20 else -1) + (1 if e20 > e50 else -1)
        out.append((f"{name} short-term trend", p,
                    f"close {c:,.0f} {'>' if c > e20 else '<'} EMA20 {e20:,.0f}; EMA20 {'>' if e20 > e50 else '<'} EMA50 {e50:,.0f}"))
        p = (1 if c > s200 else -1) + (1 if slope50 > 0 else -1)
        out.append((f"{name} primary trend", p,
                    f"close {'above' if c > s200 else 'below'} SMA200 {s200:,.0f}; EMA50 20-day slope {slope50:+.2f}%"))
    else:
        p = (1 if c > e50 else -1) + (1 if e20 > e50 else -1)
        out.append((f"{name} trend", p,
                    f"close {'above' if c > e50 else 'below'} EMA50; EMA20 {'>' if e20 > e50 else '<'} EMA50"))
    return out


def classify(indices: dict[str, pd.DataFrame], t: pd.Timestamp, breadth50: pd.Series | None = None,
             breadth200: pd.Series | None = None) -> Regime:
    """`indices` frames must already carry indicator columns. Only data up to and including t is used."""
    comps: list[tuple[str, int, str]] = []
    nifty = indices["Nifty 50"].loc[:t]
    comps += _trend_points("NIFTY 50", nifty, full=True)
    for name, label in (("Nifty Bank", "NIFTY BANK"), ("NIFTY MIDCAP 100", "NIFTY MIDCAP 100")):
        if name in indices and len(indices[name].loc[:t]) > 60:
            comps += _trend_points(label, indices[name].loc[:t], full=False)
    b50 = b200 = float("nan")
    if breadth50 is not None and len(breadth50.loc[:t]):
        b50 = float(breadth50.loc[:t].iloc[-1])
        p = 2 if b50 >= 65 else 1 if b50 >= 50 else -1 if b50 >= 35 else -2
        comps.append(("Breadth (EMA50)", p, f"{b50:.0f}% of universe stocks close above their 50-day EMA"))
    if breadth200 is not None and len(breadth200.loc[:t]):
        b200 = float(breadth200.loc[:t].iloc[-1])
        p = 1 if b200 >= 55 else -1 if b200 < 45 else 0
        comps.append(("Breadth (SMA200)", p, f"{b200:.0f}% of universe stocks close above their 200-day SMA"))
    vix = float("nan")
    if "INDIA VIX" in indices and len(indices["INDIA VIX"].loc[:t]) > 6:
        v = indices["INDIA VIX"].loc[:t]["close"]
        vix = float(v.iloc[-1])
        ch5 = float(v.iloc[-1] / v.iloc[-6] - 1) * 100
        if vix >= 20 or ch5 >= 20:
            p = -2
        elif vix >= 16 or ch5 >= 10:
            p = -1
        elif vix <= 13 and ch5 <= 0:
            p = 1
        else:
            p = 0
        comps.append(("INDIA VIX", p, f"VIX {vix:.2f}, {ch5:+.1f}% over 5 sessions"))
    max_pts = sum(1 if n.startswith(("Breadth (SMA200)", "INDIA VIX")) else 2 for n, _, _ in comps)
    score = sum(p for _, p, _ in comps) / max_pts if max_pts else 0.0
    return Regime(label_for(score), float(score), comps, b50, b200, vix)


def breadth_series(stock_frames: dict[str, pd.DataFrame], col: str = "ema50") -> pd.Series:
    """% of stocks whose close is above `col`, per date (only stocks with a valid value that day are counted)."""
    above = {}
    for s, df in stock_frames.items():
        if col in df.columns:
            ok = df[col].notna()
            above[s] = (df["close"] > df[col]).astype(float).where(ok)
    m = pd.DataFrame(above)
    return (m.sum(axis=1) / m.notna().sum(axis=1) * 100).where(m.notna().sum(axis=1) >= 20)


def regime_series(indices: dict[str, pd.DataFrame], dates: pd.DatetimeIndex, breadth50: pd.Series,
                  breadth200: pd.Series) -> pd.Series:
    out = {}
    for d in dates:
        try:
            out[d] = classify(indices, d, breadth50, breadth200).label
        except (KeyError, IndexError, ValueError):
            out[d] = np.nan
    return pd.Series(out)
