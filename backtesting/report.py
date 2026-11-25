"""Run the full validation and save it for the PDF / dashboard."""

from __future__ import annotations

import time

import pandas as pd

from backtesting.engine import generate_signals, simulate_portfolio, simulate_signals
from backtesting.performance import by_group, equity_metrics, fill_rate, trade_metrics
from backtesting.walk_forward import DEFAULT_THRESHOLD, folds, summary, walk_forward
from config import PATHS, RISK
from reporting.export import json_dump

WARMUP = 215  # sessions: SMA200 + pivot confirmation
TAIL = 20  # signals in the last 20 sessions cannot have a complete outcome yet


def cost_decomposition(trades: pd.DataFrame) -> list[dict]:
    """Mean R per trade before costs, the cost drag in R, and net R; overall and by side."""
    t = trades[(trades["state"] == "CLOSED") & (trades["entry_price"] > 0)].copy() if len(trades) else trades
    if not len(t):
        return []
    t["gross_r"] = t["gross"] / t["risk"]
    t["cost_r"] = t["costs"] / t["risk"]
    rows = [{"group": "All", "trades": len(t), "gross_r": t["gross_r"].mean(), "cost_r": t["cost_r"].mean(), "net_r": t["r"].mean()}]
    for k, g in t.groupby("side"):
        rows.append({"group": k, "trades": len(g), "gross_r": g["gross_r"].mean(), "cost_r": g["cost_r"].mean(), "net_r": g["r"].mean()})
    return [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def run_backtest(rd, verbose: bool = False) -> dict:
    t0 = time.time()
    cal = rd.calendar
    start, end = cal[WARMUP], cal[-TAIL]

    def prog(i, n, d, k):
        if verbose:
            print(f"    signals {i}/{n} sessions ({d.date()}): {k} setups", flush=True)

    sig = generate_signals(rd, start, end, progress=prog)
    sig.to_csv(PATHS.reports / "backtest_signals.csv", index=False)
    allowed = sig[sig["regime_ok"]] if len(sig) else sig
    iso = simulate_signals(rd, allowed)
    iso.to_csv(PATHS.reports / "backtest_isolated_trades.csv", index=False)
    closed = iso[iso["state"] == "CLOSED"] if len(iso) else iso

    fs, oos = walk_forward(closed, cal, start)
    # portfolio over the out-of-sample span with each fold's threshold
    th = {}
    for f in fs:
        for d in cal[(cal >= f.test_start) & (cal <= f.test_end)]:
            th[d] = f.threshold
    port = None
    if fs:
        port = simulate_portfolio(rd, sig, fs[0].test_start, fs[-1].test_end, th)
        port.trades.to_csv(PATHS.reports / "backtest_portfolio_trades.csv", index=False)
        from reporting.chart_generator import equity_chart
        equity_chart(port.equity, PATHS.reports / "backtest_equity.png",
                     f"Out-of-sample paper portfolio (walk-forward thresholds), Rs {RISK.paper_capital / 1e5:.0f} lakh start")
    fixed = simulate_portfolio(rd, sig, start, end, DEFAULT_THRESHOLD)

    def clean(m: dict) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}

    out = {
        "period": [str(start.date()), str(end.date())],
        "sessions": int(((cal >= start) & (cal <= end)).sum()),
        "signals": int(len(sig)), "signals_regime_ok": int(len(allowed)),
        "fill_rate_pct": fill_rate(iso),
        "isolated_all": clean(trade_metrics(closed)),
        "cost_decomposition": cost_decomposition(iso),
        "isolated_by_setup": by_group(closed, "setup").round(3).to_dict("records") if len(closed) else [],
        "isolated_by_side": by_group(closed, "side").round(3).to_dict("records") if len(closed) else [],
        "isolated_by_regime": by_group(closed, "regime").round(3).to_dict("records") if len(closed) else [],
        "walk_forward": {"summary": clean(summary(fs, oos)),
                         "folds": [{"train": f"{f.train_start.date()}..{f.train_end.date()}",
                                    "test": f"{f.test_start.date()}..{f.test_end.date()}", "threshold": f.threshold,
                                    "train_trades": f.train_trades, "train_exp_r": round(f.train_exp_r, 3),
                                    "test_trades": f.test_trades, "test_exp_r": round(f.test_exp_r, 3)} for f in fs]},
        "by_setup_oos": by_group(oos, "setup").round(3).to_dict("records") if len(oos) else [],
        "portfolio_oos": {**clean(equity_metrics(port.equity, RISK.paper_capital)), **clean(trade_metrics(port.trades))} if port else {},
        "portfolio_fixed60_full": {**clean(equity_metrics(fixed.equity, RISK.paper_capital)), **clean(trade_metrics(fixed.trades))},
        "assumptions": [
            "Universe = current NIFTY 100 + NIFTY MIDCAP 150 constituents (survivorship bias: delisted / demoted stocks are absent; "
            "Kotak serves no history for delisted instruments).",
            "Entries are stop orders working from the session after the signal; gap-aware fills; 5 bps slippage per fill; "
            "Indian delivery / futures costs per fill.",
            "Same-bar stop and target -> stop assumed first; no target credited on the entry bar.",
            "Shorts simulated on the cash price as a proxy for stock futures (basis, rollover and margin not modelled), F&O stocks only "
            "in live selection.",
            "Only the score threshold is selected in-sample (grid 50-70), per walk-forward fold; all other rules fixed a priori.",
            "Trades still open at the data end are excluded from metrics.",
        ],
        "runtime_s": round(time.time() - t0, 1),
    }
    json_dump(out, PATHS.reports / "backtest_summary.json")
    if fixed is not None:
        fixed.equity.to_csv(PATHS.reports / "backtest_equity_fixed60.csv")
    if port is not None:
        port.equity.to_csv(PATHS.reports / "backtest_equity_oos.csv")
    return out
