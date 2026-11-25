"""Daily candlestick charts drawn from the retrieved Kotak Neo OHLCV (no screenshots, no synthetic bars)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from config import PATHS  # noqa: E402

UP, DOWN = "#1b9e77", "#d7301f"
COL = {"entry": "#1f4e9c", "stop": "#c0392b", "t1": "#27ae60", "t2": "#117a65", "sup": "#7f8c8d", "res": "#7f8c8d"}


def candlestick_chart(df: pd.DataFrame, idea: dict, out: Path | None = None, sessions: int = 120,
                      source: str = "Kotak Neo Trade API (historical, daily)") -> Path:
    """`df` carries indicator columns and ends at the signal session. `idea` is a trade-idea dict."""
    x = df.iloc[-sessions:].copy()
    if len(x) < 90 and len(df) >= 90:
        x = df.iloc[-90:].copy()
    n = len(x)
    pos = np.arange(n)
    fig = plt.figure(figsize=(13, 8.2), dpi=150)
    gs = fig.add_gridspec(3, 1, height_ratios=[3.6, 1.0, 1.1], hspace=0.06)
    ax, axv, axr = fig.add_subplot(gs[0]), fig.add_subplot(gs[1]), fig.add_subplot(gs[2])
    for a in (ax, axv):
        a.tick_params(labelbottom=False)

    o, h, l, c = (x[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    w = 0.62
    for i in range(n):
        col = UP if c[i] >= o[i] else DOWN
        ax.vlines(pos[i], l[i], h[i], color=col, linewidth=0.8)
        ax.add_patch(Rectangle((pos[i] - w / 2, min(o[i], c[i])), w, max(abs(c[i] - o[i]), 1e-6), color=col, linewidth=0))
    ax.plot(pos, x["ema20"], color="#f39c12", lw=1.3, label="EMA 20")
    ax.plot(pos, x["ema50"], color="#8e44ad", lw=1.3, label="EMA 50")
    if x["sma200"].notna().any():
        ax.plot(pos, x["sma200"], color="#34495e", lw=1.0, ls=":", label="SMA 200")

    right = n + 9
    lines: list[tuple[float, str, str, str, float, int]] = []  # (y, color, label, linestyle, linewidth, x-start)

    def hline(y, color, label, ls="-", lw=1.2, x0=0):
        if y is None or (isinstance(y, float) and np.isnan(y)):
            return
        lines.append((float(y), color, label, ls, lw, x0))

    trade_x0 = max(0, n - 30)  # trade levels drawn over the most recent sessions and the projection area
    for lv in idea.get("chart_levels", {}).get("supports", [])[:2]:
        hline(round(lv, 2), COL["sup"], "Support", ls="--", lw=0.9)
    for lv in idea.get("chart_levels", {}).get("resistances", [])[:2]:
        hline(round(lv, 2), COL["res"], "Resistance", ls="--", lw=0.9)
    hline(idea["entry"], COL["entry"], "Entry", x0=trade_x0)
    hline(idea["stop"], COL["stop"], "Stop", x0=trade_x0)
    hline(idea["t1"], COL["t1"], "T1", x0=trade_x0)
    hline(idea["t2"], COL["t2"], "T2", x0=trade_x0)
    side = idea["side"]
    zone = [n - 0.5, right]
    ax.fill_between(zone, min(idea["entry"], idea["stop"]), max(idea["entry"], idea["stop"]), color=COL["stop"], alpha=0.10, lw=0)
    ax.fill_between(zone, min(idea["entry"], idea["t2"]), max(idea["entry"], idea["t2"]), color=COL["t1"], alpha=0.10, lw=0)
    ax.annotate("signal bar", (pos[-1], h[-1] if side == "LONG" else l[-1]),
                xytext=(-28, 16 if side == "LONG" else -20), textcoords="offset points", ha="center", fontsize=7.5, color="#555",
                arrowprops={"arrowstyle": "-", "color": "#999", "lw": 0.6})
    lo = min(l.min(), idea["stop"], idea["t2"]) if side == "SHORT" else min(l.min(), idea["stop"])
    hi = max(h.max(), idea["t2"]) if side == "LONG" else max(h.max(), idea["stop"])
    pad = (hi - lo) * 0.04
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, right + 1)
    # draw lines, then labels pushed apart vertically so none overlap
    for y, color, label, ls, lw, x0 in lines:
        ax.hlines(y, x0, right, colors=color, linestyles=ls, linewidth=lw)
    gap = (hi - lo + 2 * pad) * 0.032
    placed: list[float] = []
    for y, color, label, ls, lw, x0 in sorted(lines, key=lambda z: z[0]):
        ly = max([y] + [p + gap for p in placed[-1:]])
        placed.append(ly)
        ax.annotate(f"{label} {y:,.2f}", (right, y), xytext=(right + 0.6, ly), textcoords="data", va="center", ha="left",
                    fontsize=8, fontweight="bold", color=color,
                    arrowprops={"arrowstyle": "-", "color": color, "lw": 0.5} if abs(ly - y) > gap * 0.3 else None)
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    ax.grid(alpha=0.18)
    ax.set_ylabel("Price (INR)")

    vcol = [UP if c[i] >= o[i] else DOWN for i in range(n)]
    axv.bar(pos, x["volume"] / 1e5, color=vcol, width=w, alpha=0.8)
    axv.plot(pos, x["vol_sma20"] / 1e5, color="#2c3e50", lw=1.0, label="Vol SMA 20")
    axv.set_ylabel("Vol (lakh)")
    axv.legend(loc="upper left", fontsize=7.5, frameon=False)
    axv.grid(alpha=0.18)
    axv.set_xlim(ax.get_xlim())

    axr.plot(pos, x["rsi"], color="#2c3e50", lw=1.1)
    axr.axhline(70, color="#c0392b", lw=0.7, ls="--")
    axr.axhline(30, color="#27ae60", lw=0.7, ls="--")
    axr.axhline(50, color="#999", lw=0.5, ls=":")
    axr.set_ylim(10, 90)
    axr.set_ylabel("RSI 14")
    axr.grid(alpha=0.18)
    axr.set_xlim(ax.get_xlim())
    # date axis: label roughly every 10 sessions with the real session date
    step = max(1, n // 12)
    ticks = list(range(0, n, step))
    if n - 1 - ticks[-1] < step * 0.6:
        ticks[-1] = n - 1  # always label the signal session
    else:
        ticks.append(n - 1)
    axr.set_xticks(ticks)
    axr.set_xticklabels([x.index[i].strftime("%d %b %y") for i in ticks], rotation=0, fontsize=8)
    for a in (ax, axv):
        a.set_xticks(ticks)
    axr.set_xlabel("Session date (IST) — non-trading days omitted")

    asof = x.index[-1].strftime("%d %b %Y")
    fig.suptitle(f"{idea['symbol']}  ·  {idea['company']}  ·  NSE  ·  Daily  ·  {side} {idea['setup']}",
                 x=0.01, ha="left", fontsize=13, fontweight="bold", y=0.985)
    fig.text(0.01, 0.948, f"Data as of {asof} close  ·  {n} sessions  ·  Source: {source}  ·  Entry {idea['entry']:,.2f} | "
             f"Stop {idea['stop']:,.2f} | T1 {idea['t1']:,.2f} | T2 {idea['t2']:,.2f} | R:R {idea['rr_t1']:.2f} / {idea['rr_t2']:.2f}",
             fontsize=8.5, color="#444")
    fig.subplots_adjust(left=0.06, right=0.90, top=0.93, bottom=0.07)
    out = out or PATHS.charts / f"{idea['symbol']}_{side}_{x.index[-1].date()}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


def equity_chart(equity: pd.Series, out: Path, title: str) -> Path:
    fig, ax = plt.subplots(figsize=(10, 3.6), dpi=150)
    ax.plot(equity.index, equity.values / 1e5, color="#1f4e9c", lw=1.3)
    ax.set_ylabel("Equity (Rs lakh)")
    ax.set_title(title, fontsize=10, loc="left")
    ax.grid(alpha=0.2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out
