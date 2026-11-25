"""Historical validation of the exact screening + execution logic used for live ideas.

Stage 1, `generate_signals`: for every session, run `stock_screener.screen_at` (data up to that session only) with
the score threshold disabled, and keep every geometrically valid setup with its score.
Stage 2a, `simulate_signals`: each signal traded in isolation (sized at the configured risk) through SimTrade,
giving an R-multiple distribution per setup that portfolio constraints do not muddy.
Stage 2b, `simulate_portfolio`: a capital-constrained portfolio (max concurrent positions, one position per stock,
max new ideas per day, chosen by score) for return, drawdown and Sharpe.

Look-ahead controls: signals use bars <= d; orders start working on the session AFTER d; pivots are only known k bars
after they form; indicators are causal; regime/breadth use data <= d. Shorts are simulated on the cash price as a proxy
for the stock future (basis and rollover ignored), with futures costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from config import RISK, STRATEGY, RiskConfig, StrategyConfig
from execution.position_manager import SimTrade
from research.stock_screener import ResearchData, screen_at

log = logging.getLogger(__name__)


def generate_signals(rd: ResearchData, start: pd.Timestamp, end: pd.Timestamp, cfg: StrategyConfig = STRATEGY,
                     progress=None) -> pd.DataFrame:
    open_cfg = replace(cfg, min_score=-1.0)
    dates = rd.calendar[(rd.calendar >= start) & (rd.calendar <= end)]
    rows = []
    for i, d in enumerate(dates):
        regime, cands, _ = screen_at(rd, d, open_cfg)
        for c in cands:
            s = c.setup
            if s.reject_reason:
                continue
            rows.append({"d": d, "symbol": s.symbol, "side": s.side, "setup": s.setup, "score": c.card.total,
                         "regime": regime.label, "regime_ok": regime.allows(s.side), "entry": s.entry, "stop": s.stop,
                         "t1": s.t1, "t2": s.t2, "atr": s.atr, "rr_t2": s.rr_t2,
                         "sector": rd.universe[s.symbol].industry,
                         "fno": bool(rd.universe[s.symbol].futures)})
        if progress and (i % 20 == 0 or i == len(dates) - 1):
            progress(i + 1, len(dates), d, len(rows))
    return pd.DataFrame(rows)


def _qty(sig, risk_cfg: RiskConfig, capital: float | None = None) -> int:
    cap = capital or risk_cfg.paper_capital
    rps = abs(sig.entry - sig.stop)
    q = int(cap * risk_cfg.risk_per_trade_pct / 100 // rps) if rps > 0 else 0
    # shorts are futures in live selection (whole lots, notional <= 1x capital); the backtest sizes them by risk without
    # lot rounding because historical lot sizes are not available
    cap_pct = risk_cfg.max_position_pct if sig.side == "LONG" else risk_cfg.max_futures_notional_pct
    return max(0, min(q, int(cap * cap_pct / 100 // sig.entry)))


def _trade(sig, qty: int, cfg: StrategyConfig) -> SimTrade:
    return SimTrade(sig.symbol, sig.side, sig.entry, sig.stop, sig.t1, sig.t2, qty,
                    "EQ_DELIVERY" if sig.side == "LONG" else "STOCK_FUTURES",
                    valid_bars=cfg.entry_valid_days, max_hold=cfg.max_holding_days)


def _run(tr: SimTrade, bars: pd.DataFrame, after: pd.Timestamp) -> SimTrade:
    for d, b in bars[bars.index > after].iterrows():
        tr.on_bar(d, float(b.open), float(b.high), float(b.low), float(b.close))
        if tr.done:
            break
    return tr


def trade_record(sig, tr: SimTrade) -> dict:
    risk = abs(tr.entry_price - tr.initial_stop) * tr.qty if tr.entry_price else 0
    return {"signal_d": sig.d, "symbol": sig.symbol, "side": sig.side, "setup": sig.setup, "score": sig.score,
            "regime": sig.regime, "state": tr.state, "entry_d": tr.entry_d, "exit_d": tr.exit_d,
            "exit_reason": tr.exit_reason, "entry_price": tr.entry_price, "qty": tr.qty,
            "gross": tr.gross_pnl(), "costs": tr.total_costs(), "net": tr.realized_pnl(),
            "risk": risk, "r": tr.realized_pnl() / risk if risk else np.nan, "bars_held": tr.bars_held,
            "notional": tr.entry_price * tr.qty}


def simulate_signals(rd: ResearchData, signals: pd.DataFrame, cfg: StrategyConfig = STRATEGY,
                     risk_cfg: RiskConfig = RISK) -> pd.DataFrame:
    """Every signal traded alone. Signals whose entry never triggers are kept with state EXPIRED/CANCELLED."""
    out = []
    for sig in signals.itertuples():
        q = _qty(sig, risk_cfg)
        if q <= 0:
            continue
        tr = _run(_trade(sig, q, cfg), rd.stocks[sig.symbol], sig.d)
        out.append(trade_record(sig, tr))
    return pd.DataFrame(out)


@dataclass
class PortfolioResult:
    trades: pd.DataFrame
    equity: pd.Series  # daily mark-to-market equity


def simulate_portfolio(rd: ResearchData, signals: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
                       min_score: "float | dict", cfg: StrategyConfig = STRATEGY, risk_cfg: RiskConfig = RISK,
                       max_new_per_day: int = 3) -> PortfolioResult:
    sig = signals[signals["regime_ok"] & (signals["d"] >= start) & (signals["d"] <= end)]
    if isinstance(min_score, dict):  # per-date threshold (walk-forward): {date: threshold}
        sig = sig[sig["score"] >= sig["d"].map(min_score)]
    else:
        sig = sig[sig["score"] >= min_score]
    by_day = {d: g.sort_values("score", ascending=False) for d, g in sig.groupby("d")}
    dates = rd.calendar[(rd.calendar >= start) & (rd.calendar <= end + pd.Timedelta(days=40))]
    cash_pnl = 0.0
    live: list[tuple[object, SimTrade]] = []
    done: list[dict] = []
    curve = {}
    for d in dates:
        # advance live trades with today's bar
        still = []
        for s, tr in live:
            df = rd.stocks[tr.symbol]
            if d in df.index:
                b = df.loc[d]
                tr.on_bar(d, float(b.open), float(b.high), float(b.low), float(b.close))
            if tr.done:
                cash_pnl += tr.realized_pnl()
                if tr.entry_price:
                    done.append(trade_record(s, tr))
            else:
                still.append((s, tr))
        live = still
        mtm = 0.0
        for s, tr in live:
            df = rd.stocks[tr.symbol]
            px = float(df["close"].loc[:d].iloc[-1])
            mtm += tr.realized_pnl() + tr.unrealized_pnl(px) - (sum(f.costs for f in tr.fills if f.reason == "ENTRY") *
                                                              (tr.qty_open / tr.qty if tr.qty else 0))
        equity = risk_cfg.paper_capital + cash_pnl + mtm
        curve[d] = equity
        # new orders from today's signals (they can fill from tomorrow)
        if d <= end and d in by_day:
            busy = {tr.symbol for _, tr in live}
            n_new = 0
            for s in by_day[d].itertuples():
                if len(live) >= risk_cfg.max_open_positions or n_new >= max_new_per_day:
                    break
                if s.symbol in busy:
                    continue
                q = _qty(s, risk_cfg, equity)
                if q <= 0:
                    continue
                live.append((s, _trade(s, q, cfg)))
                busy.add(s.symbol)
                n_new += 1
    return PortfolioResult(pd.DataFrame(done), pd.Series(curve, dtype=float))
