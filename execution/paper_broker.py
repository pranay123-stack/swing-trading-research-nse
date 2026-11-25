"""Paper broker: simulated orders, fills and positions persisted in SQLite.

SAFETY: this module imports no network library and no broker client. It cannot place, modify or cancel a real
order because nothing it can reach is able to. `tests/test_no_live_orders.py` enforces that statically and at runtime.

State model: an order is immutable once placed. Fills and positions are DERIVED by replaying the order through
`execution.position_manager.SimTrade` over the stored daily candles that come after the order's signal session. Replays
are deterministic, so `update()` can run any number of times without duplicating a fill.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd

from config import assert_paper_mode
from database.repository import Repository
from execution.paper_orders import DuplicatePaperOrder, PaperOrder
from execution.position_manager import CLOSED, OPEN, PENDING, SimTrade


class PaperBroker:
    def __init__(self, repo: Repository):
        assert_paper_mode()
        self.repo = repo

    # ------------------------------------------------------------------ orders
    def place(self, order: PaperOrder) -> str:
        assert_paper_mode()
        try:
            with self.repo.tx() as c:
                c.execute(
                    "INSERT INTO paper_orders (order_id, idea_key, symbol, side, instrument, order_type, qty, trigger_price, "
                    "limit_price, stop_loss, target1, target2, created_on, valid_until, status, mode, notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order.order_id, order.idea_key, order.symbol, order.side, order.instrument, order.order_type,
                     order.qty, order.trigger_price, None, order.stop_loss, order.target1, order.target2,
                     order.created_on.isoformat(), f"{order.valid_sessions}|{order.max_hold}|{order.lot}", PENDING,
                     order.mode, order.notes))
        except sqlite3.IntegrityError as e:
            raise DuplicatePaperOrder(f"paper order for {order.idea_key} already exists") from e
        return order.order_id

    def orders(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM paper_orders ORDER BY created_on, order_id", self.repo.conn)

    def _order_rows(self):
        return self.repo.conn.execute("SELECT * FROM paper_orders ORDER BY created_on, order_id").fetchall()

    # ------------------------------------------------------------------ simulation
    @staticmethod
    def replay(row, bars: pd.DataFrame) -> SimTrade:
        valid, hold, lot = (int(x) for x in str(row["valid_until"]).split("|"))
        tr = SimTrade(row["symbol"], row["side"], row["trigger_price"], row["stop_loss"], row["target1"], row["target2"],
                      int(row["qty"]), row["instrument"], valid_bars=valid, max_hold=hold, lot=lot)
        after = bars[bars.index > pd.Timestamp(row["created_on"])]
        for d, b in after.iterrows():
            tr.on_bar(d.date(), float(b.open), float(b.high), float(b.low), float(b.close))
            if tr.done:
                break
        return tr

    def update(self, as_of: date | None = None) -> list[SimTrade]:
        """Re-derive fills and positions for every order from stored candles (up to `as_of`)."""
        assert_paper_mode()
        out = []
        for row in self._order_rows():
            bars = self.repo.candles(row["symbol"], end=as_of)
            tr = self.replay(row, bars)
            last = bars[bars.index > pd.Timestamp(row["created_on"])]
            last_d = str(last.index[-1].date()) if len(last) else None
            last_c = float(last["close"].iloc[-1]) if len(last) else None
            with self.repo.tx() as c:
                c.execute("DELETE FROM paper_fills WHERE order_id=?", (row["order_id"],))
                c.executemany("INSERT INTO paper_fills (order_id, d, action, qty, price, costs, reason) VALUES (?,?,?,?,?,?,?)",
                              [(row["order_id"], str(f.d), f.action, f.qty, f.price, round(f.costs, 2), f.reason) for f in tr.fills])
                c.execute("UPDATE paper_orders SET status=? WHERE order_id=?",
                          ("FILLED" if tr.state in (OPEN, CLOSED) else tr.state, row["order_id"]))
                c.execute("DELETE FROM paper_positions WHERE order_id=?", (row["order_id"],))
                if tr.state in (OPEN, CLOSED):
                    c.execute(
                        "INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["order_id"], tr.symbol, tr.side, tr.qty_open, tr.entry_price, str(tr.entry_d), tr.stop,
                         tr.t1, tr.t2, int(tr.t1_done), round(tr.realized_pnl(), 2), round(tr.total_costs(), 2),
                         tr.state, last_d, last_c, str(tr.exit_d) if tr.exit_d else None))
            out.append(tr)
        return out

    # ------------------------------------------------------------------ views
    def positions(self) -> pd.DataFrame:
        df = pd.read_sql_query("SELECT * FROM paper_positions", self.repo.conn)
        if df.empty:
            return df
        sgn = df["side"].map({"LONG": 1, "SHORT": -1})
        df["unrealized"] = (sgn * (df["last_close"] - df["entry_price"]) * df["qty_open"]).where(df["status"] == OPEN, 0.0)
        return df

    def fills(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM paper_fills ORDER BY d, fill_id", self.repo.conn)
