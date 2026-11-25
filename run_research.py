"""End-to-end research workflow.

    python run_research.py                 # sync data, scan, select, chart, backtest, PDF
    python run_research.py --no-sync       # reuse cached candles (still validates freshness)
    python run_research.py --skip-backtest # reuse the last saved backtest results
    python run_research.py --paper         # also place the selected ideas as PAPER orders (simulation only)

Paper-only system: see config.EXECUTION_MODE. No step of this workflow can send an order to a broker.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import date, datetime

import pandas as pd

from config import IST, PATHS, RISK, STRATEGY, UNIVERSE, assert_paper_mode
from data.historical_data import session_is_final
from data.instrument_master import InstrumentMaster, read_constituents
from data.kotak_client import KotakError, KotakMarketDataClient
from data.sync import DEFAULT_HISTORY_START, index_symbols, sync_all
from database.repository import Repository
from reporting.export import ideas_csv, json_dump
from research import support_resistance as sr
from research.event_analysis import check as event_check
from research.risk_manager import size
from research.sector_analysis import sector_table
from research.stock_screener import Candidate, ResearchData, load_research_data, regime_at, screen_at, select

log = logging.getLogger("research")


def confidence(score: float) -> str:
    return "High" if score >= 78 else "Medium" if score >= 68 else "Moderate"


def live_quotes(client: KotakMarketDataClient | None, repo: Repository, rd: ResearchData, symbols: list[str]) -> dict:
    """Kotak quotes for the selected stocks + market indices. Returns {symbol: {ltp, ts, ...}}; empty if unavailable."""
    if client is None:
        return {}
    q = [f"nse_cm|{rd.universe[s].token}" for s in symbols] + [f"nse_cm|{n}" for n in ("Nifty 50", "Nifty Bank", "INDIA VIX")]
    try:
        rows = client.quotes(q, "all")
    except KotakError as e:
        log.warning("quotes failed: %s", e)
        return {}
    tok2sym = {rd.universe[s].token: s for s in symbols}
    out, save = {}, []
    for r in rows:
        key = tok2sym.get(str(r.get("exchange_token")), str(r.get("exchange_token")))
        try:
            ts = datetime.fromtimestamp(int(r.get("lstup_time", 0)), IST).isoformat(timespec="seconds")
            ohlc = r.get("ohlc", {})
            rec = {"symbol": key, "ts": ts, "ltp": float(r["ltp"]), "open": float(ohlc.get("open", 0)),
                   "high": float(ohlc.get("high", 0)), "low": float(ohlc.get("low", 0)),
                   "prev_close": float(ohlc.get("close", 0)), "change_pct": float(r.get("per_change", 0)), "raw": r}
        except (KeyError, ValueError, TypeError):
            continue
        out[key] = rec
        save.append(rec)
    repo.save_quotes(save)
    repo.log_api_calls(client.calls)
    client.calls.clear()
    return out


def chart_levels(rd: ResearchData, c: Candidate) -> dict:
    df = rd.stocks[c.symbol]
    t = int(df.index.get_loc(c.setup.signal_date))
    lv = sr.levels_at(df, rd.pivots[c.symbol], t, c.setup.atr)
    px = c.setup.close
    s = c.setup
    trade = (s.entry, s.stop, s.t1, s.t2)
    far = lambda x: all(abs(x.price - y) > 0.3 * s.atr for y in trade)  # noqa: E731  (don't draw S/R on top of a trade line)
    sup = [round(x.price, 2) for x in sr.levels_below(lv, px) if x.major and far(x)][:2]
    res = [round(x.price, 2) for x in sr.levels_above(lv, px) if x.major and far(x)][:2]
    return {"supports": sup, "resistances": res}


def build_idea(rd: ResearchData, c: Candidate, regime, sectors: pd.DataFrame, quotes: dict, bt_setup: pd.DataFrame | None,
               scrip_date: str) -> dict:
    s, eq = c.setup, rd.universe[c.symbol]
    df = rd.stocks[c.symbol]
    t = int(df.index.get_loc(s.signal_date))
    r = df.iloc[t]
    rs = rd.rs[c.symbol].iloc[t]
    q = quotes.get(c.symbol)
    sec_row = sectors[sectors["index"] == eq.sector_index]
    sec = sec_row.iloc[0].to_dict() if len(sec_row) else {}
    card = c.card.as_dict()
    L = s.side == "LONG"
    sz = c.sizing or size(s, eq, s.signal_date.date()).as_dict()
    bt = None
    if bt_setup is not None and len(bt_setup):
        m = bt_setup[bt_setup["setup"] == s.setup]
        if len(m):
            bt = m.iloc[0].to_dict()
    word = "long" if L else "short"
    why_stock = [
        f"{eq.company} ({eq.industry}; member of {', '.join(eq.indices)}). Median 20-day traded value Rs {c.liquidity['median_value_cr']:,.0f} crore: "
        f"liquid enough to enter and exit a swing position without moving the price.",
        f"Relative strength: {rs['ret20']:+.1f}% over 20 sessions vs NIFTY 50 {rs['ret20'] - rs['rs_nifty20']:+.1f}% "
        f"({rs['rs_nifty20']:+.1f}% excess); {rs['rs_nifty60']:+.1f}% excess over 60 sessions"
        + (f"; {rs['rs_sector20']:+.1f}% vs its sector index ({eq.sector_index})." if pd.notna(rs.get("rs_sector20")) else "."),
    ]
    if sec:
        why_stock.append(f"Sector backdrop: {eq.sector_index} is {sec['ret20']:+.1f}% over 20 sessions ({sec['rs_nifty20']:+.1f}% vs NIFTY), "
                         f"rotation quadrant '{sec['quadrant']}'" + ("" if eq.sector_index_exact else " (nearest available sector index)") + ".")
    why_now = list(s.facts) + [
        f"Trend: close {r.close:,.2f} vs EMA20 {r.ema20:,.2f} / EMA50 {r.ema50:,.2f} / SMA200 {r.sma200:,.2f}; ADX {r.adx:.0f}; RSI {r.rsi:.0f}.",
        f"Market regime {regime.label} ({regime.score:+.2f}): {'supports' if (L and regime.score > 0) or (not L and regime.score < 0) else 'works against'} a {word} position.",
    ]
    catalyst = [f"Technical catalyst: the {s.setup.lower()} on {s.signal_date.date()} "
                f"({'volume ' + format(s.features.get('rvol', 0), '.1f') + 'x average' if s.features.get('rvol') else ''})."]
    if sec and ((L and sec["rs_nifty20"] > 0) or (not L and sec["rs_nifty20"] < 0)):
        catalyst.append(f"Sector rotation: {eq.sector_index} {'leading' if L else 'lagging'} the index over 20 sessions.")
    catalyst.append("No company-specific news catalyst is claimed; scheduled events found are listed under event risk.")
    confirms = [f"A {'buy' if L else 'sell'}-stop trigger at {s.entry:,.2f} ({'above' if L else 'below'} the signal bar's {'high' if L else 'low'}); "
                f"the order is valid for {STRATEGY.entry_valid_days} sessions and is cancelled if {s.stop:,.2f} trades first.",
                f"Follow-through volume at or above the 20-day average ({r.vol_sma20 / 1e5:,.1f} lakh shares) on the trigger day.",
                f"Holding {'above' if L else 'below'} {'EMA20 ' + format(r.ema20, ',.2f') if 'Pullback' not in s.setup else 'EMA50 ' + format(r.ema50, ',.2f')} on closing basis."]
    invalid = [f"Stop-loss {s.stop:,.2f}: {s.invalidation_reason} (invalidation {s.invalidation:,.2f} minus a "
               f"{STRATEGY.stop_atr_buffer} ATR buffer)." if L else
               f"Stop-loss {s.stop:,.2f}: {s.invalidation_reason} (invalidation {s.invalidation:,.2f} plus a {STRATEGY.stop_atr_buffer} ATR buffer).",
               f"Time stop: exit if neither target is reached within {STRATEGY.max_holding_days} sessions.",
               f"Regime change: a NIFTY 50 close below its 50-day EMA ({rd.indices['Nifty 50']['ema50'].iloc[-1]:,.0f}) would remove the market tailwind."
               if L else f"Regime change: broad strength (NIFTY 50 making new highs with breadth above 65%) would argue for covering early."]
    risk_amt = sz.get("risk_amount", 0)
    max_risk = (f"Rs {s.risk:,.2f} per share ({s.risk / s.entry * 100:.2f}% of entry, {s.stop_atr:.1f} ATR). Position "
                f"{sz.get('qty', 0):,} {'shares' if L else 'units'} -> Rs {risk_amt:,.0f} at the stop = {sz.get('risk_pct_capital', 0):.2f}% "
                f"of Rs {RISK.paper_capital / 1e5:.0f} lakh capital, plus ~{sz.get('round_trip_cost_pct', 0):.2f}% round-trip costs. "
                f"A gap through the stop can exceed this.")
    hold = f"{STRATEGY.min_holding_days}-{STRATEGY.max_holding_days} sessions (1-3 weeks); time stop at {STRATEGY.max_holding_days}"
    if bt:
        hold += f"; in the backtest this setup's trades lasted a median {bt['median_holding']:.0f} sessions"
    return {
        "symbol": s.symbol, "company": eq.company, "exchange": "NSE", "industry": eq.industry, "indices": eq.indices,
        "sector_index": eq.sector_index, "side": s.side, "setup": s.setup, "signal_date": str(s.signal_date.date()),
        "cmp": q["ltp"] if q else s.close, "cmp_time": q["ts"] if q else f"{s.signal_date.date()} close",
        "cmp_source": "Kotak Neo quotes API" if q else "Kotak Neo historical (last close)",
        "last_close": s.close, "entry": s.entry, "stop": s.stop, "t1": s.t1, "t2": s.t2,
        "t1_basis": s.t1_basis, "t2_basis": s.t2_basis, "invalidation": s.invalidation,
        "risk_per_share": round(s.risk, 2), "reward_t1": round(abs(s.t1 - s.entry), 2), "reward_t2": round(abs(s.t2 - s.entry), 2),
        "rr_t1": round(s.rr_t1, 2), "rr_t2": round(s.rr_t2, 2), "atr": round(s.atr, 2), "stop_atr": round(s.stop_atr, 2),
        "score": round(c.card.total, 1), "confidence": confidence(c.card.total), "scorecard": card,
        "sizing": sz, "holding": hold, "regime": regime.label, "events": c.events,
        "why_stock": why_stock, "why_now": why_now, "catalyst": catalyst, "confirms": confirms, "invalidates": invalid,
        "max_risk": max_risk, "backtest_setup": bt, "chart_levels": chart_levels(rd, c),
        "indicators": {k: (round(float(r[k]), 2) if pd.notna(r[k]) else None) for k in
                       ("ema9", "ema20", "ema50", "sma100", "sma200", "adx", "rsi", "macd", "macd_signal", "macd_hist",
                        "stochrsi_k", "roc5", "roc10", "roc20", "atr", "bb_upper", "bb_lower", "bb_width", "hv20", "rvol", "obv")},
        "scrip_master_date": scrip_date,
    }


def main(argv: list[str] | None = None) -> int:
    assert_paper_mode()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--no-yahoo", action="store_true", help="skip the Yahoo Finance event calendar")
    ap.add_argument("--paper", action="store_true", help="place the selected ideas as PAPER orders")
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args(argv)
    for p in (PATHS.logs, PATHS.reports, PATHS.charts):
        p.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(PATHS.logs / "research.log"), logging.StreamHandler(sys.stdout)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    repo = Repository()
    run_id = datetime.now(IST).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    repo.start_run(run_id)
    try:
        client = None
        try:
            client = KotakMarketDataClient()
        except KotakError as e:
            log.error("Kotak Neo market data unavailable: %s", e)
        print(f"[1] Kotak Neo market data: {'connected (read-only)' if client else 'NOT available'}")
        if not a.no_sync:
            if client is None:
                raise SystemExit("no Kotak credentials: cannot fetch current market data (use --no-sync to reuse the cache)")
            print("[2] syncing instrument master + daily candles ...")
            res = sync_all(client, repo, DEFAULT_HISTORY_START, verbose=False)
            print(f"    synced {len(res['stocks'])} stocks, {len(res['indices'])} indices; errors: {len(res['errors'])}")
        master = InstrumentMaster(client)
        cons = read_constituents()
        universe = master.build_universe(cons)
        scrip_date = master.file_date("nse_cm")
        missing = sorted(set(cons) - set(universe))
        rd = load_research_data(repo, universe, index_symbols())
        d = rd.calendar[-1]
        now = datetime.now(IST)
        stale = [s for s, df in rd.stocks.items() if df.index[-1] != d]
        print(f"[3] data validated: {len(rd.stocks)} stocks usable, {len(rd.excluded)} excluded, as-of {d.date()} "
              f"(final: {session_is_final(d.date(), now)}); stale symbols: {len(stale)}")
        regime = regime_at(rd, d)
        sectors = sector_table(rd.indices, t=d)
        print(f"[4] market regime: {regime.label} ({regime.score:+.2f})")
        reg, cands, skipped = screen_at(rd, d, regime=regime)
        print(f"[5] candidates: {len(cands)} setups on {len({c.symbol for c in cands})} stocks; "
              f"{sum(c.passed for c in cands)} pass all filters; {len(skipped)} stocks failed liquidity")

        bt_path = PATHS.reports / "backtest_summary.json"
        if not a.skip_backtest:
            from backtesting.report import run_backtest
            print("[6] backtesting (walk-forward, out-of-sample) ...")
            run_backtest(rd, verbose=True)
        bt = json.loads(bt_path.read_text()) if bt_path.exists() else None
        bt_setup = pd.DataFrame(bt["by_setup_oos"]) if bt and bt.get("by_setup_oos") else None

        use_yahoo = not a.no_yahoo
        chosen, notes = select(
            rd, cands, d, a.n,
            event_check=lambda c: event_check(rd.universe[c.symbol], d.date(), STRATEGY.max_holding_days, use_yahoo, scrip_date),
            sizer=lambda c: size(c.setup, rd.universe[c.symbol], d.date()))
        print(f"[7] selected {len(chosen)} of {a.n} requested")
        for s, why in notes:
            print(f"    skipped {s}: {why}")
        quotes = live_quotes(client, repo, rd, [c.symbol for c in chosen])
        ideas = [build_idea(rd, c, regime, sectors, quotes, bt_setup, scrip_date) for c in chosen]

        from reporting.chart_generator import candlestick_chart
        for idea in ideas:
            idea["chart"] = str(candlestick_chart(rd.stocks[idea["symbol"]].loc[:d], idea))
        print(f"[8] charts: {[i['chart'] for i in ideas]}")

        # persist
        repo.save_screen(run_id, [c.row() for c in cands])
        repo.save_ideas(run_id, ideas)
        context = {
            "run_id": run_id, "as_of": str(d.date()), "generated_at": now.isoformat(timespec="seconds"),
            "regime": {"label": regime.label, "score": regime.score, "components": regime.components,
                       "breadth_ema50": regime.breadth_ema50, "breadth_sma200": regime.breadth_sma200, "vix": regime.vix},
            "index_quotes": {k: v for k, v in quotes.items() if k in ("Nifty 50", "Nifty Bank", "INDIA VIX")},
            "sectors": sectors.to_dict("records"),
            "universe": {"constituents": len(cons), "matched": len(universe), "missing_from_master": missing,
                         "usable": len(rd.stocks), "excluded": rd.excluded, "liquidity_fail": skipped,
                         "filters": UNIVERSE.__dict__},
            "screen": {"candidates": len(cands), "passed": sum(c.passed for c in cands),
                       "top": [c.row() for c in cands[:25]], "selection_notes": notes},
            "ideas": ideas, "shortfall": max(0, a.n - len(ideas)), "scrip_master_date": scrip_date,
            "strategy": STRATEGY.__dict__, "risk": RISK.__dict__,
        }
        json_dump(context, PATHS.reports / "research_context.json")
        ideas_csv(ideas, PATHS.reports / f"trade_ideas_{d.date()}.csv")
        from reporting.pdf_report import build_pdf
        pdf = build_pdf(context, bt)
        print(f"[9] PDF: {pdf}")
        from reporting.readme_report import update_readme
        if update_readme():
            print("    README report section updated")
        if a.paper:
            from execution.paper_broker import PaperBroker
            from execution.paper_orders import DuplicatePaperOrder, PaperOrder, idea_key
            pb = PaperBroker(repo)
            for i in ideas:
                sz = i["sizing"]
                try:
                    oid = pb.place(PaperOrder(
                        idea_key=idea_key(i["symbol"], i["side"], i["setup"], i["signal_date"]), symbol=i["symbol"],
                        side=i["side"], instrument=sz["instrument"], qty=sz["qty"], lot=sz["lot_size"],
                        trigger_price=i["entry"], stop_loss=i["stop"], target1=i["t1"], target2=i["t2"],
                        created_on=date.fromisoformat(i["signal_date"]), valid_sessions=STRATEGY.entry_valid_days,
                        max_hold=STRATEGY.max_holding_days, notes=i["setup"]))
                    print(f"    PAPER order {oid} {i['side']} {sz['qty']} {i['symbol']} stop-entry {i['entry']}")
                except DuplicatePaperOrder as e:
                    print(f"    {e}")
            pb.update()
        repo.finish_run(run_id, status="DONE", as_of=str(d.date()), regime=regime.label, n_universe=len(rd.stocks),
                        n_candidates=len(cands), n_selected=len(ideas),
                        notes=f"shortfall {max(0, a.n - len(ideas))}" if len(ideas) < a.n else "")
        if len(ideas) < a.n:
            print(f"NOTE: only {len(ideas)} idea(s) met every criterion; the shortfall is reported, not filled.")
        return 0
    except Exception as e:
        repo.finish_run(run_id, status="FAILED", notes=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
