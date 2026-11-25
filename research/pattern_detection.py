"""Candlestick patterns, consolidation bases, breakouts/breakdowns, failed breakouts and gaps.

Every function looks at bar position t and earlier only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def _bar(df: pd.DataFrame, t: int):
    r = df.iloc[t]
    return float(r.open), float(r.high), float(r.low), float(r.close)


def close_location(df: pd.DataFrame, t: int) -> float:
    """Where the close sits in the day's range: 1.0 = at the high, 0.0 = at the low."""
    o, h, l, c = _bar(df, t)
    return 0.5 if h == l else (c - l) / (h - l)


def candle_patterns(df: pd.DataFrame, t: int) -> list[str]:
    if t < 1:
        return []
    o, h, l, c = _bar(df, t)
    po, ph, pl, pc = _bar(df, t - 1)
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    out = []
    if c > o and pc < po and c >= po and o <= pc and body > abs(pc - po):
        out.append("bullish_engulfing")
    if c < o and pc > po and c <= po and o >= pc and body > abs(pc - po):
        out.append("bearish_engulfing")
    if lower >= 2 * body and upper <= 0.3 * rng and body / rng < 0.4:
        out.append("hammer")
    if upper >= 2 * body and lower <= 0.3 * rng and body / rng < 0.4:
        out.append("shooting_star")
    if body / rng < 0.1:
        out.append("doji")
    if h <= ph and l >= pl:
        out.append("inside_bar")
    if t >= 6:
        ranges = (df["high"] - df["low"]).iloc[t - 6:t + 1].to_numpy()
        if ranges[-1] == ranges.min():
            out.append("nr7")
    if c > o and close_location(df, t) >= 0.75 and body / rng >= 0.6:
        out.append("strong_bull_close")
    if c < o and close_location(df, t) <= 0.25 and body / rng >= 0.6:
        out.append("strong_bear_close")
    return out


BULLISH = {"bullish_engulfing", "hammer", "strong_bull_close"}
BEARISH = {"bearish_engulfing", "shooting_star", "strong_bear_close"}


@dataclass
class Base:
    start: int
    end: int  # last bar of the base (t-1 when testing a breakout on t)
    high: float
    low: float
    bars: int
    height_atr: float


def consolidation(df: pd.DataFrame, end: int, atr: float, min_bars: int = 6, max_bars: int = 30,
                  max_height_atr: float = 3.5) -> Base | None:
    """Longest window ending at `end` whose high-low range is <= max_height_atr x ATR (a tight base)."""
    best = None
    for n in range(min_bars, max_bars + 1):
        s = end - n + 1
        if s < 0:
            break
        hi = float(df["high"].iloc[s:end + 1].max())
        lo = float(df["low"].iloc[s:end + 1].min())
        if atr > 0 and (hi - lo) / atr <= max_height_atr:
            best = Base(s, end, hi, lo, n, (hi - lo) / atr)
        else:
            break
    return best


def prior_high(df: pd.DataFrame, t: int, n: int) -> float:
    return float(df["high"].iloc[max(0, t - n):t].max())


def prior_low(df: pd.DataFrame, t: int, n: int) -> float:
    return float(df["low"].iloc[max(0, t - n):t].min())


def is_breakout(df: pd.DataFrame, t: int, n: int = 20) -> bool:
    return float(df["close"].iloc[t]) > prior_high(df, t, n)


def is_breakdown(df: pd.DataFrame, t: int, n: int = 20) -> bool:
    return float(df["close"].iloc[t]) < prior_low(df, t, n)


def failed_breakout(df: pd.DataFrame, t: int, n: int = 20, within: int = 5) -> tuple[bool, float, int]:
    """In the last `within` bars price traded above the prior n-bar high but has now closed back below it,
    and today closed below the low of the bar that made the excursion. Returns (flag, level, excursion bar)."""
    for j in range(t - 1, max(n, t - within) - 1, -1):
        lvl = prior_high(df, j, n)
        if float(df["high"].iloc[j]) > lvl:
            c = float(df["close"].iloc[t])
            if c < lvl and c < float(df["low"].iloc[j]) and float(df["high"].iloc[j + 1:t + 1].max()) <= float(df["high"].iloc[j]):
                return True, lvl, j
            return False, lvl, j
    return False, float("nan"), -1


def failed_breakdown(df: pd.DataFrame, t: int, n: int = 20, within: int = 5) -> tuple[bool, float, int]:
    for j in range(t - 1, max(n, t - within) - 1, -1):
        lvl = prior_low(df, j, n)
        if float(df["low"].iloc[j]) < lvl:
            c = float(df["close"].iloc[t])
            if c > lvl and c > float(df["high"].iloc[j]) and float(df["low"].iloc[j + 1:t + 1].min()) >= float(df["low"].iloc[j]):
                return True, lvl, j
            return False, lvl, j
    return False, float("nan"), -1


def gap_pct(df: pd.DataFrame, t: int) -> float:
    if t < 1:
        return 0.0
    return (float(df["open"].iloc[t]) / float(df["close"].iloc[t - 1]) - 1) * 100


def unfilled_gaps(df: pd.DataFrame, t: int, lookback: int = 60, min_pct: float = 1.5) -> list[dict]:
    """Gaps in the last `lookback` bars that price has not traded back through since (they can act as support/resistance)."""
    out = []
    for j in range(max(1, t - lookback), t + 1):
        g = gap_pct(df, j)
        if abs(g) < min_pct:
            continue
        pc = float(df["close"].iloc[j - 1])
        after = df.iloc[j:t + 1]
        if g > 0 and float(after["low"].min()) > pc:
            out.append({"d": df.index[j], "dir": "up", "gap_pct": g, "fill_level": pc})
        if g < 0 and float(after["high"].max()) < pc:
            out.append({"d": df.index[j], "dir": "down", "gap_pct": g, "fill_level": pc})
    return out


def up_down_volume_ratio(df: pd.DataFrame, t: int, n: int = 20) -> float:
    w = df.iloc[max(1, t - n + 1):t + 1]
    chg = df["close"].diff().iloc[max(1, t - n + 1):t + 1]
    up = float(w["volume"][chg > 0].sum())
    dn = float(w["volume"][chg < 0].sum())
    return up / dn if dn > 0 else (np.inf if up > 0 else 1.0)
