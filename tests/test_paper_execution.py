"""Paper execution: fill rules, costs, persistence, duplicate prevention. Bars here are hand-written assertions."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from config import COSTS
from execution.paper_broker import PaperBroker
from execution.paper_orders import DuplicatePaperOrder, PaperOrder, idea_key
from execution.portfolio import summary
from execution.position_manager import CANCELLED, CLOSED, EXPIRED, OPEN, PENDING, SimTrade
from execution.transaction_costs import cost, equity_delivery, round_trip_pct, slip, stock_futures

NOSLIP = replace(COSTS, slippage_bps=0.0)


def long_trade(**kw):
    base = dict(symbol="X", side="LONG", trigger=100.0, stop=95.0, t1=106.0, t2=112.0, qty=100, cost_cfg=NOSLIP)
    base.update(kw)
    return SimTrade(**base)


def short_trade(**kw):
    base = dict(symbol="X", side="SHORT", trigger=100.0, stop=105.0, t1=94.0, t2=88.0, qty=100, instrument="STOCK_FUTURES",
                cost_cfg=NOSLIP)
    base.update(kw)
    return SimTrade(**base)


# ------------------------------------------------------------------------------------------ entries
def test_entry_at_trigger():
    t = long_trade()
    f = t.on_bar(1, 98, 101, 97, 100.5)
    assert t.state == OPEN and f[0].price == 100.0 and f[0].reason == "ENTRY"


def test_gap_through_trigger_fills_at_open_not_trigger():
    t = long_trade()
    t.on_bar(1, 102, 103, 101.5, 102.5)
    assert t.entry_price == 102.0  # the trigger price was never available


def test_cancel_when_invalidated_before_trigger():
    t = long_trade()
    t.on_bar(1, 97, 99, 94, 95)
    assert t.state == CANCELLED and not t.fills


def test_expiry():
    t = long_trade(valid_bars=2)
    t.on_bar(1, 98, 99, 97, 98)
    t.on_bar(2, 98, 99.5, 97, 99)
    assert t.state == EXPIRED and not t.fills


def test_no_target_credited_on_entry_bar_but_stop_is():
    t = long_trade()
    t.on_bar(1, 99, 113, 98, 110)  # touches trigger and T2 on the fill bar
    assert t.state == OPEN and len(t.fills) == 1
    t2 = long_trade()
    t2.on_bar(1, 99, 101, 94, 96)  # touches trigger and stop on the fill bar -> stopped (worst case)
    assert t2.state == CLOSED and t2.exit_reason == "STOP"


# ------------------------------------------------------------------------------------------ exits
def test_gap_through_stop_exits_at_open():
    t = long_trade()
    t.on_bar(1, 99, 101, 98, 100)
    t.on_bar(2, 92, 93, 90, 91)
    assert t.exit_reason == "STOP_GAP" and t.fills[-1].price == 92.0
    assert t.realized_pnl() < -(100 - 95) * 100  # loss larger than planned


def test_same_bar_stop_and_target_assumes_stop():
    t = long_trade()
    t.on_bar(1, 99, 101, 98, 100)
    t.on_bar(2, 100, 107, 94, 100)
    assert t.state == CLOSED and t.exit_reason == "STOP" and t.fills[-1].price == 95.0


def test_partial_at_t1_then_breakeven_from_next_bar():
    t = long_trade()
    t.on_bar(1, 99, 101, 98, 100)
    t.on_bar(2, 101, 106.5, 99.5, 105)  # T1 hit; low 99.5 < entry 100 on the SAME bar must not stop the rest
    assert t.t1_done and t.qty_open == 50 and t.state == OPEN
    t.on_bar(3, 104, 105, 99.9, 100.5)  # breakeven stop now active
    assert t.state == CLOSED and t.exit_reason == "BREAKEVEN" and t.fills[-1].price == 100.0
    assert t.realized_pnl() == pytest.approx(50 * 6 - t.total_costs())


def test_t2_exit():
    t = long_trade()
    t.on_bar(1, 99, 101, 98, 100)
    t.on_bar(2, 101, 106.5, 100.5, 106)
    t.on_bar(3, 106, 113, 105, 112.5)
    assert t.exit_reason == "T2" and t.qty_open == 0
    assert t.gross_pnl() == pytest.approx(50 * 6 + 50 * 12)


def test_gap_through_target_exits_at_open():
    t = long_trade()
    t.on_bar(1, 99, 101, 98, 100)
    t.on_bar(2, 115, 116, 114, 115)
    assert t.exit_reason == "T2_GAP" and t.fills[-1].price == 115.0


def test_time_stop():
    t = long_trade(max_hold=3)
    t.on_bar(1, 99, 101, 98, 100)
    for d in (2, 3, 4):
        t.on_bar(d, 100, 101, 99, 100.7)
    assert t.exit_reason == "TIME" and t.fills[-1].price == 100.7 and t.bars_held == 3


def test_short_mirror():
    t = short_trade()
    t.on_bar(1, 101, 102, 99.5, 100)
    assert t.state == OPEN and t.fills[0].action == "SELL" and t.entry_price == 100.0
    t.on_bar(2, 99, 99.5, 93.5, 94)
    assert t.t1_done and t.qty_open == 50 and t.fills[-1].action == "BUY" and t.fills[-1].price == 94.0
    t.on_bar(3, 93, 94, 87, 88)
    assert t.exit_reason == "T2" and t.gross_pnl() == pytest.approx(50 * 6 + 50 * 12)


def test_short_gap_against():
    t = short_trade()
    t.on_bar(1, 101, 102, 99.5, 100)
    t.on_bar(2, 108, 109, 107, 108)
    assert t.exit_reason == "STOP_GAP" and t.fills[-1].price == 108.0


def test_slippage_is_adverse():
    assert slip(100, True) > 100 > slip(100, False)
    t = long_trade(cost_cfg=COSTS)
    t.on_bar(1, 99, 101, 98, 100)
    assert t.entry_price > 100


# ------------------------------------------------------------------------------------------ costs
def test_delivery_costs_hand_calculated():
    c = equity_delivery(100_000, True)
    assert c.brokerage == pytest.approx(200)  # 0.20%
    assert c.stt == pytest.approx(100)  # 0.1%
    assert c.stamp == pytest.approx(15)  # 0.015% buy only
    assert c.exchange == pytest.approx(2.97)
    assert c.sebi == pytest.approx(0.1)
    assert c.gst == pytest.approx(0.18 * (200 + 2.97 + 0.1))
    s = equity_delivery(100_000, False)
    assert s.stamp == 0 and s.dp == COSTS.dp_charge_per_sell


def test_futures_costs_hand_calculated():
    b = stock_futures(1_000_000, True)
    s = stock_futures(1_000_000, False)
    assert b.stt == 0 and s.stt == pytest.approx(500)  # 0.05% on sell (Budget 2026)
    assert b.stamp == pytest.approx(20) and s.stamp == 0
    assert b.brokerage == 10


def test_round_trip_cost_reasonable():
    assert 0.4 < round_trip_pct("EQ_DELIVERY", 1000, 100) < 0.8
    assert round_trip_pct("STOCK_FUTURES", 1000, 500) < 0.1
    with pytest.raises(ValueError):
        cost("OPTIONS", 1000, True)


# ------------------------------------------------------------------------------------------ paper broker
def order(**kw):
    base = dict(idea_key=idea_key("RELIANCE", "LONG", "Breakout", "2026-06-10"), symbol="RELIANCE", side="LONG",
                instrument="EQ_DELIVERY", qty=10, trigger_price=1.0, stop_loss=0.5, target1=2.0, target2=3.0,
                created_on=date(2026, 6, 10))
    base.update(kw)
    return PaperOrder(**base)


def real_order(candles, d="2026-06-10"):
    """An order with levels around the real RELIANCE prices after `d`."""
    nxt = candles["RELIANCE"].loc[d:].iloc[1]
    trig = round(float(nxt.high) - 1.0, 2)  # will trigger on the next session
    return order(trigger_price=trig, stop_loss=round(trig * 0.95, 2), target1=round(trig * 1.04, 2),
                 target2=round(trig * 1.08, 2), qty=20, created_on=date.fromisoformat(d))


def test_order_validation():
    with pytest.raises(ValueError):
        order(stop_loss=1.5)  # stop above trigger on a long
    with pytest.raises(ValueError):
        order(side="SHORT")  # long-shaped levels on a short
    with pytest.raises(ValueError):
        order(mode="LIVE")
    with pytest.raises(ValueError):
        order(qty=7, lot=5)


def test_duplicate_orders_refused(repo):
    pb = PaperBroker(repo)
    pb.place(order())
    with pytest.raises(DuplicatePaperOrder):
        pb.place(order())
    assert len(pb.orders()) == 1


def test_replay_on_real_candles_is_idempotent_and_persisted(repo, candles):
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    pb = PaperBroker(repo)
    oid = pb.place(real_order(candles))
    pb.update()
    f1 = pb.fills()
    pb.update()
    pb.update()
    f2 = pb.fills()
    assert len(f1) == len(f2) >= 2
    pd.testing.assert_frame_equal(f1.drop(columns="fill_id"), f2.drop(columns="fill_id"))
    pos = pb.positions()
    assert pos.iloc[0]["order_id"] == oid
    entry = f1[f1["reason"] == "ENTRY"].iloc[0]
    assert entry["d"] > "2026-06-10"  # never fills on the signal session
    # a fresh broker over the same DB sees the same state (persistence)
    assert len(PaperBroker(repo).fills()) == len(f1)


def test_no_lookahead_in_paper_fills(repo, candles):
    """Fills derived with data up to day X must equal the first part of fills derived later."""
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    pb = PaperBroker(repo)
    pb.place(real_order(candles))
    pb.update(as_of=date(2026, 6, 12))
    early = pb.fills()
    pb.update()
    late = pb.fills()
    assert len(early) >= 1
    pd.testing.assert_frame_equal(early.drop(columns="fill_id").reset_index(drop=True),
                                  late.drop(columns="fill_id").iloc[:len(early)].reset_index(drop=True))


def test_portfolio_summary(repo, candles):
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    pb = PaperBroker(repo)
    pb.place(real_order(candles))
    pb.update()
    s = summary(pb)
    assert s.capital == 1_000_000
    assert s.open_positions + s.closed_positions == 1
    assert s.equity == pytest.approx(s.capital + s.realized + s.unrealized)


def test_pending_state_before_any_bar():
    t = long_trade()
    assert t.state == PENDING and t.realized_pnl() == 0 and t.unrealized_pnl(100) == 0
