"""Indian equity trading-cost model (delivery cash equity and stock futures). Rates and sources: config.CostConfig."""

from __future__ import annotations

from dataclasses import dataclass

from config import COSTS, CostConfig


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp


def equity_delivery(turnover: float, is_buy: bool, cfg: CostConfig = COSTS) -> CostBreakdown:
    brokerage = turnover * cfg.delivery_brokerage_pct / 100
    if cfg.delivery_brokerage_cap is not None:
        brokerage = min(brokerage, cfg.delivery_brokerage_cap)
    stt = turnover * cfg.stt_delivery_pct / 100
    exch = turnover * cfg.exch_txn_cash_pct / 100
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    stamp = turnover * cfg.stamp_delivery_buy_pct / 100 if is_buy else 0.0
    gst = (brokerage + exch + sebi) * cfg.gst_pct / 100
    dp = 0.0 if is_buy else cfg.dp_charge_per_sell
    return CostBreakdown(brokerage, stt, exch, sebi, stamp, gst, dp)


def stock_futures(turnover: float, is_buy: bool, cfg: CostConfig = COSTS) -> CostBreakdown:
    brokerage = cfg.futures_brokerage_flat
    stt = 0.0 if is_buy else turnover * cfg.stt_futures_sell_pct / 100
    exch = turnover * cfg.exch_txn_fut_pct / 100
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    stamp = turnover * cfg.stamp_futures_buy_pct / 100 if is_buy else 0.0
    gst = (brokerage + exch + sebi) * cfg.gst_pct / 100
    return CostBreakdown(brokerage, stt, exch, sebi, stamp, gst, 0.0)


def cost(instrument: str, turnover: float, is_buy: bool, cfg: CostConfig = COSTS) -> float:
    if turnover <= 0:
        return 0.0
    if instrument == "EQ_DELIVERY":
        return equity_delivery(turnover, is_buy, cfg).total
    if instrument == "STOCK_FUTURES":
        return stock_futures(turnover, is_buy, cfg).total
    raise ValueError(f"unknown instrument type {instrument!r}")


def round_trip_pct(instrument: str, price: float, qty: int, cfg: CostConfig = COSTS) -> float:
    t = price * qty
    return (cost(instrument, t, True, cfg) + cost(instrument, t, False, cfg)) / t * 100 if t else 0.0


def slip(price: float, is_buy: bool, cfg: CostConfig = COSTS) -> float:
    """Apply adverse slippage: buys fill higher, sells fill lower."""
    f = cfg.slippage_bps / 1e4
    return price * (1 + f) if is_buy else price * (1 - f)
