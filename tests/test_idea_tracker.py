"""Published ideas are carried forward with their original levels and a status derived from real candles."""

from __future__ import annotations

import json

import pytest

from execution.idea_tracker import live_ideas, place_paper, published_ideas
from execution.paper_broker import PaperBroker


def idea_on(candles, d="2026-06-10"):
    nxt = candles["RELIANCE"].loc[d:].iloc[1]
    trig = round(float(nxt.high) - 1.0, 2)
    return {"symbol": "RELIANCE", "side": "LONG", "setup": "Breakout", "signal_date": d, "entry": trig,
            "stop": round(trig * 0.95, 2), "t1": round(trig * 1.04, 2), "t2": round(trig * 1.08, 2),
            "sizing": {"executable": True, "qty": 20, "instrument": "EQ_DELIVERY", "lot_size": 1}}


def publish(repo, idea, run_id="r1"):
    repo.start_run(run_id)
    repo.save_ideas(run_id, [idea])
    repo.finish_run(run_id, status="DONE")


def test_carry_forward_status(repo, candles):
    import pandas as pd
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    idea = idea_on(candles)
    publish(repo, idea)
    pb = PaperBroker(repo)
    assert place_paper(pb, idea) is not None
    assert place_paper(pb, idea) is None  # idempotent: duplicates refused
    day2 = candles["RELIANCE"].loc["2026-06-10":].index[2].date()
    live = live_ideas(repo, day2)
    assert len(live) == 1
    c = live[0]
    assert c["origin"] == "carried" and c["issued_on"] == "2026-06-10"
    assert (c["entry"], c["stop"], c["t1"], c["t2"]) == (idea["entry"], idea["stop"], idea["t1"], idea["t2"])  # never re-fitted
    assert c["status"]["state"] in ("OPEN", "PENDING")
    assert c["status_text"]


def test_resolved_ideas_are_not_carried(repo, candles):
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    idea = idea_on(candles, "2025-06-10")
    publish(repo, idea)
    place_paper(PaperBroker(repo), idea)
    assert live_ideas(repo, candles["RELIANCE"].index[-1].date()) == []  # resolved long before the data end


def test_carried_payloads_are_not_republished(repo, candles):
    idea = idea_on(candles)
    publish(repo, idea, "r1")
    publish(repo, {**idea, "origin": "carried"}, "r2")
    assert list(published_ideas(repo)) == ["RELIANCE|LONG|Breakout|2026-06-10"]
