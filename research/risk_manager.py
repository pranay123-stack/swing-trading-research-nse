"""Position sizing and executability.

Short selling in Indian cash equities: SEBI permits short sales in the cash market only if they are covered the same
day (intraday); an overnight (delivery) short in NSE cash equity is not possible for a retail account. A 1-3 week short
therefore needs a derivative: the stock's NSE futures contract (or SLB, which is not modelled). So:
  * LONG ideas  -> EQ_DELIVERY (cash, delivery), any quantity.
  * SHORT ideas -> STOCK_FUTURES, in whole lots, only for F&O-eligible stocks; the contract must have >= 5 days to expiry
                   or the holding window spans a rollover. A short in a non-F&O stock is reported as a RESEARCH-ONLY
                   directional view and is not executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config import RISK, STRATEGY, RiskConfig
from data.instrument_master import Equity
from execution.transaction_costs import round_trip_pct
from research.trade_generator import Setup


@dataclass
class Sizing:
    instrument: str  # EQ_DELIVERY / STOCK_FUTURES / NONE
    executable: bool
    qty: int
    lots: int
    lot_size: int
    contract: str
    contract_expiry: date | None
    notional: float
    capital_pct: float
    risk_amount: float  # loss at the stop (before gap risk and costs)
    risk_pct_capital: float
    round_trip_cost_pct: float
    note: str

    def as_dict(self) -> dict:
        return self.__dict__ | {"contract_expiry": str(self.contract_expiry) if self.contract_expiry else None}


def size(setup: Setup, eq: Equity | None, on: date, cfg: RiskConfig = RISK) -> Sizing:
    risk_budget = cfg.paper_capital * cfg.risk_per_trade_pct / 100
    notional_cap = cfg.paper_capital * cfg.max_position_pct / 100
    rps = setup.risk
    if rps <= 0:
        return Sizing("NONE", False, 0, 0, 1, "", None, 0, 0, 0, 0, 0, "zero risk per share; invalid levels")
    if setup.side == "LONG":
        qty = int(risk_budget // rps)
        qty = min(qty, int(notional_cap // setup.entry))
        note = "cash delivery (CNC)"
        if qty * setup.entry >= notional_cap * 0.999:
            note += f"; capped by the {cfg.max_position_pct:.0f}% max allocation"
        if qty <= 0:
            return Sizing("EQ_DELIVERY", False, 0, 0, 1, setup.symbol, None, 0, 0, 0, 0, 0, "risk budget below one share")
        return Sizing("EQ_DELIVERY", True, qty, 0, 1, f"{setup.symbol}-EQ", None, qty * setup.entry,
                      qty * setup.entry / cfg.paper_capital * 100, qty * rps, qty * rps / cfg.paper_capital * 100,
                      round_trip_pct("EQ_DELIVERY", setup.entry, qty), note)
    # SHORT
    fut = eq.nearest_future(on, min_days=STRATEGY.max_holding_days + 7) if eq else None
    rollover = False
    if eq and fut is None:
        fut = eq.nearest_future(on, min_days=5)
        rollover = fut is not None
    if fut is None:
        return Sizing("NONE", False, 0, 0, 1, "", None, 0, 0, 0, 0, 0,
                      "not in NSE F&O: an overnight short cannot be executed in the cash market -> research-only view")
    lot = int(fut["lot"])
    lots_by_risk = int(risk_budget // (rps * lot))
    lots_by_cap = int(cfg.paper_capital * cfg.max_futures_notional_pct / 100 // (setup.entry * lot))
    lots = min(lots_by_risk, lots_by_cap)
    note = f"short {fut['trading_symbol']} (NSE stock futures, lot {lot}, expiry {fut['expiry']})"
    if rollover:
        note += "; the holding window crosses this expiry, so a rollover to the next month would be needed"
    if lots <= 0 and lots_by_risk > 0:
        return Sizing("STOCK_FUTURES", False, 0, 0, lot, fut["trading_symbol"], fut["expiry"], setup.entry * lot,
                      setup.entry * lot / cfg.paper_capital * 100, rps * lot, rps * lot / cfg.paper_capital * 100,
                      round_trip_pct("STOCK_FUTURES", setup.entry, lot),
                      note + f"; one lot's notional exceeds {cfg.max_futures_notional_pct:.0f}% of capital -> not executable at this capital")
    if lots <= 0:
        one_lot_risk = rps * lot
        return Sizing("STOCK_FUTURES", False, 0, 0, lot, fut["trading_symbol"], fut["expiry"], setup.entry * lot,
                      setup.entry * lot / cfg.paper_capital * 100, one_lot_risk, one_lot_risk / cfg.paper_capital * 100,
                      round_trip_pct("STOCK_FUTURES", setup.entry, lot),
                      note + f"; one lot risks Rs {one_lot_risk:,.0f} (> {cfg.risk_per_trade_pct}% budget) -> not executable at this capital")
    qty = lots * lot
    return Sizing("STOCK_FUTURES", True, qty, lots, lot, fut["trading_symbol"], fut["expiry"], qty * setup.entry,
                  qty * setup.entry / cfg.paper_capital * 100, qty * rps, qty * rps / cfg.paper_capital * 100,
                  round_trip_pct("STOCK_FUTURES", setup.entry, qty),
                  note + "; futures margin (~15-25% of notional) is blocked, not the full notional")
