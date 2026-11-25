"""Walk-forward validation.

The only parameter chosen from data is the minimum score threshold, picked from a small fixed grid on each TRAIN
window (by mean net R of the isolated-signal simulation, needing >= MIN_TRAIN_TRADES trades), then applied unchanged to
the following TEST window. Out-of-sample results are the concatenation of the test windows only. Everything else
(indicator periods, setup rules, stop/target construction, costs) is fixed a priori and never fitted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtesting.performance import trade_metrics

GRID = (50.0, 55.0, 60.0, 65.0, 70.0)
MIN_TRAIN_TRADES = 20
DEFAULT_THRESHOLD = 60.0


@dataclass
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    threshold: float
    train_trades: int
    train_exp_r: float
    test_trades: int
    test_exp_r: float


def folds(calendar: pd.DatetimeIndex, first: pd.Timestamp, train: int = 250, test: int = 63) -> list[tuple]:
    cal = calendar[calendar >= first]
    out = []
    i = 0
    while i + train + 1 < len(cal):
        tr_s, tr_e = cal[i], cal[i + train - 1]
        te_s = cal[i + train]
        te_e = cal[min(i + train + test - 1, len(cal) - 1)]
        out.append((tr_s, tr_e, te_s, te_e))
        i += test
    return out


def _exp_r(t: pd.DataFrame) -> tuple[int, float]:
    f = t[t["entry_price"] > 0]
    return len(f), float(f["r"].mean()) if len(f) else float("nan")


def walk_forward(trades: pd.DataFrame, calendar: pd.DatetimeIndex, first: pd.Timestamp,
                 train: int = 250, test: int = 63) -> tuple[list[Fold], pd.DataFrame]:
    """`trades` = isolated-signal simulation of ALL signals (from engine.simulate_signals), regime filter applied."""
    fs, oos = [], []
    for tr_s, tr_e, te_s, te_e in folds(calendar, first, train, test):
        # a train trade must have CLOSED before the test window starts, otherwise its outcome is future information
        tr = trades[(trades["signal_d"] >= tr_s) & (trades["signal_d"] <= tr_e)]
        tr = tr[tr["exit_d"].isna() | (pd.to_datetime(tr["exit_d"]) < te_s)]
        best, best_val, best_n = DEFAULT_THRESHOLD, -np.inf, 0
        for th in GRID:
            n, e = _exp_r(tr[tr["score"] >= th])
            if n >= MIN_TRAIN_TRADES and e > best_val:
                best, best_val, best_n = th, e, n
        if best_val == -np.inf:
            best_n, best_val = _exp_r(tr[tr["score"] >= best])
        te = trades[(trades["signal_d"] >= te_s) & (trades["signal_d"] <= te_e) & (trades["score"] >= best)]
        n, e = _exp_r(te)
        fs.append(Fold(tr_s, tr_e, te_s, te_e, best, best_n, best_val, n, e))
        oos.append(te.assign(fold_threshold=best))
    return fs, (pd.concat(oos) if oos else pd.DataFrame())


def summary(fs: list[Fold], oos: pd.DataFrame) -> dict:
    m = trade_metrics(oos) if len(oos) else {"trades": 0}
    m["folds"] = len(fs)
    m["thresholds"] = [f.threshold for f in fs]
    m["positive_folds"] = sum(1 for f in fs if f.test_trades and f.test_exp_r > 0)
    return m
