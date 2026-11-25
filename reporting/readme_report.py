"""Write the contents of the latest research report into README.md (between the REPORT markers), from the same
data the PDF is built from: reports/research_context.json and reports/backtest_summary.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

from config import PATHS, STRATEGY

START, END = "<!-- REPORT:START -->", "<!-- REPORT:END -->"
FSTART, FEND = "<!-- FILES:START -->", "<!-- FILES:END -->"
REPO = "https://github.com/pranay123-stack/swing-trading-research-nse"

DESCRIPTIONS = [
    ("Swing_Trading_Research_", "**The research report (PDF)** — executive summary, methodology, backtest evidence, market regime, sector "
                                "analysis, each trade idea with its chart, thesis, levels, event risk and score card, limitations, data sources."),
    ("trade_ideas_", "**Trade ideas (CSV)** — one row per idea: symbol, side, setup, CMP, entry, stop, T1, T2, risk/reward, ATR, "
                     "position size, capital at risk, score, holding period, event summary."),
    ("research_context.json", "**Full research context (JSON)** — everything the PDF is built from: regime components, sector table, universe "
                              "and liquidity exclusions, top 25 screened setups with reject reasons, ideas with score cards and indicators."),
    ("backtest_summary.json", "**Backtest summary (JSON)** — period, signal counts, fill rate, isolated / walk-forward / portfolio metrics, "
                              "before-vs-after-cost table, results by setup, side and regime, every walk-forward fold, assumptions."),
    ("backtest_signals.csv", "**Every historical setup (CSV)** — each setup the screener found on each past session, with its score, regime and levels."),
    ("backtest_isolated_trades.csv", "**Isolated-signal trades (CSV)** — every regime-compatible signal traded on its own through the fill model: "
                                     "entry/exit dates, exit reason, gross, costs, net, R multiple."),
    ("backtest_portfolio_trades.csv", "**Out-of-sample portfolio trades (CSV)** — trades taken by the capital-constrained walk-forward paper portfolio."),
    ("backtest_equity_oos.csv", "**Out-of-sample equity curve (CSV)** — daily paper equity, daily return and drawdown of the walk-forward portfolio."),
    ("backtest_equity_fixed60.csv", "**Full-period equity curve (CSV)** — same portfolio over the whole period with the fixed score threshold 60."),
    ("backtest_equity.png", "**Equity curve chart (PNG)** — the out-of-sample paper portfolio's equity."),
]


def _describe(name: str) -> str:
    for prefix, d in DESCRIPTIONS:
        if name.startswith(prefix):
            return d
    if name.endswith(".png"):
        parts = name[:-4].split("_")
        if len(parts) >= 3:
            return f"**Daily candlestick chart (PNG)** — {parts[0]} {parts[1].lower()} idea, data as of {parts[2]}: candles, EMA20/50, SMA200, " \
                   "volume, RSI 14, support/resistance, entry, stop, T1, T2."
    return ""


def _size(p: Path) -> str:
    n = p.stat().st_size
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def files_markdown() -> str:
    L = []
    for folder, title in ((PATHS.reports, "reports/"), (PATHS.charts, "charts/")):
        files = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.startswith("."))
        if not files:
            continue
        L.append(f"**`{title}`**\n")
        for p in files:
            rel = p.relative_to(PATHS.root).as_posix()
            L.append(f"- [`{p.name}`]({rel}) ({_size(p)}) — {_describe(p.name)}  \n"
                     f"  [View]({REPO}/blob/main/{rel}) · [Download]({REPO}/raw/main/{rel})")
        L.append("")
    L.append(f"Download everything at once: [repository ZIP]({REPO}/archive/refs/heads/main.zip).")
    return "\n".join(L)


def _f(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def report_markdown(ctx: dict, bt: dict | None) -> str:
    rel = lambda p: Path(p).resolve().relative_to(PATHS.root).as_posix()  # noqa: E731
    pdf = next(iter(sorted(PATHS.reports.glob(f"Swing_Trading_Research_{ctx['as_of']}.pdf"))), None)
    reg = ctx["regime"]
    ideas = ctx["ideas"]
    L = []
    L.append(f"**Report:** [`{rel(pdf)}`]({rel(pdf)})" if pdf else "**Report:** not generated yet")
    L.append(f"  \n**Data as of:** {ctx['as_of']} NSE close · **generated:** {ctx['generated_at']} IST · run `{ctx['run_id']}`\n")

    L.append("#### 1. Executive summary\n")
    L.append(f"* Market regime: **{reg['label']}** (composite {reg['score']:+.2f} on −1…+1). Breadth: {reg['breadth_ema50']:.0f}% of the "
             f"universe above its 50-day EMA, {reg['breadth_sma200']:.0f}% above its 200-day SMA. INDIA VIX {reg['vix']:.2f}.")
    L.append(f"* Scan: {ctx['universe']['usable']} liquid stocks → {ctx['screen']['candidates']} setups → {ctx['screen']['passed']} passed every "
             f"rule → **{len(ideas)} idea(s)** after event, executability, sector and correlation checks."
             + (f" **Shortfall of {ctx['shortfall']}** reported, not filled." if ctx.get("shortfall") else ""))
    if bt:
        wf = bt["walk_forward"]["summary"]
        L.append(f"* Historical evidence: walk-forward out-of-sample expectancy **{wf['expectancy_r']:+.2f} R/trade** (t = {wf['expectancy_r_t']:.1f}), "
                 f"{wf['positive_folds']} of {wf['folds']} test windows positive — the setup family shows **no edge**; ideas are presented as "
                 f"individually argued theses with defined risk.")
    L.append("")
    if ideas:
        L.append("| # | Stock | Side / setup | CMP | Entry | Stop | T1 | T2 | R:R T1 / T2 | Score |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, d in enumerate(ideas, 1):
            L.append(f"| {i} | **{d['symbol']}** ({d['company']}) | {d['side']} · {d['setup']} | {_f(d['cmp'])} | {_f(d['entry'])} | "
                     f"{_f(d['stop'])} | {_f(d['t1'])} | {_f(d['t2'])} | {d['rr_t1']:.2f} / {d['rr_t2']:.2f} | {d['score']:.0f} ({d['confidence']}) |")
        L.append("")

    L.append("#### 2. Research methodology\n")
    L.append("Data sources, universe filters, regime-first approach, the nine price-action setups, structural entry/stop/target rules, "
             "the 100-point score, selection rules, sizing and Indian cost model — summarised in [Method](#method) below.")
    if bt:
        L.append(f"\nHistorical validation ({bt['period'][0]} → {bt['period'][1]}, {bt['sessions']} sessions, {bt['signals']} setups, "
                 f"{bt['fill_rate_pct']:.0f}% of stop entries triggered):\n")
        iso, wf, po = bt["isolated_all"], bt["walk_forward"]["summary"], bt.get("portfolio_oos", {})
        L.append("| Measure | All signals (isolated) | Walk-forward OOS | OOS paper portfolio |")
        L.append("|---|---|---|---|")
        L.append(f"| Trades | {iso['trades']} | {wf['trades']} | {po.get('trades', '-')} |")
        L.append(f"| Win rate | {iso['win_rate']:.1f}% | {wf['win_rate']:.1f}% | {_f(po.get('win_rate'), 1)}% |")
        L.append(f"| Profit factor | {_f(iso['profit_factor'])} | {_f(wf['profit_factor'])} | {_f(po.get('profit_factor'))} |")
        L.append(f"| Expectancy (R, net) | {_f(iso['expectancy_r'], 3)} (t {_f(iso['expectancy_r_t'], 1)}) | "
                 f"{_f(wf['expectancy_r'], 3)} (t {_f(wf['expectancy_r_t'], 1)}) | {_f(po.get('expectancy_r'), 3)} |")
        L.append(f"| Avg holding (sessions) | {_f(iso['avg_holding'], 1)} | {_f(wf['avg_holding'], 1)} | {_f(po.get('avg_holding'), 1)} |")
        L.append(f"| Return / max DD / Sharpe | - | - | {_f(po.get('total_return_pct'), 1)}% / {_f(po.get('max_drawdown_pct'), 1)}% / {_f(po.get('sharpe'))} |")
        if bt.get("cost_decomposition"):
            L.append("\n| Group | Trades | Mean R before costs | Cost drag (R) | Mean R after costs |")
            L.append("|---|---|---|---|---|")
            for r in bt["cost_decomposition"]:
                L.append(f"| {r['group']} | {r['trades']} | {r['gross_r']:+.3f} | {r['cost_r']:.3f} | {r['net_r']:+.3f} |")
        if bt.get("by_setup_oos"):
            L.append("\n| Setup (out-of-sample) | Trades | Win % | Profit factor | Expectancy R | t | Median hold |")
            L.append("|---|---|---|---|---|---|---|")
            for r in bt["by_setup_oos"]:
                L.append(f"| {r['setup']} | {r['trades']} | {_f(r['win_rate'], 1)} | {_f(r['profit_factor'])} | {_f(r['expectancy_r'], 3)} | "
                         f"{_f(r['expectancy_r_t'], 1)} | {_f(r['median_holding'], 0)} |")
        if (PATHS.reports / "backtest_equity.png").exists():
            L.append("\n![Out-of-sample paper equity](reports/backtest_equity.png)")
    L.append("")

    L.append("#### 3. Current market regime\n")
    L.append("| Component | Points | Observation |")
    L.append("|---|---|---|")
    for name, pts, obs in reg["components"]:
        L.append(f"| {name} | {pts:+d} | {obs} |")
    if ctx.get("index_quotes"):
        L.append("\n| Index | Last | Change | Quote time (IST) |")
        L.append("|---|---|---|---|")
        for k, q in ctx["index_quotes"].items():
            L.append(f"| {k} | {_f(q['ltp'])} | {q['change_pct']:+.2f}% | {q['ts']} |")
    L.append("")

    L.append("#### 4. Sector analysis\n")
    L.append("| Index | 5d % | 20d % | 60d % | vs NIFTY 20d | vs NIFTY 60d | > EMA50 | Quadrant |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s in ctx["sectors"]:
        L.append(f"| {s['index']} | {_f(s['ret5'], 1)} | {_f(s['ret20'], 1)} | {_f(s['ret60'], 1)} | {_f(s['rs_nifty20'], 1)} | "
                 f"{_f(s['rs_nifty60'], 1)} | {'yes' if s['above_ema50'] else 'no'} | {s['quadrant']} |")
    L.append("")

    L.append("#### 5–11. Trade ideas\n")
    for i, d in enumerate(ideas, 1):
        sz = d["sizing"]
        L.append(f"##### Idea {i}: {d['symbol']} — {d['side']} ({d['setup']}), score {d['score']:.0f}/100\n")
        L.append(f"{d['company']} · NSE · {d['industry']} · signal session {d['signal_date']} · "
                 f"{'executable as ' + sz['instrument'].replace('_', ' ').lower() if sz.get('executable') else 'research view only'}\n")
        L.append(f"![{d['symbol']} daily chart]({rel(d['chart'])})\n")
        for q, k in (("Why this stock?", "why_stock"), ("Why now?", "why_now"), ("What is the catalyst?", "catalyst"),
                     ("What confirms the trade?", "confirms"), ("What invalidates the trade?", "invalidates")):
            L.append(f"**{q}**")
            L += [f"- {x}" for x in d[k]]
            L.append("")
        L.append(f"**What is the maximum risk?** {d['max_risk']}\n")
        L.append(f"**Expected holding period:** {d['holding']}\n")
        L.append("| Item | Level | Basis |")
        L.append("|---|---|---|")
        L.append(f"| Current market price | {_f(d['cmp'])} | {d['cmp_source']}, {d['cmp_time']} |")
        L.append(f"| Entry (stop order) | {_f(d['entry'])} | beyond the {d['signal_date']} bar, valid {STRATEGY.entry_valid_days} sessions |")
        L.append(f"| Stop-loss | {_f(d['stop'])} | invalidation {_f(d['invalidation'])} ± {STRATEGY.stop_atr_buffer} ATR; {d['stop_atr']:.2f} ATR |")
        L.append(f"| Target 1 (book 50%) | {_f(d['t1'])} | {d['t1_basis']} |")
        L.append(f"| Target 2 | {_f(d['t2'])} | {d['t2_basis']} |")
        L.append(f"| Risk / reward per share | {_f(d['risk_per_share'])} / {_f(d['reward_t1'])}, {_f(d['reward_t2'])} | R:R {d['rr_t1']:.2f} / {d['rr_t2']:.2f} |")
        L.append(f"| Paper position | {sz.get('qty', 0):,} | {sz.get('note', '')}; notional Rs {_f(sz.get('notional'), 0)}, "
                 f"risk Rs {_f(sz.get('risk_amount'), 0)} ({_f(sz.get('risk_pct_capital'))}% of capital) |")
        ev = d.get("events") or {}
        L.append(f"\n**Event risk:** {ev.get('summary', 'not checked')}\n")
        L.append("| Score component | Points | Supporting | Against |")
        L.append("|---|---|---|---|")
        for c in d["scorecard"]["components"]:
            L.append(f"| {c['name']} | {c['points']:g}/{c['max']} | {'<br>'.join(c['pros']) or '-'} | {'<br>'.join(c['cons']) or '-'} |")
        L.append("")

    L.append("#### 12. Research limitations\n")
    L.append("Technical analysis only (no valuation/news catalyst claimed); Yahoo earnings dates can be estimates; survivorship bias in "
             "the backtest universe; shorts backtested on cash prices as a futures proxy; daily bars hide the intraday path (worst case "
             "assumed); stops are not guaranteed through gaps; scores rank evidence, they are not probabilities.\n")
    L.append("#### 13. Data sources and as-of timestamp\n")
    L.append(f"Kotak Neo historical + quotes API (prices, {ctx['as_of']} close), Kotak scrip master ({ctx['scrip_master_date']}), "
             "NSE Indices constituent files, Yahoo Finance calendar, Kotak Neo pricing page / NSE charges / Budget 2026 STT for costs. "
             "All timestamps IST.")
    return "\n".join(L)


def update_readme(readme: Path = PATHS.root / "README.md") -> bool:
    ctxp, btp = PATHS.reports / "research_context.json", PATHS.reports / "backtest_summary.json"
    if not (readme.exists() and ctxp.exists()):
        return False
    ctx = json.loads(ctxp.read_text())
    bt = json.loads(btp.read_text()) if btp.exists() else None
    text = readme.read_text()
    if START not in text:
        return False
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda m: f"{START}\n{report_markdown(ctx, bt)}\n{END}", text, flags=re.S)
    if FSTART in new:
        new = re.sub(re.escape(FSTART) + r".*?" + re.escape(FEND), lambda m: f"{FSTART}\n{files_markdown()}\n{FEND}", new, flags=re.S)
    readme.write_text(new)
    return True


if __name__ == "__main__":
    print("README updated" if update_readme() else "nothing to update")
