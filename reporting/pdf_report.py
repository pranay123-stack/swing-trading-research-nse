"""Professional PDF research report (ReportLab)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from config import COSTS, IST, PATHS, RISK, STRATEGY, UNIVERSE

TITLE = "Swing Trading Research — Indian Equity Markets"
NAVY = colors.HexColor("#1f3a5f")
GREY = colors.HexColor("#5d6d7e")
LIGHT = colors.HexColor("#eef2f7")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=15, textColor=NAVY, spaceBefore=6, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=12, textColor=NAVY, spaceBefore=8, spaceAfter=4)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontSize=10, textColor=NAVY, spaceBefore=6, spaceAfter=2)
BODY = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9, leading=12.2, alignment=TA_LEFT)
SMALL = ParagraphStyle("S", parent=BODY, fontSize=7.8, leading=10, textColor=GREY)
BUL = ParagraphStyle("BL", parent=BODY, leftIndent=10, bulletIndent=2)
CELL = ParagraphStyle("C", parent=BODY, fontSize=7.8, leading=9.6)


def p(text: str, style=BODY) -> Paragraph:
    return Paragraph(text, style)


def bullets(items, style=BUL):
    return [Paragraph(str(i), style, bulletText="•") for i in items if i]


def table(rows, widths=None, header=True, font=7.8, zebra=True):
    data = [[c if isinstance(c, Paragraph) else Paragraph(str(c), CELL) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8d0da")),
          ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), font),
          ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        data[0] = [Paragraph(f"<font color='white'><b>{c.text if isinstance(c, Paragraph) else c}</b></font>", CELL)
                   for c in rows[0]]
        t = Table(data, colWidths=widths, repeatRows=1)
    if zebra:
        for i in range(1 if header else 0, len(rows)):
            if i % 2 == 0:
                st.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    t.setStyle(TableStyle(st))
    return t


def fmt(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(GREY)
    canvas.drawString(15 * mm, 9 * mm, f"{TITLE}  ·  research and paper simulation only, not investment advice")
    canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def load_analyst_notes() -> dict:
    f = PATHS.root / "data" / "analyst_notes.json"
    return json.loads(f.read_text()) if f.exists() else {}


def build_pdf(ctx: dict, bt: dict | None, out: Path | None = None) -> Path:
    out = out or PATHS.reports / f"Swing_Trading_Research_{ctx['as_of']}.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=15 * mm, title=TITLE, author="Swing research system (paper)")
    ideas = ctx["ideas"]
    notes = load_analyst_notes()
    reg = ctx["regime"]
    W = A4[0] - 30 * mm
    S: list = []

    # ------------------------------------------------------------------ cover / 1. executive summary
    S += [p(f"<font size=20 color='#1f3a5f'><b>{TITLE}</b></font>", BODY), Spacer(1, 6),
          p(f"Data as of the <b>{ctx['as_of']}</b> NSE close · generated {ctx['generated_at']} IST · "
            f"horizon 1–3 weeks · daily timeframe", SMALL), Spacer(1, 8)]
    S.append(p("1. Executive Summary", H1))
    nq = ctx.get("index_quotes", {})
    nifty_q = nq.get("Nifty 50")
    S.append(p(
        f"The market regime model classifies Indian equities as <b>{reg['label']}</b> (composite {reg['score']:+.2f} on a −1 to +1 scale). "
        f"{reg['breadth_ema50']:.0f}% of the liquid universe closes above its 50-day EMA and {reg['breadth_sma200']:.0f}% above its 200-day SMA; "
        f"INDIA VIX is {reg['vix']:.2f}."
        + (f" NIFTY 50 last traded {fmt(nifty_q['ltp'])} ({nifty_q['change_pct']:+.2f}%) at {nifty_q['ts']}." if nifty_q else "")))
    S.append(Spacer(1, 4))
    S.append(p(
        f"A systematic scan of {ctx['universe']['usable']} liquid NSE stocks (NIFTY 100 + NIFTY MIDCAP 150, after liquidity and data-quality "
        f"filters) produced {ctx['screen']['candidates']} technical setups, of which {ctx['screen']['passed']} passed every rule "
        f"(trend, volume, structure-based reward:risk, regime and a minimum score of {STRATEGY.min_score:.0f}/100). After event-risk, "
        f"executability, sector and correlation checks, <b>{len(ideas)} trade idea{'s' if len(ideas) != 1 else ''}</b> "
        f"{'are' if len(ideas) != 1 else 'is'} presented."))
    if ctx.get("shortfall"):
        S.append(p(f"<b>Shortfall:</b> {ctx['shortfall']} fewer idea(s) than requested met every criterion. The gap is reported rather than filled "
                   f"with weaker setups.", BODY))
    if bt:
        wf = bt["walk_forward"]["summary"]
        cd = {r["group"]: r for r in bt.get("cost_decomposition", [])}
        S.append(Spacer(1, 4))
        exp, tstat = wf.get("expectancy_r", 0) or 0, wf.get("expectancy_r_t", 0) or 0
        edge = exp > 0 and tstat >= 2
        text = (f"<b>Historical evidence.</b> The same rules were replayed over {bt['period'][0]} – {bt['period'][1]} on this universe. "
                f"Walk-forward out-of-sample expectancy was {exp:+.2f} R per trade (t = {tstat:.1f}), with "
                f"{wf.get('positive_folds')} of {wf.get('folds')} test windows positive")
        if "All" in cd:
            text += (f"; across all {cd['All']['trades']} simulated trades the average was {cd['All']['gross_r']:+.3f} R before costs and "
                     f"{cd['All']['net_r']:+.3f} R after costs")
        text += (". The setup family therefore has historical support." if edge else
                 ". The setup family did <b>not</b> show a historical edge, so the ideas below are presented as individually argued chart "
                 "theses with defined risk, not as outputs of a proven system (details in section 2).")
        S.append(p(text))
    S.append(Spacer(1, 6))
    rows = [["#", "Stock", "Side / setup", "CMP", "Entry", "Stop", "T1", "T2", "R:R (T1/T2)", "Score"]]
    for i, d in enumerate(ideas, 1):
        rows.append([i, f"<b>{d['symbol']}</b><br/>{d['company']}", f"{d['side']}<br/>{d['setup']}", fmt(d["cmp"]),
                     fmt(d["entry"]), fmt(d["stop"]), fmt(d["t1"]), fmt(d["t2"]), f"{d['rr_t1']:.2f} / {d['rr_t2']:.2f}",
                     f"{d['score']:.0f} ({d['confidence']})"])
    S.append(table(rows, [8 * mm, 38 * mm, 32 * mm, 16 * mm, 16 * mm, 16 * mm, 16 * mm, 16 * mm, 18 * mm, 16 * mm]))
    S.append(Spacer(1, 4))
    for d in ideas:
        n = notes.get(d["symbol"], {})
        line = n.get("summary") or f"{d['side'].title()} {d['symbol']}: {d['setup'].lower()} — {d['why_now'][0]}"
        S += bullets([f"<b>{d['symbol']}</b>: {line}"])
    S.append(Spacer(1, 4))
    S.append(p("All positions in this document are virtual. The system that produced it has no order-placement capability "
               "(paper execution only).", SMALL))

    # ------------------------------------------------------------------ 2. methodology
    S += [PageBreak(), p("2. Research Methodology", H1)]
    S += bullets([
        f"<b>Data.</b> Daily OHLCV for every stock and index from the Kotak Neo Trade API historical endpoint "
        f"(<i>market-data/1.0/historical/details</i>, interval D), requested in ≤179-day windows and cached in SQLite. "
        f"The instrument master (tokens, tick size, F&amp;O contracts, record dates) is Kotak's daily scrip master "
        f"({ctx['scrip_master_date']}). Universe membership is NSE Indices' published constituent files.",
        f"<b>Universe filters.</b> Price ≥ Rs {UNIVERSE.min_price:.0f}; median 20-day traded value ≥ Rs {UNIVERSE.min_median_traded_value_cr:.0f} crore; "
        f"median volume ≥ {UNIVERSE.min_median_volume:,}; ≥ {UNIVERSE.min_history_bars} sessions of history; no zero-volume session in the last 60 "
        f"(suspension proxy); hard data-quality checks (OHLC consistency, duplicates, missing sessions vs the NIFTY calendar, staleness).",
        "<b>Regime first.</b> Market direction is scored from NIFTY 50, NIFTY BANK and NIFTY MIDCAP 100 trend structure, universe breadth "
        "and INDIA VIX before any stock is considered. Long ideas are not taken in a Strong Bearish regime, short ideas not in a Strong Bullish one.",
        "<b>Setups (price action, not indicator crossovers).</b> Long: base breakout on volume, trend-continuation flag, pullback to a rising "
        "20/50-day EMA with a reversal bar, bounce from multi-touch support. Short: base breakdown on volume, bear flag, rally rejected at a "
        "falling EMA, failed breakout (trapped buyers), rejection at multi-touch resistance.",
        f"<b>Levels.</b> Entry is a stop order beyond the signal bar, valid {STRATEGY.entry_valid_days} sessions (price must confirm). The stop sits "
        f"{STRATEGY.stop_atr_buffer} ATR beyond the structural invalidation level (never a fixed percentage); stops tighter than "
        f"{STRATEGY.min_stop_atr} ATR are widened, setups needing more than {STRATEGY.max_stop_atr} ATR are rejected. T1 is the nearest swing "
        f"level in the trade's direction (pivot clusters, 52-week extremes), T2 the next one; measured moves or volatility projections are used "
        f"only where no level exists, and are labelled. Ideas need reward:risk ≥ {STRATEGY.min_rr_t1} to T1 and ≥ {STRATEGY.min_rr_t2} to T2 "
        f"<i>at the structural levels</i>; targets are never stretched to pass.",
        "<b>Scoring (100).</b> Trend structure 20, momentum 15, volume 15, relative strength 15, price action 15, reward:risk 10, regime 10. "
        "Every point is tied to a stated observation (see each idea's score card).",
        f"<b>Selection.</b> Highest scores first; one idea per sector; 60-day return correlation ≤ {STRATEGY.max_pair_correlation} between ideas; "
        f"no earnings/results/board meeting inside the holding window; shorts must be executable through NSE stock futures.",
        f"<b>Sizing.</b> Virtual capital Rs {RISK.paper_capital / 1e5:.0f} lakh, {RISK.risk_per_trade_pct}% risked per idea at the stop, "
        f"≤ {RISK.max_position_pct:.0f}% of capital per position; futures sized in whole lots.",
        f"<b>Costs.</b> Delivery brokerage {COSTS.delivery_brokerage_pct}% (Kotak Neo Trade Free plan), STT {COSTS.stt_delivery_pct}% each side "
        f"(delivery) / {COSTS.stt_futures_sell_pct}% on futures sales (Budget 2026 rate), NSE transaction charges, SEBI fee, stamp duty, 18% GST, "
        f"DP charge, plus {COSTS.slippage_bps:.0f} bps slippage per fill.",
    ])
    if bt:
        S.append(p("Historical validation", H2))
        wf = bt["walk_forward"]["summary"]
        iso = bt["isolated_all"]
        po = bt.get("portfolio_oos", {})
        S.append(p(
            f"The identical screening and execution code was replayed over {bt['period'][0]} – {bt['period'][1]} ({bt['sessions']} sessions). "
            f"{bt['signals']} setups were generated ({bt['signals_regime_ok']} regime-compatible); {bt['fill_rate_pct']:.0f}% of their stop entries "
            f"triggered. Walk-forward: the score threshold was chosen on each 250-session train window and applied to the next 63 sessions; only "
            f"those out-of-sample windows are reported."))
        rows = [["Measure", "All regime-compatible signals (isolated)", "Walk-forward out-of-sample", "OOS paper portfolio"],
                ["Trades", iso.get("trades"), wf.get("trades"), po.get("trades", "-")],
                ["Win rate", f"{iso.get('win_rate', 0):.1f}%", f"{wf.get('win_rate', 0):.1f}%", f"{po.get('win_rate', 0):.1f}%" if po else "-"],
                ["Avg win / avg loss (Rs)", f"{fmt(iso.get('avg_win'), 0)} / {fmt(iso.get('avg_loss'), 0)}",
                 f"{fmt(wf.get('avg_win'), 0)} / {fmt(wf.get('avg_loss'), 0)}",
                 f"{fmt(po.get('avg_win'), 0)} / {fmt(po.get('avg_loss'), 0)}" if po else "-"],
                ["Profit factor", fmt(iso.get("profit_factor")), fmt(wf.get("profit_factor")), fmt(po.get("profit_factor")) if po else "-"],
                ["Expectancy (R, net)", f"{fmt(iso.get('expectancy_r'), 3)} (t={fmt(iso.get('expectancy_r_t'), 1)})",
                 f"{fmt(wf.get('expectancy_r'), 3)} (t={fmt(wf.get('expectancy_r_t'), 1)})",
                 f"{fmt(po.get('expectancy_r'), 3)}" if po else "-"],
                ["Avg holding (sessions)", fmt(iso.get("avg_holding"), 1), fmt(wf.get("avg_holding"), 1), fmt(po.get("avg_holding"), 1) if po else "-"],
                ["Total return / max DD / Sharpe", "-", "-",
                 f"{fmt(po.get('total_return_pct'), 1)}% / {fmt(po.get('max_drawdown_pct'), 1)}% / {fmt(po.get('sharpe'), 2)}" if po else "-"],
                ["Positive OOS folds", "-", f"{wf.get('positive_folds')} of {wf.get('folds')}", "-"]]
        S.append(table(rows, [42 * mm, 48 * mm, 48 * mm, 42 * mm]))
        if bt.get("cost_decomposition"):
            rows = [["Group (all regime-compatible signals)", "Trades", "Mean R before costs", "Cost drag (R)", "Mean R after costs"]]
            for r in bt["cost_decomposition"]:
                rows.append([r["group"], r["trades"], f"{r['gross_r']:+.3f}", f"{r['cost_r']:.3f}", f"{r['net_r']:+.3f}"])
            S += [Spacer(1, 4), table(rows, [55 * mm, 18 * mm, 36 * mm, 30 * mm, 36 * mm])]
        eq = PATHS.reports / "backtest_equity.png"
        if eq.exists():
            S += [Spacer(1, 4), Image(str(eq), width=W, height=W * 0.36)]
        if bt.get("by_setup_oos"):
            rows = [["Setup (out-of-sample)", "Trades", "Win %", "Profit factor", "Expectancy R", "t-stat", "Median hold"]]
            for r in bt["by_setup_oos"]:
                rows.append([r["setup"], r["trades"], fmt(r["win_rate"], 1), fmt(r["profit_factor"]), fmt(r["expectancy_r"], 3),
                             fmt(r["expectancy_r_t"], 1), fmt(r["median_holding"], 0)])
            S += [Spacer(1, 4), table(rows, [58 * mm, 16 * mm, 16 * mm, 22 * mm, 24 * mm, 16 * mm, 22 * mm])]
        verdict = notes.get("_backtest_verdict")
        if verdict:
            S += [Spacer(1, 4), p(f"<b>Reading the evidence.</b> {verdict}")]
        S.append(p("Assumptions: " + " ".join(bt.get("assumptions", [])), SMALL))

    # ------------------------------------------------------------------ 3. regime
    S += [PageBreak(), p("3. Current Market Regime", H1),
          p(f"<b>{reg['label']}</b> — composite score {reg['score']:+.2f}. Each component scores −2 to +2 (breadth vs SMA200 and VIX −1/−2 to +1).")]
    rows = [["Component", "Points", "Observation"]] + [[c[0], f"{c[1]:+d}", c[2]] for c in reg["components"]]
    S.append(table(rows, [45 * mm, 15 * mm, W - 60 * mm]))
    if nq:
        S.append(Spacer(1, 4))
        rows = [["Index", "Last", "Change", "Day range", "Quote time (IST)"]]
        for k, q in nq.items():
            rows.append([k, fmt(q["ltp"]), f"{q['change_pct']:+.2f}%", f"{fmt(q['low'])} – {fmt(q['high'])}", q["ts"]])
        S.append(table(rows, [40 * mm, 28 * mm, 22 * mm, 45 * mm, 45 * mm]))
    if notes.get("_regime"):
        S += [Spacer(1, 4), p(notes["_regime"])]
    S.append(p("Implication: in this regime, long ideas receive the full regime-alignment points and short ideas are only accepted if their "
               "own structure is strong enough to clear the threshold without them.", SMALL))

    # ------------------------------------------------------------------ 4. sectors
    S += [p("4. Sector Analysis", H1),
          p("NSE sector indices ranked by 20-session return relative to NIFTY 50. Quadrant: Leading = outperforming over 60 sessions and "
            "improving; Weakening = outperforming but fading; Improving = lagging but turning up; Lagging = underperforming and fading.")]
    rows = [["Index", "5d %", "20d %", "60d %", "vs NIFTY 20d", "vs NIFTY 60d", "> EMA50", "RSI", "Quadrant"]]
    for s in ctx["sectors"]:
        rows.append([s["index"], fmt(s["ret5"], 1), fmt(s["ret20"], 1), fmt(s["ret60"], 1), fmt(s["rs_nifty20"], 1),
                     fmt(s["rs_nifty60"], 1), "yes" if s["above_ema50"] else "no", fmt(s["rsi"], 0), s["quadrant"]])
    S.append(table(rows, [36 * mm, 14 * mm, 14 * mm, 14 * mm, 20 * mm, 20 * mm, 15 * mm, 12 * mm, 22 * mm], font=7.2))
    if notes.get("_sectors"):
        S += [Spacer(1, 4), p(notes["_sectors"])]

    # ------------------------------------------------------------------ 5-11. ideas
    S += [PageBreak(), p("5. Three Best Trade Ideas" if len(ideas) == 3 else f"5. Trade Ideas ({len(ideas)} of 3 requested)", H1)]
    S.append(p("Sections 6–11 follow for each idea: technical thesis, levels, reward:risk, invalidation, event risk and the dated daily chart."))
    for i, d in enumerate(ideas, 1):
        n = notes.get(d["symbol"], {})
        sz = d["sizing"]
        if i > 1:
            S.append(PageBreak())
        S.append(p(f"Idea {i}: {d['symbol']} — {d['side']} ({d['setup']})", H1))
        S.append(p(f"{d['company']} · NSE · {d['industry']} · {', '.join(d['indices'])} · signal session {d['signal_date']} · "
                   f"score <b>{d['score']:.0f}/100</b> ({d['confidence']} confidence) · "
                   f"{'executable: ' + sz['instrument'].replace('_', ' ').lower() if sz.get('executable') else 'research view only'}", SMALL))
        S.append(Image(d["chart"], width=W, height=W * 8.2 / 13))
        S.append(p(f"Chart: {d['symbol']} daily candles, last 120 sessions to {d['signal_date']}, EMA20/EMA50/SMA200, volume with "
                   f"20-day average, RSI 14, nearest support/resistance and the trade levels. Source: Kotak Neo historical API.", SMALL))
        if n.get("thesis"):
            S += [p("Analyst view", H2), p(n["thesis"])]
        S.append(p("6. Technical thesis", H2))
        for q, key in (("WHY THIS STOCK?", "why_stock"), ("WHY NOW?", "why_now"), ("WHAT IS THE CATALYST?", "catalyst"),
                       ("WHAT CONFIRMS THE TRADE?", "confirms"), ("WHAT INVALIDATES THE TRADE?", "invalidates")):
            extra = n.get(f"{key}_extra")
            S.append(KeepTogether([p(q, H3)] + bullets(d[key] + ([extra] if extra else []))))
        S += [p("WHAT IS THE MAXIMUM RISK?", H3), p(d["max_risk"]), p("WHAT IS THE EXPECTED HOLDING PERIOD?", H3), p(d["holding"])]

        S.append(p("7–8. Entry, stop-loss, targets and reward:risk", H2))
        rows = [["Item", "Level", "Basis"],
                ["Current market price", fmt(d["cmp"]), f"{d['cmp_source']}, {d['cmp_time']}"],
                ["Entry (stop order)", fmt(d["entry"]), f"{'buy' if d['side'] == 'LONG' else 'sell'}-stop beyond the {d['signal_date']} bar; valid {STRATEGY.entry_valid_days} sessions"],
                ["Stop-loss", fmt(d["stop"]), f"invalidation {fmt(d['invalidation'])} ± {STRATEGY.stop_atr_buffer} ATR; {d['stop_atr']:.2f} ATR from entry"],
                ["Target 1 (book 50%)", fmt(d["t1"]), d["t1_basis"]],
                ["Target 2 (balance)", fmt(d["t2"]), d["t2_basis"]],
                ["Risk per share", fmt(d["risk_per_share"]), f"ATR(14) {fmt(d['atr'])}"],
                ["Reward per share", f"{fmt(d['reward_t1'])} / {fmt(d['reward_t2'])}", "to T1 / T2"],
                ["Reward:risk", f"{d['rr_t1']:.2f} / {d['rr_t2']:.2f}", "to T1 / T2 (after T1 the stop moves to entry)"],
                ["Position (paper)", f"{sz.get('qty', 0):,}" + (f" ({sz.get('lots')} lots)" if sz.get("lots") else ""),
                 f"{sz.get('note', '')}; notional Rs {fmt(sz.get('notional'), 0)} ({fmt(sz.get('capital_pct'), 1)}% of capital)"],
                ["Capital at risk", f"Rs {fmt(sz.get('risk_amount'), 0)}", f"{fmt(sz.get('risk_pct_capital'), 2)}% of capital; round-trip costs ≈ {fmt(sz.get('round_trip_cost_pct'), 2)}%"]]
        S.append(table(rows, [38 * mm, 30 * mm, W - 68 * mm]))
        if d["side"] == "SHORT":
            S.append(p("Execution note: NSE cash equities cannot be held short overnight; this short is expressed through the stock's NSE futures "
                       "contract named above (margin, not full notional, is blocked). Futures trade at a basis to the cash price; levels are cash "
                       "levels and should be translated by the prevailing basis.", SMALL))

        S.append(p("9. Invalidation conditions", H2))
        S += bullets(d["invalidates"])
        S.append(p("10. Upcoming event risk", H2))
        ev = d.get("events") or {}
        S.append(p(ev.get("summary", "not checked")))
        if ev.get("all"):
            rows = [["Event", "Date", "Source", "Retrieved"]] + [[e["event"], e["event_date"], e["source"], e.get("retrieved_at", "")]
                                                                for e in ev["all"]]
            S.append(table(rows, [52 * mm, 22 * mm, 70 * mm, 36 * mm], font=7))
        if ev.get("sources_failed"):
            S.append(p("Sources that could not be reached: " + ", ".join(ev["sources_failed"]), SMALL))
        if n.get("events_note"):
            S.append(p(n["events_note"]))

        S.append(p("Score card", H2))
        rows = [["Component", "Points", "Supporting", "Against"]]
        for c in d["scorecard"]["components"]:
            rows.append([c["name"], f"{c['points']:g} / {c['max']}", "<br/>".join(c["pros"]) or "-", "<br/>".join(c["cons"]) or "-"])
        rows.append(["<b>Total</b>", f"<b>{d['score']:.0f} / 100</b>", "", ""])
        S.append(table(rows, [34 * mm, 18 * mm, (W - 52 * mm) / 2, (W - 52 * mm) / 2], font=7))
        bts = d.get("backtest_setup")
        if bts:
            S.append(p(f"Backtest evidence for '{d['setup']}' setups (walk-forward, out-of-sample): {bts['trades']} trades, win rate "
                       f"{bts['win_rate']:.1f}%, profit factor {fmt(bts['profit_factor'])}, expectancy {fmt(bts['expectancy_r'], 3)} R "
                       f"(t = {fmt(bts['expectancy_r_t'], 1)}), median hold {bts['median_holding']:.0f} sessions.", SMALL))

    # ------------------------------------------------------------------ 11. charts index
    S += [PageBreak(), p("11. Daily Candlestick Charts", H1),
          p("Each idea's chart appears on its idea page above. Files (PNG, 150 dpi) are delivered alongside this report:")]
    S += bullets([f"{d['symbol']}: {Path(d['chart']).name}" for d in ideas])

    # ------------------------------------------------------------------ 12. limitations
    S += [p("12. Research Limitations", H1)]
    lim = [
        "Technical analysis only. No valuation, earnings-revision or ownership analysis was performed; no news-based catalyst is claimed.",
        "Event risk relies on Yahoo Finance's calendar (dates for Indian stocks can be estimates until a board meeting is notified), Kotak's "
        "scrip-master record dates and manual checks of exchange filings where recorded. NSE/BSE APIs refused scripted access from this machine.",
        "The backtest universe is today's index membership (survivorship bias); delisted stocks have no Kotak history. Shorts are simulated on "
        "cash prices as a proxy for futures. Results describe a short history (≈3 years) and a mostly rising market.",
        "Daily bars hide the intraday sequence: the simulation assumes the worse outcome when a stop and a target share a bar and never "
        "credits a target on the entry bar. Real fills, especially through gaps and at the open, can be worse than modelled.",
        "Stops are not guaranteed: an overnight gap can open beyond the stop, making the realised loss larger than the planned 1% of capital.",
        "Scores rank setups by the quality of their evidence; they are not probabilities of success.",
    ]
    S += bullets(lim + notes.get("_limitations", []))

    # ------------------------------------------------------------------ 13. sources
    S += [p("13. Data Sources and As-of Timestamp", H1)]
    rows = [["Data", "Source", "As of"],
            ["Daily OHLCV (stocks, indices, VIX)", "Kotak Neo Trade API — market-data/1.0/historical/details (interval D)", f"{ctx['as_of']} close"],
            ["Live quotes (CMP)", "Kotak Neo Trade API — script-details/1.0/quotes", ", ".join(sorted({d['cmp_time'] for d in ideas})) or "-"],
            ["Instrument master, F&O contracts, record dates", "Kotak Neo scrip master (nse_cm-v1.csv, nse_fo.csv)", ctx["scrip_master_date"]],
            ["Index constituents and industry", "NSE Indices Ltd — niftyindices.com/IndexConstituent (NIFTY 50, NEXT 50, 100, MIDCAP 150)", "files downloaded 2026-11-25"],
            ["Earnings / dividend dates", "Yahoo Finance calendar (yfinance); manual exchange checks where noted", "see each idea"],
            ["Costs", "Kotak Neo pricing page; NSE charge schedule; Budget 2026 STT (effective 2026-04-01)", "checked 2026-11-25"]]
    S.append(table(rows, [45 * mm, 95 * mm, 40 * mm]))
    S += [Spacer(1, 6), p(f"Report generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST by run_research.py (run {ctx['run_id']}). "
                          "All timestamps are IST. Paper research only — this document is not investment advice.", SMALL)]
    doc.build(S, onFirstPage=_footer, onLaterPages=_footer)
    return out
