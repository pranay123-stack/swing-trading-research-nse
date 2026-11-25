"""Sector rotation from NSE sector indices (all served by the Kotak historical API)."""

from __future__ import annotations

import pandas as pd

from research.indicators import add_indicators
from research.relative_strength import excess_return


def sector_table(index_frames: dict[str, pd.DataFrame], bench: str = "Nifty 50", t: pd.Timestamp | None = None,
                 exclude: tuple[str, ...] = ("INDIA VIX",)) -> pd.DataFrame:
    """One row per index on date t: returns, RS vs NIFTY, trend position and a rotation quadrant."""
    b = index_frames[bench]["close"]
    rows = []
    for name, df in index_frames.items():
        if name in exclude or df.empty:
            continue
        x = df if "ema50" in df.columns else add_indicators(df)
        x = x.loc[:t] if t is not None else x
        if len(x) < 60:
            continue
        c = x["close"]
        last = x.iloc[-1]
        rs20 = float(excess_return(c, b, 20).iloc[-1])
        rs60 = float(excess_return(c, b, 60).iloc[-1])
        rs20_prev = float(excess_return(c, b, 20).iloc[-6]) if len(c) > 30 else float("nan")
        momentum = rs20 - rs20_prev  # is relative strength improving over the last week?
        if rs60 >= 0 and momentum >= 0:
            quad = "Leading"
        elif rs60 >= 0:
            quad = "Weakening"
        elif momentum >= 0:
            quad = "Improving"
        else:
            quad = "Lagging"
        rows.append({
            "index": name,
            "close": float(last.close),
            "ret5": float(c.pct_change(5).iloc[-1] * 100),
            "ret20": float(c.pct_change(20).iloc[-1] * 100),
            "ret60": float(c.pct_change(60).iloc[-1] * 100),
            "rs_nifty20": rs20,
            "rs_nifty60": rs60,
            "rs_momentum": momentum,
            "above_ema50": bool(last.close > last.ema50),
            "above_sma200": bool(last.close > last.sma200) if pd.notna(last.sma200) else None,
            "rsi": float(last.rsi),
            "quadrant": quad,
        })
    return pd.DataFrame(rows).sort_values("rs_nifty20", ascending=False).reset_index(drop=True)
