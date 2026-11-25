"""Daily technical indicators. All are causal: the value on day t uses only bars up to and including t.

Standard indicators come from TA-Lib (Wilder smoothing for RSI/ATR/ADX, SMA-seeded EMA). Historical volatility and
relative volume are computed here because TA-Lib has no direct equivalent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib

TRADING_DAYS = 252


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of an OHLCV frame with every indicator column the screener, scorer and charts use."""
    out = df.copy()
    o, h, l, c, v = (out[k].astype(float).to_numpy() for k in ("open", "high", "low", "close", "volume"))
    # trend
    for n in (9, 20, 50):
        out[f"ema{n}"] = talib.EMA(c, n)
    out["sma100"] = talib.SMA(c, 100)
    out["sma200"] = talib.SMA(c, 200)
    out["adx"] = talib.ADX(h, l, c, 14)
    out["plus_di"] = talib.PLUS_DI(h, l, c, 14)
    out["minus_di"] = talib.MINUS_DI(h, l, c, 14)
    # momentum
    out["rsi"] = talib.RSI(c, 14)
    macd, sig, hist = talib.MACD(c, 12, 26, 9)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd, sig, hist
    k, d = talib.STOCHRSI(c, timeperiod=14, fastk_period=14, fastd_period=3, fastd_matype=0)
    out["stochrsi_k"], out["stochrsi_d"] = k, d
    for n in (5, 10, 20):
        out[f"roc{n}"] = talib.ROC(c, n)
    # volatility
    out["atr"] = talib.ATR(h, l, c, 14)
    out["atr_pct"] = out["atr"] / out["close"] * 100
    up, mid, lo = talib.BBANDS(c, 20, 2.0, 2.0, 0)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = up, mid, lo
    out["bb_width"] = (up - lo) / mid * 100
    out["hv20"] = historical_volatility(out["close"], 20)
    # volume
    out["vol_sma20"] = talib.SMA(v, 20)
    out["rvol"] = relative_volume(out["volume"], 20)
    out["obv"] = talib.OBV(c, v)
    out["obv_ema20"] = talib.EMA(out["obv"].to_numpy(dtype=float), 20)
    out["traded_value_cr"] = out["close"] * out["volume"] / 1e7
    return out


def historical_volatility(close: pd.Series, n: int = 20) -> pd.Series:
    """Annualised standard deviation of daily log returns over n sessions, in percent."""
    r = np.log(close.astype(float)).diff()
    return r.rolling(n).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Today's volume divided by the average of the PREVIOUS n sessions (today excluded, so a spike is not diluted)."""
    v = volume.astype(float)
    return v / v.shift(1).rolling(n).mean()


def returns(close: pd.Series, n: int) -> pd.Series:
    return close.astype(float).pct_change(n) * 100
