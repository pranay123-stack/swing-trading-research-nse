"""Swing pivots and horizontal support/resistance levels.

A pivot high at bar i is a bar whose high is the highest of bars [i-k, i+k]. It can only be KNOWN at bar i+k, so every
function that asks "what levels exist at bar t" only uses pivots with i <= t - k. This is what keeps the backtest free of
look-ahead from pivot detection.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Pivot:
    i: int  # bar position
    d: pd.Timestamp
    price: float
    kind: str  # "H" or "L"
    left: int = 0  # bars to the left it dominates (capped at 50)
    right: int = 0  # bars to the right it dominates in the FULL series (capped at 50)

    def strength_at(self, t: int) -> int:
        """How many bars on each side this pivot dominates as known at bar t (right side truncated at t: causal)."""
        return min(self.left, self.right, t - self.i)


@dataclass
class Level:
    price: float
    touches: int
    first: pd.Timestamp
    last: pd.Timestamp
    kinds: str  # which pivot kinds formed it, e.g. "HL" = acted as both resistance and support
    strength: int = 0  # strongest member pivot's dominance (bars each side) as known at evaluation time

    @property
    def major(self) -> bool:
        """A level that matters for targets: tested more than once, or a swing that dominated >= 10 bars each side."""
        return self.touches >= 2 or self.strength >= MAJOR_STRENGTH

    @property
    def role_flip(self) -> bool:
        return "H" in self.kinds and "L" in self.kinds


MAJOR_STRENGTH = 10
_CAP = 50


def _dominance(x: np.ndarray, i: int, step: int, higher: bool) -> int:
    n = 0
    j = i + step
    while 0 <= j < len(x) and n < _CAP:
        if (x[j] > x[i]) if higher else (x[j] < x[i]):
            break
        n += 1
        j += step
    return n


def find_pivots(df: pd.DataFrame, k: int = 5) -> list[Pivot]:
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    n = len(df)
    out: list[Pivot] = []
    for i in range(k, n - k):
        wh = h[i - k:i + k + 1]
        wl = l[i - k:i + k + 1]
        if h[i] == wh.max() and np.argmax(wh) == k:
            out.append(Pivot(i, df.index[i], float(h[i]), "H", _dominance(h, i, -1, True), _dominance(h, i, 1, True)))
        if l[i] == wl.min() and np.argmin(wl) == k:
            out.append(Pivot(i, df.index[i], float(l[i]), "L", _dominance(l, i, -1, False), _dominance(l, i, 1, False)))
    return out


def known_pivots(pivots: list[Pivot], t: int, k: int = 5, lookback: int = 250) -> list[Pivot]:
    """Pivots confirmed by bar t (i + k <= t) within the lookback window."""
    return [p for p in pivots if p.i + k <= t and p.i >= t - lookback]


def cluster_levels(pivots: list[Pivot], tolerance: float, t: int | None = None) -> list[Level]:
    """Group pivot prices within `tolerance` (absolute price, usually 0.5 x ATR) into levels."""
    if not pivots:
        return []
    ps = sorted(pivots, key=lambda p: p.price)
    groups: list[list[Pivot]] = [[ps[0]]]
    for p in ps[1:]:
        if p.price - np.mean([q.price for q in groups[-1]]) <= tolerance:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        out.append(Level(
            price=float(np.mean([p.price for p in g])),
            touches=len(g),
            first=min(p.d for p in g),
            last=max(p.d for p in g),
            kinds="".join(sorted({p.kind for p in g})),
            strength=max(p.strength_at(t) if t is not None else min(p.left, p.right) for p in g),
        ))
    return out


def levels_at(df: pd.DataFrame, pivots: list[Pivot], t: int, atr: float, k: int = 5, lookback: int = 250) -> list[Level]:
    return cluster_levels(known_pivots(pivots, t, k, lookback), tolerance=0.5 * atr, t=t)


def nearest_above(levels: list[Level], price: float, min_gap: float = 0.0) -> Level | None:
    c = [lv for lv in levels if lv.price > price + min_gap]
    return min(c, key=lambda lv: lv.price) if c else None


def nearest_below(levels: list[Level], price: float, min_gap: float = 0.0) -> Level | None:
    c = [lv for lv in levels if lv.price < price - min_gap]
    return max(c, key=lambda lv: lv.price) if c else None


def levels_above(levels: list[Level], price: float) -> list[Level]:
    return sorted((lv for lv in levels if lv.price > price), key=lambda lv: lv.price)


def levels_below(levels: list[Level], price: float) -> list[Level]:
    return sorted((lv for lv in levels if lv.price < price), key=lambda lv: -lv.price)


def last_swing(pivots: list[Pivot], t: int, kind: str, k: int = 5) -> Pivot | None:
    c = [p for p in pivots if p.kind == kind and p.i + k <= t]
    return c[-1] if c else None
