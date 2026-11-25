"""Indicators vs independent reference implementations, on real RELIANCE / TCS candles; causality."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.indicators import add_indicators, historical_volatility, relative_volume


def ref_ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    out[n - 1] = x[:n].mean()  # SMA seed (TA-Lib convention)
    a = 2 / (n + 1)
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def ref_rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c)
    g, l = np.maximum(d, 0), np.maximum(-d, 0)
    out = np.full(len(c), np.nan)
    ag, al = g[:n].mean(), l[:n].mean()
    out[n] = 100 - 100 / (1 + ag / al) if al else 100
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + l[i - 1]) / n
        out[i] = 100 - 100 / (1 + ag / al) if al else 100
    return out


def ref_atr(h, l, c, n=14):
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    out = np.full(len(c), np.nan)
    a = tr[:n].mean()
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i - 1]) / n
        out[i] = a
    return out


@pytest.fixture(scope="module")
def rel(candles):
    return add_indicators(candles["RELIANCE"])


def test_ema_matches_reference(rel):
    c = rel["close"].to_numpy()
    for n in (9, 20, 50):
        np.testing.assert_allclose(rel[f"ema{n}"].to_numpy()[n:], ref_ema(c, n)[n:], rtol=1e-9)


def test_sma(rel):
    np.testing.assert_allclose(rel["sma200"].iloc[-1], rel["close"].iloc[-200:].mean(), rtol=1e-12)
    np.testing.assert_allclose(rel["sma100"].iloc[-1], rel["close"].iloc[-100:].mean(), rtol=1e-12)


def test_rsi_matches_wilder_reference(rel):
    np.testing.assert_allclose(rel["rsi"].to_numpy()[15:], ref_rsi(rel["close"].to_numpy())[15:], rtol=1e-8)
    assert rel["rsi"].dropna().between(0, 100).all()


def test_atr_matches_wilder_reference(rel):
    h, l, c = (rel[k].to_numpy() for k in ("high", "low", "close"))
    np.testing.assert_allclose(rel["atr"].to_numpy()[15:], ref_atr(h, l, c)[15:], rtol=1e-8)


def test_macd_identity(rel):
    m = rel.dropna(subset=["macd_signal"])
    np.testing.assert_allclose(m["macd_hist"], m["macd"] - m["macd_signal"], atol=1e-9)


def test_bollinger(rel):
    w = rel["close"].iloc[-20:]
    assert rel["bb_mid"].iloc[-1] == pytest.approx(w.mean())
    assert rel["bb_upper"].iloc[-1] == pytest.approx(w.mean() + 2 * w.std(ddof=0))


def test_adx_and_stochrsi_ranges(rel):
    assert rel["adx"].dropna().between(0, 100).all()
    assert rel["stochrsi_k"].dropna().between(-1e-9, 100 + 1e-9).all()


def test_roc(rel):
    c = rel["close"]
    assert rel["roc10"].iloc[-1] == pytest.approx((c.iloc[-1] / c.iloc[-11] - 1) * 100)


def test_obv(rel):
    c, v = rel["close"], rel["volume"]
    step = np.sign(c.diff()).fillna(0) * v
    obv = step.cumsum() + v.iloc[0]  # TA-Lib seeds OBV with the first volume
    np.testing.assert_allclose(rel["obv"], obv, rtol=1e-9)


def test_historical_volatility():
    c = pd.Series([100, 101, 99, 102, 100.5, 103], dtype=float)
    hv = historical_volatility(c, 5)
    r = np.log(c).diff().iloc[1:]
    assert hv.iloc[-1] == pytest.approx(r.std(ddof=1) * np.sqrt(252) * 100)


def test_relative_volume_excludes_today():
    v = pd.Series([100.0] * 20 + [300.0])
    assert relative_volume(v, 20).iloc[-1] == pytest.approx(3.0)


def test_indicators_are_causal(candles):
    """Values up to day t must not change when later bars are added or altered."""
    df = candles["TCS"]
    cut = 700
    a = add_indicators(df.iloc[:cut])
    future = df.iloc[cut:].copy()
    future[["open", "high", "low", "close"]] *= 1.5  # wild future
    b = add_indicators(pd.concat([df.iloc[:cut], future]))
    cols = [c for c in a.columns if c not in ("open", "high", "low", "close", "volume")]
    pd.testing.assert_frame_equal(a[cols], b[cols].iloc[:cut], check_freq=False)
