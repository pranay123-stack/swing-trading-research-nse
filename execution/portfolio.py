"""Virtual portfolio summary over the paper broker's derived positions."""

from __future__ import annotations

from dataclasses import dataclass

from config import RISK
from execution.paper_broker import PaperBroker


@dataclass
class PortfolioSummary:
    capital: float
    realized: float
    unrealized: float
    costs: float
    open_positions: int
    closed_positions: int
    pending_orders: int
    gross_exposure: float

    @property
    def equity(self) -> float:
        return self.capital + self.realized + self.unrealized


def summary(broker: PaperBroker, capital: float = RISK.paper_capital) -> PortfolioSummary:
    pos = broker.positions()
    orders = broker.orders()
    if pos.empty:
        return PortfolioSummary(capital, 0, 0, 0, 0, 0, int((orders["status"] == "PENDING").sum()) if len(orders) else 0, 0)
    op = pos[pos["status"] == "OPEN"]
    return PortfolioSummary(
        capital=capital,
        realized=float(pos["realized"].sum()),
        unrealized=float(pos["unrealized"].sum()),
        costs=float(pos["costs"].sum()),
        open_positions=len(op),
        closed_positions=int((pos["status"] == "CLOSED").sum()),
        pending_orders=int((orders["status"] == "PENDING").sum()),
        gross_exposure=float((op["last_close"] * op["qty_open"]).sum()),
    )
