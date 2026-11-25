"""Data-quality checks on daily OHLCV. A failed hard check excludes the symbol; it is never patched."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd


@dataclass
class ValidationReport:
    symbol: str
    n_bars: int
    first: date | None
    last: date | None
    missing_sessions: list[date] = field(default_factory=list)  # in reference calendar, absent here
    extra_sessions: list[date] = field(default_factory=list)  # here, absent from reference calendar
    bad_ohlc: int = 0
    nonpositive: int = 0
    duplicates: int = 0
    zero_volume_days: int = 0
    big_moves: list[tuple[date, float]] = field(default_factory=list)  # |close/prev close - 1| > 20%
    stale_days: int = 0  # reference calendar sessions after this symbol's last bar
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_row(self) -> dict:
        return {"symbol": self.symbol, "bars": self.n_bars, "first": self.first, "last": self.last,
                "missing": len(self.missing_sessions), "bad_ohlc": self.bad_ohlc, "zero_vol": self.zero_volume_days,
                "big_moves": len(self.big_moves), "stale_days": self.stale_days, "ok": self.ok,
                "errors": "; ".join(self.errors), "warnings": "; ".join(self.warnings)}


def validate(symbol: str, df: pd.DataFrame, calendar: pd.DatetimeIndex | None = None,
             max_missing_frac: float = 0.02, max_stale_days: int = 1, is_index: bool = False) -> ValidationReport:
    rep = ValidationReport(symbol, len(df), df.index[0].date() if len(df) else None, df.index[-1].date() if len(df) else None)
    if df.empty:
        rep.errors.append("no data")
        return rep
    rep.duplicates = int(df.index.duplicated().sum())
    if rep.duplicates:
        rep.errors.append(f"{rep.duplicates} duplicate dates")
    if not df.index.is_monotonic_increasing:
        rep.errors.append("dates not sorted")
    px = df[["open", "high", "low", "close"]]
    rep.nonpositive = int((px <= 0).any(axis=1).sum())
    if rep.nonpositive:
        rep.errors.append(f"{rep.nonpositive} bars with non-positive prices")
    bad = (df["high"] < px.max(axis=1) - 1e-9) | (df["low"] > px.min(axis=1) + 1e-9)
    rep.bad_ohlc = int(bad.sum())
    if rep.bad_ohlc:
        rep.errors.append(f"{rep.bad_ohlc} bars where high/low do not bound open/close")
    if not is_index:
        zv = df["volume"] <= 0
        rep.zero_volume_days = int(zv.sum())
        if int(zv.iloc[-60:].sum()):
            rep.warnings.append(f"{int(zv.iloc[-60:].sum())} zero-volume sessions in the last 60")
    r = df["close"].pct_change().abs()
    rep.big_moves = [(d.date(), float(v)) for d, v in r[r > 0.20].items()]
    if rep.big_moves:
        rep.warnings.append(f"{len(rep.big_moves)} daily moves >20% (check for unadjusted corporate action)")
    if calendar is not None and len(calendar):
        cal = calendar[(calendar >= df.index[0]) & (calendar <= calendar[-1])]
        have = df.index
        missing = cal.difference(have)
        rep.missing_sessions = [d.date() for d in missing if d <= have[-1]]
        rep.extra_sessions = [d.date() for d in have.difference(calendar)]
        rep.stale_days = int((cal > have[-1]).sum())
        span = int(((cal >= have[0]) & (cal <= have[-1])).sum())
        if span and len(rep.missing_sessions) / span > max_missing_frac:
            rep.errors.append(f"{len(rep.missing_sessions)} of {span} sessions missing")
        elif rep.missing_sessions:
            rep.warnings.append(f"{len(rep.missing_sessions)} sessions missing")
        if rep.stale_days > max_stale_days:
            rep.errors.append(f"stale: last bar {have[-1].date()}, {rep.stale_days} sessions behind the market calendar")
    return rep


def gap_report(df: pd.DataFrame, min_gap_pct: float = 2.0) -> pd.DataFrame:
    """Opening gaps vs the prior close larger than `min_gap_pct`, and whether each was filled the same day."""
    prev = df["close"].shift(1)
    gap = (df["open"] / prev - 1) * 100
    up = gap >= min_gap_pct
    dn = gap <= -min_gap_pct
    out = pd.DataFrame({"gap_pct": gap, "prev_close": prev, "open": df["open"], "low": df["low"], "high": df["high"]})
    out = out[up | dn].copy()
    out["filled_same_day"] = np.where(out["gap_pct"] > 0, out["low"] <= out["prev_close"], out["high"] >= out["prev_close"])
    return out
