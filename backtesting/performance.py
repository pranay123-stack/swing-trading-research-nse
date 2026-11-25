"""Performance metrics. All P&L figures are net of transaction costs and slippage."""

from __future__ import annotations

import numpy as np
import pandas as pd


def trade_metrics(trades: pd.DataFrame) -> dict:
    t = trades[trades["entry_price"] > 0] if len(trades) else trades
    n = len(t)
    if n == 0:
        return {"trades": 0}
    wins = t[t["net"] > 0]
    losses = t[t["net"] <= 0]
    gp, gl = float(wins["net"].sum()), float(-losses["net"].sum())
    r = t["r"].dropna()
    se = float(r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else float("nan")
    return {
        "trades": n,
        "win_rate": len(wins) / n * 100,
        "avg_win": float(wins["net"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["net"].mean()) if len(losses) else 0.0,
        "profit_factor": gp / gl if gl > 0 else float("inf"),
        "expectancy": float(t["net"].mean()),
        "expectancy_r": float(r.mean()) if len(r) else float("nan"),
        "expectancy_r_se": se,
        "expectancy_r_t": float(r.mean() / se) if se and se > 0 else float("nan"),
        "avg_holding": float(t["bars_held"].mean()),
        "median_holding": float(t["bars_held"].median()),
        "total_net": float(t["net"].sum()),
        "total_costs": float(t["costs"].sum()),
        "exit_reasons": t["exit_reason"].value_counts().to_dict(),
    }


def fill_rate(trades: pd.DataFrame) -> float:
    if not len(trades):
        return float("nan")
    return float((trades["entry_price"] > 0).mean() * 100)


def equity_metrics(equity: pd.Series, capital: float) -> dict:
    if equity.empty:
        return {}
    ret = equity.pct_change().dropna()
    peak = equity.cummax()
    dd = (equity / peak - 1) * 100
    years = max(len(equity) / 252, 1e-9)
    total = (equity.iloc[-1] / capital - 1) * 100
    sharpe = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252)) if ret.std(ddof=1) > 0 else float("nan")
    return {
        "total_return_pct": float(total),
        "cagr_pct": float(((equity.iloc[-1] / capital) ** (1 / years) - 1) * 100),
        "max_drawdown_pct": float(dd.min()),
        "sharpe": sharpe,
        "sessions": len(equity),
    }


def by_group(trades: pd.DataFrame, col: str) -> pd.DataFrame:
    rows = []
    for k, g in trades[trades["entry_price"] > 0].groupby(col):
        m = trade_metrics(g)
        rows.append({col: k, **{x: m[x] for x in ("trades", "win_rate", "profit_factor", "expectancy_r",
                                                  "expectancy_r_t", "median_holding")}})
    return pd.DataFrame(rows).sort_values("trades", ascending=False) if rows else pd.DataFrame()
