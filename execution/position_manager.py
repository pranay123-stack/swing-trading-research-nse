"""Daily-bar trade state machine shared by the paper broker and the backtester (one fill model, two users).

Fill rules (long shown; short is the mirror image):
  PENDING (buy-stop at `trigger`, valid `valid_bars` sessions after the signal):
    * open >= trigger           -> filled at the OPEN (gapped through the trigger; no better price was available)
    * high >= trigger           -> filled at the trigger
    * neither, and low <= stop  -> order CANCELLED (idea invalidated before it triggered)
    * not filled in time        -> EXPIRED
    On the fill bar only the stop is checked (we cannot know whether the high came before or after the fill, so a
    target on the fill bar is never credited; a stop touched on the fill bar is always taken).
  OPEN:
    * open <= stop              -> exit at the OPEN (gap through the stop: the loss is larger than planned)
    * open >= T2 / T1           -> exit at the OPEN (gap through the target)
    * low <= stop and high >= a target on the same bar -> STOP assumed first (conservative)
    * high >= T1                -> book `t1_fraction` at T1; the stop moves to the entry price from the NEXT bar
    * high >= T2                -> exit the rest at T2
    * after `max_hold` sessions -> exit the rest at that session's close (time stop)
  Every fill gets adverse slippage; costs are computed per fill by the caller-supplied cost function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from config import COSTS, CostConfig
from execution.transaction_costs import cost as cost_fn
from execution.transaction_costs import slip

PENDING, OPEN, CLOSED, CANCELLED, EXPIRED = "PENDING", "OPEN", "CLOSED", "CANCELLED", "EXPIRED"


@dataclass
class Fill:
    d: object
    action: str  # BUY / SELL
    qty: int
    price: float
    costs: float
    reason: str  # ENTRY / STOP / STOP_GAP / T1 / T2 / T1_GAP / T2_GAP / TIME / BREAKEVEN


@dataclass
class SimTrade:
    symbol: str
    side: str
    trigger: float
    stop: float
    t1: float
    t2: float
    qty: int
    instrument: str = "EQ_DELIVERY"
    valid_bars: int = 3
    max_hold: int = 15
    t1_fraction: float = 0.5
    lot: int = 1
    cost_cfg: CostConfig = COSTS
    state: str = PENDING
    entry_price: float = 0.0
    entry_d: object = None
    qty_open: int = 0
    t1_done: bool = False
    bars_waited: int = 0
    bars_held: int = 0
    be_pending: bool = False
    fills: list[Fill] = field(default_factory=list)
    exit_d: object = None
    exit_reason: str = ""

    @property
    def long(self) -> bool:
        return self.side == "LONG"

    @property
    def done(self) -> bool:
        return self.state in (CLOSED, CANCELLED, EXPIRED)

    def _fill(self, d, qty: int, raw_price: float, reason: str, opening: bool) -> Fill:
        is_buy = (self.long and opening) or (not self.long and not opening)
        px = round(slip(raw_price, is_buy, self.cost_cfg), 2)
        f = Fill(d, "BUY" if is_buy else "SELL", qty, px, cost_fn(self.instrument, px * qty, is_buy, self.cost_cfg), reason)
        self.fills.append(f)
        return f

    def _exit(self, d, qty: int, price: float, reason: str) -> None:
        qty = min(qty, self.qty_open)
        if qty <= 0:
            return
        self._fill(d, qty, price, reason, opening=False)
        self.qty_open -= qty
        if self.qty_open == 0:
            self.state, self.exit_d, self.exit_reason = CLOSED, d, reason

    def _t1_qty(self) -> int:
        q = int(self.qty * self.t1_fraction)
        q -= q % self.lot
        return q if 0 < q < self.qty_open else 0

    def on_bar(self, d, o: float, h: float, l: float, c: float) -> list[Fill]:
        n0 = len(self.fills)
        if self.done:
            return []
        s = 1 if self.long else -1
        # prices are mapped so that "up" is always in the trade's favour
        O, H, L, C = (o, h, l, c) if self.long else (-o, -l, -h, -c)
        TR, ST, T1, T2 = s * self.trigger, s * self.stop, s * self.t1, s * self.t2

        if self.state == PENDING:
            self.bars_waited += 1
            if O >= TR or H >= TR:
                fill_at = O if O >= TR else TR
                self.state = OPEN
                self.qty_open = self.qty
                self.entry_d = d
                f = self._fill(d, self.qty, s * fill_at, "ENTRY", opening=True)
                self.entry_price = f.price
                if L <= ST:
                    self._exit(d, self.qty_open, self.stop, "STOP")
                return self.fills[n0:]
            if L <= ST:
                self.state, self.exit_reason = CANCELLED, "invalidated before trigger"
            elif self.bars_waited >= self.valid_bars:
                self.state, self.exit_reason = EXPIRED, "entry not triggered"
            return []

        # OPEN
        self.bars_held += 1
        if self.be_pending:
            # breakeven stop becomes active on the bar after T1 was booked
            self.stop = self.entry_price
            ST = s * self.stop
            self.be_pending = False
        # gaps at the open
        if O <= ST:
            self._exit(d, self.qty_open, s * O, "STOP_GAP" if not self.t1_done else "BREAKEVEN_GAP")
            return self.fills[n0:]
        if O >= T2:
            self._exit(d, self.qty_open, s * O, "T2_GAP")
            return self.fills[n0:]
        if O >= T1 and not self.t1_done:
            q = self._t1_qty()
            if q:
                self._exit(d, q, s * O, "T1_GAP")
            self.t1_done = True
            self.be_pending = True
        # intrabar
        stop_hit = L <= ST
        t1_hit = (not self.t1_done) and H >= T1
        t2_hit = H >= T2
        if stop_hit:
            self._exit(d, self.qty_open, self.stop, "STOP" if not self.t1_done else "BREAKEVEN")
            return self.fills[n0:]
        if t1_hit:
            q = self._t1_qty()
            if q:
                self._exit(d, q, self.t1, "T1")
            self.t1_done = True
            self.be_pending = True
        if t2_hit and self.qty_open:
            self._exit(d, self.qty_open, self.t2, "T2")
            return self.fills[n0:]
        if self.bars_held >= self.max_hold and self.qty_open:
            self._exit(d, self.qty_open, c, "TIME")
        return self.fills[n0:]

    # ------------------------------------------------------------------ accounting
    def realized_pnl(self) -> float:
        """Net of costs, for the quantity already closed (entry costs attributed pro-rata to the closed quantity)."""
        s = 1 if self.long else -1
        exits = [f for f in self.fills if f.reason != "ENTRY"]
        gross = sum(s * (f.price - self.entry_price) * f.qty for f in exits)
        closed_qty = sum(f.qty for f in exits)
        entry_costs = sum(f.costs for f in self.fills if f.reason == "ENTRY")
        part = closed_qty / self.qty if self.qty else 0.0
        return gross - sum(f.costs for f in exits) - entry_costs * part

    def gross_pnl(self) -> float:
        s = 1 if self.long else -1
        return sum(s * (f.price - self.entry_price) * f.qty for f in self.fills if f.reason != "ENTRY")

    def total_costs(self) -> float:
        return sum(f.costs for f in self.fills)

    def unrealized_pnl(self, mark: float) -> float:
        if self.state != OPEN:
            return 0.0
        s = 1 if self.long else -1
        return s * (mark - self.entry_price) * self.qty_open

    def r_multiple(self) -> float:
        """Net realized P&L in units of the initial risk (entry to initial stop, full quantity)."""
        risk = abs(self.entry_price - self.initial_stop) * self.qty
        return self.realized_pnl() / risk if risk else 0.0

    def __post_init__(self):
        self.initial_stop = self.stop
