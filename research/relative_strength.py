"""Relative strength of a stock against NIFTY 50 and against its sector index."""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (5, 10, 20, 60)


def excess_return(stock: pd.Series, bench: pd.Series, n: int) -> pd.Series:
    """(1 + r_stock) / (1 + r_bench) - 1 over n sessions, in percent, aligned on the stock's dates."""
    b = bench.reindex(stock.index).ffill()
    return ((stock / stock.shift(n)) / (b / b.shift(n)) - 1) * 100


def rs_line(stock: pd.Series, bench: pd.Series) -> pd.Series:
    b = bench.reindex(stock.index).ffill()
    return stock / b


def mansfield_rs(stock: pd.Series, bench: pd.Series, n: int = 50) -> pd.Series:
    """RS line relative to its own n-day mean, in percent. > 0 = outperforming its recent norm."""
    line = rs_line(stock, bench)
    return (line / line.rolling(n).mean() - 1) * 100


def rs_table(stock: pd.Series, bench: pd.Series, sector: pd.Series | None) -> pd.DataFrame:
    cols = {}
    for n in HORIZONS:
        cols[f"ret{n}"] = (stock / stock.shift(n) - 1) * 100
        cols[f"rs_nifty{n}"] = excess_return(stock, bench, n)
        if sector is not None:
            cols[f"rs_sector{n}"] = excess_return(stock, sector, n)
    cols["mansfield"] = mansfield_rs(stock, bench)
    cols["rs_line_slope20"] = rs_line(stock, bench).pct_change(20) * 100
    return pd.DataFrame(cols, index=stock.index)


def percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional percentile (0-100) of a metric across the universe on one date."""
    s = pd.Series(values, dtype=float).dropna()
    if s.empty:
        return {}
    r = s.rank(pct=True) * 100
    return {k: float(v) for k, v in r.items()} | {k: np.nan for k in values if k not in r}
