"""Chart, PDF and CSV generation from real fixture data."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from config import STRATEGY
from reporting.chart_generator import candlestick_chart
from reporting.export import ideas_csv
from reporting.pdf_report import TITLE, build_pdf
from research.risk_manager import size
from research.sector_analysis import sector_table
from research.stock_screener import regime_at, screen_at
from run_research import build_idea


@pytest.fixture(scope="module")
def idea_ctx(research_data):
    rd = research_data
    for d in rd.calendar[-300:][::-1]:
        _, cands, _ = screen_at(rd, d, replace(STRATEGY, min_score=-1))
        ok = [c for c in cands if not c.setup.reject_reason]
        if ok:
            break
    c = ok[0]
    c.sizing = size(c.setup, rd.universe[c.symbol], d.date()).as_dict()
    c.events = {"summary": "test: events not checked", "events": [], "all": [], "sources_ok": [], "sources_failed": []}
    reg = regime_at(rd, d)
    sec = sector_table(rd.indices, t=d)
    idea = build_idea(rd, c, reg, sec, {}, None, "2026-11-25")
    return rd, d, idea, reg, sec


def test_chart_png(idea_ctx, tmp_path):
    rd, d, idea, _, _ = idea_ctx
    p = candlestick_chart(rd.stocks[idea["symbol"]].loc[:d], idea, tmp_path / "c.png")
    data = p.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 50_000


def test_idea_answers_every_question(idea_ctx):
    _, _, idea, _, _ = idea_ctx
    for k in ("why_stock", "why_now", "catalyst", "confirms", "invalidates"):
        assert idea[k] and all(isinstance(x, str) and len(x) > 10 for x in idea[k])
    assert idea["max_risk"] and idea["holding"]
    assert "guarantee" not in str(idea).lower() and "sure-shot" not in str(idea).lower() and "risk-free" not in str(idea).lower()


def test_pdf_and_csv(idea_ctx, tmp_path):
    rd, d, idea, reg, sec = idea_ctx
    idea["chart"] = str(candlestick_chart(rd.stocks[idea["symbol"]].loc[:d], idea, tmp_path / "c.png"))
    ctx = {"run_id": "test", "as_of": str(d.date()), "generated_at": "test",
           "regime": {"label": reg.label, "score": reg.score, "components": reg.components, "breadth_ema50": reg.breadth_ema50,
                      "breadth_sma200": reg.breadth_sma200, "vix": reg.vix},
           "index_quotes": {}, "sectors": sec.to_dict("records"),
           "universe": {"usable": len(rd.stocks), "constituents": 10, "matched": 10, "missing_from_master": [], "excluded": {},
                        "liquidity_fail": {}},
           "screen": {"candidates": 1, "passed": 1, "top": [], "selection_notes": []},
           "ideas": [idea], "shortfall": 2, "scrip_master_date": "2026-11-25"}
    pdf = build_pdf(ctx, None, tmp_path / "r.pdf")
    raw = pdf.read_bytes()
    assert raw[:5] == b"%PDF-" and len(raw) > 100_000
    from pypdf import PdfReader  # reportlab ships no reader; pypdf is installed with the test deps
    text = "".join(pg.extract_text() for pg in PdfReader(str(pdf)).pages)
    assert TITLE.split("—")[0].strip() in text
    for q in ("WHY THIS STOCK?", "WHY NOW?", "WHAT IS THE CATALYST?", "WHAT CONFIRMS THE TRADE?", "WHAT INVALIDATES THE TRADE?",
              "WHAT IS THE MAXIMUM RISK?", "WHAT IS THE EXPECTED HOLDING PERIOD?", "Shortfall", "Research Limitations",
              "Data Sources and As-of Timestamp"):
        assert q in text, q
    csv = ideas_csv([idea], tmp_path / "i.csv")
    df = pd.read_csv(csv)
    assert df.iloc[0]["symbol"] == idea["symbol"] and df.iloc[0]["entry"] == pytest.approx(idea["entry"])
