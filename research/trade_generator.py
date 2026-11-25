"""Swing setup detection and trade-level construction.

For bar t (the last completed session) a setup is a price-action event plus a plan for the NEXT sessions:
  * entry: a stop order beyond the signal bar (buy-stop above its high / sell-stop below its low), valid for
    STRATEGY.entry_valid_days sessions. Price has to keep moving in the trade's direction to trigger it; we never assume a
    fill at the signal bar's close.
  * invalidation: the structural price that proves the idea wrong (base low, pullback swing low, support, breakdown
    bar high...). The stop sits STRATEGY.stop_atr_buffer x ATR beyond it. If that is tighter than 1 ATR it is widened to
    1 ATR (inside daily noise otherwise); wider than 3.5 ATR and the setup is rejected as not a swing trade.
  * targets: T1 = the nearest structural level in the trade's direction; T2 = the next level beyond it. Where no level
    exists (new highs / lows) a measured move or a volatility projection is used and labelled as such. Both are capped by
    what ~15 sessions of normal volatility can reach. Targets are never moved to make the reward:risk pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from config import STRATEGY, StrategyConfig
from research import pattern_detection as pat
from research import support_resistance as sr

T1_REACH_ATR = 3.5  # a T1 further than this many ATRs is not reachable in a 1-3 week hold -> projection used instead
T2_REACH_ATR = 5.0  # hard cap on T2 distance
PROJ_T1_ATR = 2.5
PROJ_T2_ATR = 4.5


@dataclass
class Setup:
    symbol: str
    side: str  # LONG / SHORT
    setup: str
    signal_date: pd.Timestamp
    close: float
    atr: float
    entry: float
    stop: float
    invalidation: float
    invalidation_reason: str
    t1: float
    t1_basis: str
    t2: float
    t2_basis: str
    tick: float = 0.05
    support: float | None = None
    resistance: float | None = None
    facts: list[str] = field(default_factory=list)  # human-readable price-action observations
    features: dict[str, Any] = field(default_factory=dict)
    reject_reason: str = ""

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def rr_t1(self) -> float:
        return abs(self.t1 - self.entry) / self.risk if self.risk else 0.0

    @property
    def rr_t2(self) -> float:
        return abs(self.t2 - self.entry) / self.risk if self.risk else 0.0

    @property
    def stop_atr(self) -> float:
        return self.risk / self.atr if self.atr else float("nan")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(risk=self.risk, rr_t1=self.rr_t1, rr_t2=self.rr_t2, stop_atr=self.stop_atr)
        d["signal_date"] = str(pd.Timestamp(self.signal_date).date())
        return d


def round_tick(x: float, tick: float, mode: str = "nearest") -> float:
    if tick <= 0:
        return round(x, 2)
    q = x / tick
    q = np.floor(q + 1e-9) if mode == "down" else np.ceil(q - 1e-9) if mode == "up" else np.round(q)
    return round(float(q * tick), 2)


@dataclass
class StockView:
    """Everything known about one stock at bar position t (no data after t is reachable through it)."""
    symbol: str
    df: pd.DataFrame  # with indicator columns; may extend beyond t, only rows <= t are read
    pivots: list[sr.Pivot]
    t: int
    tick: float = 0.05
    k: int = 5

    @property
    def row(self) -> pd.Series:
        return self.df.iloc[self.t]

    def levels(self) -> list[sr.Level]:
        return sr.levels_at(self.df, self.pivots, self.t, float(self.row.atr), self.k)


# ---------------------------------------------------------------------------------------------- level builder
def _build(v: StockView, side: str, name: str, invalidation: float, inv_reason: str, facts: list[str],
           cfg: StrategyConfig, base: pat.Base | None = None, breakout_level: float | None = None) -> Setup:
    r = v.row
    atr = float(r.atr)
    tick = v.tick
    if side == "LONG":
        entry = round_tick(float(r.high) + tick, tick, "up")
        stop = invalidation - cfg.stop_atr_buffer * atr
        if entry - stop < cfg.min_stop_atr * atr:
            stop = entry - cfg.min_stop_atr * atr
        stop = round_tick(stop, tick, "down")
    else:
        entry = round_tick(float(r.low) - tick, tick, "down")
        stop = invalidation + cfg.stop_atr_buffer * atr
        if stop - entry < cfg.min_stop_atr * atr:
            stop = entry + cfg.min_stop_atr * atr
        stop = round_tick(stop, tick, "up")
    risk = abs(entry - stop)
    levels = v.levels()
    sign = 1 if side == "LONG" else -1
    # MAJOR levels in the trade's direction, beyond the entry by at least a quarter ATR (the level being broken is not a
    # target). Single-touch minor swings are not treated as targets: they would put a "resistance" in front of almost
    # every entry and say nothing about where supply actually sits.
    ahead = sorted((lv for lv in levels if lv.major and sign * (lv.price - entry) > 0.25 * atr),
                   key=lambda lv: sign * (lv.price - entry))
    # 52-week extreme as an additional structural reference
    look = v.df.iloc[max(0, v.t - 250):v.t]
    ext = float(look["high"].max()) if side == "LONG" else float(look["low"].min())
    if sign * (ext - entry) > 0.25 * atr and not any(abs(lv.price - ext) < 0.5 * atr for lv in ahead):
        ahead.append(sr.Level(ext, 1, look.index[0], look.index[-1], "52W"))
        ahead.sort(key=lambda lv: sign * (lv.price - entry))
    measured = None
    if base is not None and breakout_level is not None:
        measured = breakout_level + sign * (base.high - base.low)

    def lvl_desc(lv: sr.Level) -> str:
        role = "resistance" if side == "LONG" else "support"
        if lv.kinds == "52W":
            return f"52-week {'high' if side == 'LONG' else 'low'} {lv.price:,.2f}"
        what = f"{lv.touches} touches" if lv.touches > 1 else f"swing {'high' if side == 'LONG' else 'low'} dominating {lv.strength} sessions each side"
        return f"{role} {lv.price:,.2f} ({what}, last {lv.last.date()})"

    t1 = t1_basis = None
    if ahead and abs(ahead[0].price - entry) <= T1_REACH_ATR * atr:
        t1, t1_basis = ahead[0].price, lvl_desc(ahead[0])
    elif measured is not None and 0 < sign * (measured - entry) <= T1_REACH_ATR * atr:
        t1, t1_basis = measured, f"measured move (base height {base.high - base.low:,.2f} projected from {breakout_level:,.2f})"
    else:
        t1, t1_basis = entry + sign * PROJ_T1_ATR * atr, f"volatility projection ({PROJ_T1_ATR} x ATR; no structure within reach)"
    rest = [lv for lv in ahead if sign * (lv.price - t1) > 0.5 * atr]
    cap = entry + sign * T2_REACH_ATR * atr
    if rest and sign * (rest[0].price - cap) <= 0:
        t2, t2_basis = rest[0].price, lvl_desc(rest[0])
    elif measured is not None and sign * (measured - t1) > 0.5 * atr and sign * (measured - cap) <= 0:
        t2, t2_basis = measured, "measured move from the base"
    else:
        proj = entry + sign * PROJ_T2_ATR * atr
        if sign * (proj - t1) > 0.5 * atr:
            t2, t2_basis = proj, f"volatility projection ({PROJ_T2_ATR} x ATR, ~15-session reach)"
        else:
            t2, t2_basis = t1, "same as T1 (no room beyond T1 within reach)"
    # A target is placed just in front of the level (orders sitting at a level often go unfilled).
    t1 = round_tick(t1 - sign * 0.1 * atr, tick)
    t2 = round_tick(max(t2 - sign * 0.1 * atr, t1) if side == "LONG" else min(t2 - sign * 0.1 * atr, t1), tick)
    sup = sr.nearest_below(levels, float(r.close))
    res = sr.nearest_above(levels, float(r.close))
    s = Setup(v.symbol, side, name, v.df.index[v.t], float(r.close), atr, entry, stop, round(invalidation, 2), inv_reason,
              t1, t1_basis, t2, t2_basis, tick, sup.price if sup else None, res.price if res else None, facts)
    # hard geometry checks
    if risk / atr > cfg.max_stop_atr:
        s.reject_reason = f"stop {risk / atr:.1f} ATR away (> {cfg.max_stop_atr}); invalidation too far for a swing trade"
    elif side == "LONG" and (entry - float(r.ema20)) / atr > cfg.max_extension_atr:
        s.reject_reason = f"entry {(entry - float(r.ema20)) / atr:.1f} ATR above EMA20; extended, chasing"
    elif side == "SHORT" and (float(r.ema20) - entry) / atr > cfg.max_extension_atr:
        s.reject_reason = f"entry {(float(r.ema20) - entry) / atr:.1f} ATR below EMA20; extended, chasing"
    elif s.rr_t1 < cfg.min_rr_t1:
        s.reject_reason = f"reward:risk to T1 {s.rr_t1:.2f} < {cfg.min_rr_t1} ({t1_basis} is too close)"
    elif s.rr_t2 < cfg.min_rr_t2:
        s.reject_reason = f"reward:risk to T2 {s.rr_t2:.2f} < {cfg.min_rr_t2}; structure does not support 1:2"
    return s


# ---------------------------------------------------------------------------------------------- detectors
def _up_trend(r) -> bool:
    return r.close > r.sma200 and r.ema50 > r.sma200


def _down_trend(r) -> bool:
    return r.close < r.sma200 and r.ema50 < r.sma200


def detect(v: StockView, cfg: StrategyConfig = STRATEGY) -> list[Setup]:
    df, t = v.df, v.t
    if t < 210:
        return []
    r = v.row
    if any(pd.isna(r[c]) for c in ("ema20", "ema50", "sma200", "atr", "rsi", "rvol")):
        return []
    atr = float(r.atr)
    o, h, l, c = float(r.open), float(r.high), float(r.low), float(r.close)
    cl = pat.close_location(df, t)
    candles = pat.candle_patterns(df, t)
    rvol = float(r.rvol)
    ema50_slope10 = float(df["ema50"].iloc[t] / df["ema50"].iloc[t - 10] - 1) * 100
    out: list[Setup] = []

    # ---------------- LONG: breakout from a base on volume
    if _up_trend(r):
        base = pat.consolidation(df, t - 1, atr, min_bars=8, max_bars=40)
        ph = pat.prior_high(df, t, cfg.breakout_lookback)
        if base and c > base.high and c > ph and rvol >= cfg.volume_confirm_rvol and cl >= 0.6:
            facts = [f"closed {c:,.2f}, above the {base.bars}-session base high {base.high:,.2f} and the {cfg.breakout_lookback}-day high {ph:,.2f}",
                     f"breakout volume {rvol:.1f}x the 20-day average; closed in the top {100 - cl * 100:.0f}% of the day's range",
                     f"base depth {base.height_atr:.1f} ATR ({base.low:,.2f}-{base.high:,.2f})"]
            inv = min(l, base.high)
            out.append(_build(v, "LONG", "Breakout", inv, f"a close back below {inv:,.2f} (breakout bar low / base top) means the breakout failed",
                              facts, cfg, base, base.high))
        # ---------------- LONG: trend continuation out of a tight flag
        elif r.ema20 > r.ema50 and float(r.adx) >= 20:
            flag = pat.consolidation(df, t - 1, atr, min_bars=4, max_bars=12, max_height_atr=2.5)
            if flag and c > flag.high and flag.low >= float(df["ema20"].iloc[t - 1]) - 0.5 * atr and rvol >= 1.2 and cl >= 0.6:
                facts = [f"broke out of a {flag.bars}-session flag ({flag.low:,.2f}-{flag.high:,.2f}) that held above the rising 20-day EMA",
                         f"ADX {float(r.adx):.0f}: trend strength confirmed; volume {rvol:.1f}x average on the break"]
                out.append(_build(v, "LONG", "Trend continuation", flag.low,
                                  f"a move below the flag low {flag.low:,.2f} ends the continuation pattern", facts, cfg, flag, flag.high))
        # ---------------- LONG: pullback to the 20/50 EMA
        if r.ema20 > r.ema50 and ema50_slope10 > 0:
            low3 = float(df["low"].iloc[t - 2:t + 1].min())
            touched = low3 <= float(r.ema20) + 0.3 * atr
            held = c >= float(r.ema50) - 0.3 * atr
            hi15 = float(df["high"].iloc[t - 15:t].max())
            depth = (hi15 - low3) / atr
            reversal = c > o and (c > float(df["high"].iloc[t - 1]) or bool(set(candles) & pat.BULLISH))
            if touched and held and depth >= 1.5 and 38 <= float(r.rsi) <= 62 and reversal:
                which = "50-day" if low3 <= float(r.ema50) + 0.3 * atr else "20-day"
                facts = [f"pulled back {depth:.1f} ATR from the {hi15:,.2f} swing high into the rising {which} EMA (low {low3:,.2f})",
                         f"RSI cooled to {float(r.rsi):.0f}; today's bar reversed up ({', '.join(candles) or 'close above prior high'})"]
                inv = float(df["low"].iloc[t - 5:t + 1].min())
                out.append(_build(v, "LONG", f"Pullback to {which} EMA", inv,
                                  f"a break of the pullback low {inv:,.2f} means the trend's buyers did not defend the EMA", facts, cfg))
    # ---------------- LONG: support bounce
    if c > float(r.sma200) or ema50_slope10 > 0:
        lvls = [lv for lv in v.levels() if lv.touches >= 2 and ("L" in lv.kinds)]
        low3 = float(df["low"].iloc[t - 2:t + 1].min())
        for lv in sorted(lvls, key=lambda x: abs(x.price - low3)):
            if abs(low3 - lv.price) <= 0.5 * atr and c > lv.price + 0.2 * atr and c > o and (cl >= 0.6 or set(candles) & pat.BULLISH):
                facts = [f"tested support {lv.price:,.2f} ({lv.touches} prior touches since {lv.first.date()}) and closed {((c - lv.price) / atr):.1f} ATR above it",
                         f"bullish reaction bar ({', '.join(candles) or 'strong close'})"]
                inv = min(lv.price, low3)
                out.append(_build(v, "LONG", "Support bounce", inv, f"a close below support {inv:,.2f} turns it into resistance", facts, cfg))
                break

    # ---------------- SHORT: breakdown from a base on volume
    if _down_trend(r):
        base = pat.consolidation(df, t - 1, atr, min_bars=8, max_bars=40)
        pl = pat.prior_low(df, t, cfg.breakout_lookback)
        if base and c < base.low and c < pl and rvol >= cfg.volume_confirm_rvol and cl <= 0.4:
            facts = [f"closed {c:,.2f}, below the {base.bars}-session base low {base.low:,.2f} and the {cfg.breakout_lookback}-day low {pl:,.2f}",
                     f"breakdown volume {rvol:.1f}x average; closed in the bottom {cl * 100:.0f}% of the range"]
            inv = max(h, base.low)
            out.append(_build(v, "SHORT", "Breakdown", inv, f"a close back above {inv:,.2f} means the breakdown failed",
                              facts, cfg, base, base.low))
        elif r.ema20 < r.ema50 and float(r.adx) >= 20:
            flag = pat.consolidation(df, t - 1, atr, min_bars=4, max_bars=12, max_height_atr=2.5)
            if flag and c < flag.low and flag.high <= float(df["ema20"].iloc[t - 1]) + 0.5 * atr and rvol >= 1.2 and cl <= 0.4:
                facts = [f"broke down from a {flag.bars}-session bear flag ({flag.low:,.2f}-{flag.high:,.2f}) capped by the falling 20-day EMA",
                         f"ADX {float(r.adx):.0f}; volume {rvol:.1f}x on the break"]
                out.append(_build(v, "SHORT", "Bearish continuation", flag.high,
                                  f"a move above the flag high {flag.high:,.2f} ends the pattern", facts, cfg, flag, flag.low))
        # rally into a falling EMA that is rejected
        if r.ema20 < r.ema50 and ema50_slope10 < 0:
            high3 = float(df["high"].iloc[t - 2:t + 1].max())
            lo15 = float(df["low"].iloc[t - 15:t].min())
            if (high3 >= float(r.ema20) - 0.3 * atr and c <= float(r.ema50) + 0.3 * atr and (high3 - lo15) / atr >= 1.5
                    and 38 <= float(r.rsi) <= 62 and c < o and (c < float(df["low"].iloc[t - 1]) or set(candles) & pat.BEARISH)):
                facts = [f"rallied {(high3 - lo15) / atr:.1f} ATR into the falling 20-day EMA and was rejected (high {high3:,.2f})",
                         f"RSI {float(r.rsi):.0f}; bearish reversal bar ({', '.join(candles) or 'close below prior low'})"]
                inv = float(df["high"].iloc[t - 5:t + 1].max())
                out.append(_build(v, "SHORT", "Bearish trend continuation (EMA rejection)", inv,
                                  f"a break above the rally high {inv:,.2f} invalidates the downtrend continuation", facts, cfg))
    # ---------------- SHORT: failed breakout
    fb, lvl, j = pat.failed_breakout(df, t, cfg.breakout_lookback)
    if fb and c < float(r.ema20) and cl <= 0.4:
        exc_high = float(df["high"].iloc[j:t + 1].max())
        facts = [f"pushed above the {cfg.breakout_lookback}-day high {lvl:,.2f} on {df.index[j].date()} (high {exc_high:,.2f}) but could not hold it",
                 f"now closed {c:,.2f}, back below the level and below the 20-day EMA: buyers who chased the breakout are trapped"]
        out.append(_build(v, "SHORT", "Failed breakout", exc_high,
                          f"a new high above {exc_high:,.2f} would mean the breakout resumed", facts, cfg))
    # ---------------- SHORT: resistance rejection
    if c < float(r.sma200) or ema50_slope10 < 0:
        lvls = [lv for lv in v.levels() if lv.touches >= 2 and ("H" in lv.kinds)]
        high3 = float(df["high"].iloc[t - 2:t + 1].max())
        for lv in sorted(lvls, key=lambda x: abs(x.price - high3)):
            if abs(high3 - lv.price) <= 0.5 * atr and c < lv.price - 0.2 * atr and c < o and (cl <= 0.4 or set(candles) & pat.BEARISH):
                facts = [f"tested resistance {lv.price:,.2f} ({lv.touches} prior touches since {lv.first.date()}) and was rejected",
                         f"bearish reaction bar ({', '.join(candles) or 'weak close'})"]
                inv = max(lv.price, high3)
                out.append(_build(v, "SHORT", "Resistance rejection", inv, f"a close above {inv:,.2f} breaks the resistance", facts, cfg))
                break
    for s in out:
        s.features.update(rvol=rvol, close_location=cl, candles=candles, ema50_slope10=ema50_slope10)
    return out
