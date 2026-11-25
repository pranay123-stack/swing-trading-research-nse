"""Position sizing, executability of shorts, trade-level math."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from config import RISK
from research.risk_manager import size
from research.trade_generator import Setup


def setup(side="LONG", entry=1000.0, stop=960.0, t1=1050.0, t2=1100.0, sym="RELIANCE"):
    return Setup(sym, side, "Breakout", pd.Timestamp("2026-11-23"), entry - 5, 25.0, entry, stop, stop + 5, "x",
                 t1, "lvl", t2, "lvl", 0.1)


def test_long_sizing_by_risk(universe):
    s = setup()
    z = size(s, universe["RELIANCE"], date(2026, 11, 25))
    budget = RISK.paper_capital * RISK.risk_per_trade_pct / 100  # Rs 10,000
    assert z.executable and z.instrument == "EQ_DELIVERY"
    assert z.qty == int(budget // 40) == 250
    assert z.risk_amount == pytest.approx(250 * 40)
    assert z.risk_pct_capital == pytest.approx(1.0)
    assert 0 < z.round_trip_cost_pct < 1


def test_long_sizing_capped_by_allocation(universe):
    s = setup(entry=1000, stop=995, t1=1010, t2=1020)  # tiny risk -> risk sizing would allow 2000 shares
    z = size(s, universe["RELIANCE"], date(2026, 11, 25))
    assert z.qty == int(RISK.paper_capital * RISK.max_position_pct / 100 // 1000) == 250
    assert "max allocation" in z.note


def test_short_uses_futures_lots(universe):
    s = setup("SHORT", entry=1400, stop=1415, t1=1370, t2=1360)  # 15 x 500 = Rs 7,500 risk per lot
    z = size(s, universe["RELIANCE"], date(2026, 11, 25))
    lot = 500  # RELIANCE lot size in the real scrip master
    assert z.instrument == "STOCK_FUTURES" and z.lot_size == lot
    assert z.qty % lot == 0
    assert z.risk_amount <= RISK.paper_capital * RISK.risk_per_trade_pct / 100
    assert z.contract.startswith("RELIANCE") and z.contract.endswith("FUT")
    assert (z.contract_expiry - date(2026, 11, 25)).days >= 5
    assert z.executable and z.lots == 1 and z.notional <= RISK.paper_capital


def test_short_not_executable_when_one_lot_exceeds_budget(universe):
    s = setup("SHORT", entry=1400, stop=1460, t1=1300, t2=1250)  # 60 x 500 = Rs 30,000 > Rs 10,000
    z = size(s, universe["RELIANCE"], date(2026, 11, 25))
    assert not z.executable and "not executable" in z.note


def test_short_without_fno_is_research_only():
    s = setup("SHORT", entry=1400, stop=1430, t1=1350, t2=1320, sym="NOTFNO")
    z = size(s, None, date(2026, 11, 25))
    assert not z.executable and z.instrument == "NONE" and "cash market" in z.note


def test_rr_math():
    s = setup(entry=1000, stop=960, t1=1050, t2=1100)
    assert s.risk == 40 and s.rr_t1 == pytest.approx(1.25) and s.rr_t2 == pytest.approx(2.5)
    sh = setup("SHORT", entry=1000, stop=1040, t1=950, t2=900)
    assert sh.rr_t1 == pytest.approx(1.25) and sh.rr_t2 == pytest.approx(2.5)
