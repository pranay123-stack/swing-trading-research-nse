"""Streamlit dashboard:  streamlit run app.py

Reads the SQLite database and the files written by run_research.py. It makes no broker calls of any kind; the only
actions it offers are PAPER simulation and report export.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from config import EXECUTION_MODE, IST, PATHS, RISK, STRATEGY, assert_paper_mode
from data.data_validator import validate
from database.repository import Repository
from execution.paper_broker import PaperBroker
from execution.paper_orders import DuplicatePaperOrder, PaperOrder, idea_key
from execution.portfolio import summary as portfolio_summary
from research.indicators import add_indicators

assert_paper_mode()
st.set_page_config(page_title="Swing Research (Paper)", layout="wide")


@st.cache_resource
def repo() -> Repository:
    return Repository()


def load_json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


R = repo()
ctx = load_json(PATHS.reports / "research_context.json")
bt = load_json(PATHS.reports / "backtest_summary.json")

st.title("Swing Trading Research — Indian Equities")
st.caption(f"Execution mode: **{EXECUTION_MODE}** (simulation only, no broker order interface exists in this application). "
           + (f"Research as of **{ctx['as_of']}** close, generated {ctx['generated_at']} IST." if ctx else "No research run yet: `python run_research.py`."))

tabs = st.tabs(["A. Market overview", "B. Screener", "C. Trade ideas", "D. Paper portfolio", "E. Reports", "F. Data health"])


def candle_fig(sym: str, idea: dict | None = None, n: int = 120):
    df = R.candles(sym)
    if df.empty:
        return None
    x = add_indicators(df).iloc[-n:]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.62, 0.18, 0.2], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=x.index, open=x.open, high=x.high, low=x.low, close=x.close, name=sym), 1, 1)
    fig.add_trace(go.Scatter(x=x.index, y=x.ema20, name="EMA20", line={"width": 1.2}), 1, 1)
    fig.add_trace(go.Scatter(x=x.index, y=x.ema50, name="EMA50", line={"width": 1.2}), 1, 1)
    if idea:
        for k, col in (("entry", "#1f4e9c"), ("stop", "#c0392b"), ("t1", "#27ae60"), ("t2", "#117a65")):
            fig.add_hline(y=idea[k], line_color=col, line_dash="dash", annotation_text=f"{k.upper()} {idea[k]:,.2f}", row=1, col=1)
    fig.add_trace(go.Bar(x=x.index, y=x.volume, name="Volume", marker_color="#95a5a6"), 2, 1)
    fig.add_trace(go.Scatter(x=x.index, y=x.rsi, name="RSI", line={"color": "#2c3e50"}), 3, 1)
    fig.add_hline(y=70, line_dash="dot", row=3, col=1)
    fig.add_hline(y=30, line_dash="dot", row=3, col=1)
    fig.update_layout(height=640, xaxis_rangeslider_visible=False, margin={"t": 30, "b": 10},
                      xaxis3={"rangebreaks": [{"bounds": ["sat", "mon"]}]})
    return fig


# ---------------------------------------------------------------------------------------------- A
with tabs[0]:
    if ctx:
        reg = ctx["regime"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Regime", reg["label"], f"{reg['score']:+.2f}")
        c2.metric("Breadth > EMA50", f"{reg['breadth_ema50']:.0f}%")
        c3.metric("Breadth > SMA200", f"{reg['breadth_sma200']:.0f}%")
        c4.metric("INDIA VIX", f"{reg['vix']:.2f}")
        st.dataframe(pd.DataFrame(reg["components"], columns=["Component", "Points", "Observation"]), width="stretch", hide_index=True)
        if ctx.get("index_quotes"):
            st.subheader("Index quotes (Kotak Neo)")
            st.dataframe(pd.DataFrame(ctx["index_quotes"].values())[["symbol", "ltp", "change_pct", "low", "high", "ts"]], hide_index=True)
        st.subheader("Sector performance")
        sec = pd.DataFrame(ctx["sectors"])
        st.plotly_chart(go.Figure(go.Bar(x=sec["rs_nifty20"], y=sec["index"], orientation="h")).update_layout(
            height=520, title="20-session return vs NIFTY 50 (%)", yaxis={"autorange": "reversed"}), width="stretch")
        st.dataframe(sec.round(2), width="stretch", hide_index=True)
    fig = candle_fig("Nifty 50", n=250)
    if fig:
        st.subheader("NIFTY 50 (daily)")
        st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------------------------------------- B
with tabs[1]:
    rid = ctx["run_id"] if ctx else R.latest_run_id()
    if rid:
        df = R.screen(rid)
        if df.empty:
            st.info("No setups in the latest run.")
        else:
            payload = pd.DataFrame([json.loads(x) for x in df["payload"]])
            c1, c2, c3 = st.columns(3)
            side = c1.multiselect("Side", ["LONG", "SHORT"], ["LONG", "SHORT"])
            only = c2.checkbox("Only setups passing every filter", False)
            mins = c3.slider("Minimum score", 0, 100, 0)
            v = payload[payload["side"].isin(side) & (payload["score"] >= mins)]
            if only:
                v = v[v["passed"]]
            st.dataframe(v, width="stretch", hide_index=True)
        if ctx:
            st.caption(f"Universe: {ctx['universe']['usable']} usable stocks; {len(ctx['universe']['liquidity_fail'])} failed liquidity; "
                       f"{len(ctx['universe']['excluded'])} excluded for data quality.")
            with st.expander("Liquidity / data exclusions"):
                st.write({**ctx["universe"]["liquidity_fail"], **ctx["universe"]["excluded"]})

# ---------------------------------------------------------------------------------------------- C
with tabs[2]:
    if ctx and ctx["ideas"]:
        if ctx.get("shortfall"):
            st.warning(f"Only {len(ctx['ideas'])} idea(s) met every criterion; shortfall {ctx['shortfall']} is reported, not filled.")
        for i, d in enumerate(ctx["ideas"], 1):
            st.subheader(f"{i}. {d['symbol']} — {d['side']} · {d['setup']} · score {d['score']:.0f}")
            c = st.columns(7)
            for col, (k, lab) in zip(c, [("cmp", "CMP"), ("entry", "Entry"), ("stop", "Stop"), ("t1", "T1"), ("t2", "T2"),
                                         ("rr_t1", "R:R T1"), ("rr_t2", "R:R T2")]):
                col.metric(lab, f"{d[k]:,.2f}")
            st.caption(f"{d['company']} · {d['industry']} · CMP {d['cmp_source']} {d['cmp_time']} · sizing: {d['sizing'].get('note')}")
            if Path(d["chart"]).exists():
                st.image(d["chart"])
            for q, k in (("Why this stock?", "why_stock"), ("Why now?", "why_now"), ("Catalyst", "catalyst"),
                         ("What confirms", "confirms"), ("What invalidates", "invalidates")):
                with st.expander(q, expanded=k == "why_now"):
                    for line in d[k]:
                        st.markdown(f"- {line}")
            st.markdown(f"**Maximum risk:** {d['max_risk']}  \n**Holding:** {d['holding']}  \n**Events:** {(d.get('events') or {}).get('summary')}")
            with st.expander("Score card"):
                st.dataframe(pd.DataFrame(d["scorecard"]["components"]), width="stretch", hide_index=True)
    else:
        st.info("No trade ideas yet.")

# ---------------------------------------------------------------------------------------------- D
with tabs[3]:
    pb = PaperBroker(R)
    st.caption("Every order here is simulated. Fills are derived from stored daily candles using the same conservative fill model as the backtest.")
    c1, c2 = st.columns(2)
    if c1.button("Place current ideas as PAPER orders") and ctx:
        for d in ctx["ideas"]:
            sz = d["sizing"]
            try:
                oid = pb.place(PaperOrder(idea_key=idea_key(d["symbol"], d["side"], d["setup"], d["signal_date"]), symbol=d["symbol"],
                                          side=d["side"], instrument=sz["instrument"], qty=sz["qty"], lot=sz["lot_size"],
                                          trigger_price=d["entry"], stop_loss=d["stop"], target1=d["t1"], target2=d["t2"],
                                          created_on=date.fromisoformat(d["signal_date"]), valid_sessions=STRATEGY.entry_valid_days,
                                          max_hold=STRATEGY.max_holding_days, notes=d["setup"]))
                st.success(f"PAPER order {oid} for {d['symbol']}")
            except DuplicatePaperOrder:
                st.info(f"{d['symbol']}: paper order already exists (duplicates are refused)")
    if c2.button("Re-simulate paper positions from latest candles"):
        pb.update()
    s = portfolio_summary(pb)
    m = st.columns(6)
    m[0].metric("Virtual capital", f"Rs {s.capital:,.0f}")
    m[1].metric("Equity", f"Rs {s.equity:,.0f}")
    m[2].metric("Realized P&L", f"Rs {s.realized:,.0f}")
    m[3].metric("Unrealized P&L", f"Rs {s.unrealized:,.0f}")
    m[4].metric("Open / closed", f"{s.open_positions} / {s.closed_positions}")
    m[5].metric("Pending orders", s.pending_orders)
    st.subheader("Orders")
    st.dataframe(pb.orders(), width="stretch", hide_index=True)
    pos = pb.positions()
    st.subheader("Open positions")
    st.dataframe(pos[pos["status"] == "OPEN"] if len(pos) else pos, width="stretch", hide_index=True)
    st.subheader("Closed positions")
    st.dataframe(pos[pos["status"] == "CLOSED"] if len(pos) else pos, width="stretch", hide_index=True)
    st.subheader("Fill history")
    st.dataframe(pb.fills(), width="stretch", hide_index=True)
    if bt:
        st.subheader("Backtest (walk-forward, out-of-sample)")
        st.json(bt["walk_forward"]["summary"])
        st.dataframe(pd.DataFrame(bt["walk_forward"]["folds"]), hide_index=True)
        eq = PATHS.reports / "backtest_equity_oos.csv"
        if eq.exists():
            e = pd.read_csv(eq, index_col=0, parse_dates=True).iloc[:, 0]
            st.line_chart(e)

# ---------------------------------------------------------------------------------------------- E
with tabs[4]:
    pdfs = sorted(PATHS.reports.glob("Swing_Trading_Research_*.pdf"))
    if ctx and st.button("Regenerate PDF from the latest research run"):
        from reporting.pdf_report import build_pdf
        st.success(f"Written {build_pdf(ctx, bt)}")
        pdfs = sorted(PATHS.reports.glob("Swing_Trading_Research_*.pdf"))
    for f in pdfs[-3:]:
        st.download_button(f"Download {f.name}", f.read_bytes(), f.name, "application/pdf")
    for f in sorted(PATHS.reports.glob("trade_ideas_*.csv"))[-3:]:
        st.download_button(f"Download {f.name}", f.read_bytes(), f.name, "text/csv")
    if ctx:
        for d in ctx["ideas"]:
            if Path(d["chart"]).exists():
                st.download_button(f"Download chart {Path(d['chart']).name}", Path(d["chart"]).read_bytes(), Path(d["chart"]).name, "image/png")

# ---------------------------------------------------------------------------------------------- F
with tabs[5]:
    cov = R.candle_coverage()
    api = R.api_log(2000)
    last_ok = api[api["status"] == 200]["ts"].max() if len(api) else None
    c = st.columns(4)
    c[0].metric("Last successful API call", str(last_ok)[:19] if last_ok else "never")
    c[1].metric("Symbols cached", len(cov))
    c[2].metric("Latest bar", cov["last_d"].max() if len(cov) else "-")
    c[3].metric("API errors (last 2000 calls)", int((api["status"] != 200).sum()) if len(api) else 0)
    if len(cov):
        latest = cov["last_d"].max()
        cov["fresh"] = cov["last_d"] == latest
        st.subheader("Data freshness")
        st.dataframe(cov, width="stretch", hide_index=True)
        cal = R.candles("Nifty 50").index
        rows = []
        for s in cov["symbol"]:
            rep = validate(s, R.candles(s), cal, is_index=" " in s)
            rows.append(rep.as_row())
        st.subheader("Validation (missing candles, OHLC errors, staleness)")
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.subheader("API log")
    st.dataframe(api, width="stretch", hide_index=True)
    st.caption(f"Now {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
