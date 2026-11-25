"""Carry published ideas forward until they are resolved.

Every idea the research publishes is also placed as a PAPER order (stop-entry, valid 3 sessions). On later runs, ideas
whose paper order is still PENDING (not triggered yet) or whose position is still OPEN are carried forward: they keep
their original thesis and levels (nothing is re-fitted after publication) and get a status as of the latest close.
New setups only fill the remaining slots.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from config import STRATEGY
from database.repository import Repository
from execution.paper_broker import PaperBroker
from execution.paper_orders import DuplicatePaperOrder, PaperOrder, idea_key


def key_of(idea: dict) -> str:
    return idea_key(idea["symbol"], idea["side"], idea["setup"], idea["signal_date"])


def place_paper(pb: PaperBroker, idea: dict) -> str | None:
    sz = idea["sizing"]
    if not sz.get("executable") or not sz.get("qty"):
        return None
    try:
        return pb.place(PaperOrder(
            idea_key=key_of(idea), symbol=idea["symbol"], side=idea["side"], instrument=sz["instrument"], qty=sz["qty"],
            lot=sz.get("lot_size") or 1, trigger_price=idea["entry"], stop_loss=idea["stop"], target1=idea["t1"],
            target2=idea["t2"], created_on=date.fromisoformat(idea["signal_date"]),
            valid_sessions=STRATEGY.entry_valid_days, max_hold=STRATEGY.max_holding_days, notes=idea["setup"]))
    except DuplicatePaperOrder:
        return None


def published_ideas(repo: Repository) -> dict[str, dict]:
    """idea_key -> the most recently published payload for that idea."""
    out: dict[str, dict] = {}
    rows = repo.conn.execute(
        "SELECT t.payload FROM trade_ideas t JOIN research_runs r USING (run_id) WHERE r.status='DONE' ORDER BY r.started_at")
    for (payload,) in rows:
        d = json.loads(payload)
        if d.get("origin") == "carried":
            continue
        out[key_of(d)] = d
    return out


def status_of(pb: PaperBroker, order_id: str, as_of: date) -> dict:
    o = pb.orders().set_index("order_id").loc[order_id]
    pos = pb.positions()
    pos = pos[pos["order_id"] == order_id]
    fills = pb.fills()
    fills = fills[fills["order_id"] == order_id]
    st = {"order_id": order_id, "order_status": o["status"], "as_of": str(as_of)}
    bars = pb.repo.candles(o["symbol"], end=as_of)
    after = bars[bars.index > pd.Timestamp(o["created_on"])]
    st["sessions_since_signal"] = len(after)
    st["last_close"] = float(bars["close"].iloc[-1]) if len(bars) else None
    if len(pos):
        p = pos.iloc[0]
        entry = fills[fills["reason"] == "ENTRY"].iloc[0]
        st.update(state=p["status"], entry_date=entry["d"], entry_fill=float(entry["price"]), qty_open=int(p["qty_open"]),
                  stop_now=float(p["stop"]), t1_done=bool(p["t1_done"]), realized=float(p["realized"]),
                  unrealized=float(p.get("unrealized", 0.0)), exit_date=p["exit_d"],
                  exits=fills[fills["reason"] != "ENTRY"][["d", "qty", "price", "reason"]].to_dict("records"))
        held = len(bars[bars.index >= pd.Timestamp(entry["d"])])
        st["sessions_held"] = held
        st["sessions_left"] = max(0, STRATEGY.max_holding_days - held + 1)
        sgn = 1 if o["side"] == "LONG" else -1
        if st["last_close"]:
            st["open_pnl_pct"] = round(sgn * (st["last_close"] / st["entry_fill"] - 1) * 100, 2)
        trig = float(o["trigger_price"])
        day_open = float(bars.loc[pd.Timestamp(entry["d"]), "open"])
        gapped = (day_open >= trig) if o["side"] == "LONG" else (day_open <= trig)
        st["fill_note"] = f"filled at {st['entry_fill']:,.2f} on {entry['d']}"
        if gapped:
            st["fill_note"] += (f" (the session opened at {day_open:,.2f}, beyond the {trig:,.2f} trigger, so the fill is the open plus "
                                f"slippage, {abs(st['entry_fill'] - trig):,.2f} worse than the planned entry)")
    else:
        st["state"] = o["status"]  # PENDING / EXPIRED / CANCELLED
        st["sessions_left_to_trigger"] = max(0, STRATEGY.entry_valid_days - len(after)) if o["status"] == "PENDING" else 0
    return st


def describe(st: dict, idea: dict) -> str:
    s = st["state"]
    if s == "PENDING":
        return (f"Not triggered yet: {st['sessions_since_signal']} session(s) since the signal, entry order valid for "
                f"{st['sessions_left_to_trigger']} more.")
    if s == "OPEN":
        txt = (f"TRIGGERED — {st['fill_note']}. Last close {st['last_close']:,.2f} ({st['open_pnl_pct']:+.2f}% on the position); "
               f"stop {st['stop_now']:,.2f}, T1 {idea['t1']:,.2f}, T2 {idea['t2']:,.2f}; "
               f"{st['sessions_held']} session(s) held, time stop in {st['sessions_left']}.")
        if st.get("t1_done"):
            txt += " T1 booked; stop moved to entry."
        return txt
    if s == "CLOSED":
        ex = st["exits"][-1] if st.get("exits") else {}
        return f"CLOSED on {st.get('exit_date')} ({ex.get('reason', '')} at {ex.get('price', 0):,.2f}); realized P&L Rs {st['realized']:,.0f} net of costs."
    if s == "EXPIRED":
        return "EXPIRED — the entry never triggered within its validity window."
    if s == "CANCELLED":
        return "CANCELLED — price hit the invalidation level before the entry triggered."
    return s


def live_ideas(repo: Repository, as_of: date) -> list[dict]:
    """Published ideas whose paper order is still PENDING or whose position is OPEN as of `as_of`."""
    pb = PaperBroker(repo)
    pb.update(as_of)
    pub = published_ideas(repo)
    orders = pb.orders()
    out = []
    for o in orders.itertuples():
        idea = pub.get(o.idea_key)
        if idea is None or date.fromisoformat(o.created_on) >= as_of:
            continue
        st = status_of(pb, o.order_id, as_of)
        if st["state"] in ("PENDING", "OPEN"):
            carried = dict(idea)
            carried["origin"] = "carried"
            carried["issued_on"] = idea["signal_date"]
            carried["status"] = st
            carried["status_text"] = describe(st, idea)
            out.append(carried)
    return out
