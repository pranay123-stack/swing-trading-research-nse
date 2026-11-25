"""Upcoming event risk inside the holding window.

Sources, in order of authority. Each event carries its source and retrieval timestamp; nothing is inferred or invented.
  1. data/events_verified.json: events checked by hand on the exchange's own pages (NSE corporate filings / event
     calendar), recorded with the URL and time of the check. nseindia.com and api.bseindia.com refuse scripted access
     from this network (HTTP 403, checked 2026-11-25), so the final shortlist is verified manually in a browser.
  2. Kotak Neo scrip master (exchange-supplied): pRecordDate / book-closure dates.
  3. Yahoo Finance calendar via yfinance: next earnings date and ex-dividend date. Yahoo earnings dates for Indian
     stocks are frequently ESTIMATES until the company announces its board meeting; they are labelled as such.
If no source is reachable, the check reports "unverified" rather than "no events".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from config import IST, PATHS
from data.instrument_master import Equity

log = logging.getLogger(__name__)

HARD_EVENTS = ("earnings", "results", "board meeting")


@dataclass
class EventCheck:
    symbol: str
    window_start: date
    window_end: date
    events: list[dict] = field(default_factory=list)  # in-window events
    all_events: list[dict] = field(default_factory=list)  # everything found, for the report
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)

    @property
    def hard_conflict(self) -> bool:
        """An earnings / results / board-meeting date inside the holding window."""
        return any(any(k in e["event"].lower() for k in HARD_EVENTS) for e in self.events)

    @property
    def verified(self) -> bool:
        return bool(self.sources_ok)

    def summary(self) -> str:
        if not self.verified:
            return "event risk UNVERIFIED (no source reachable)"
        if not self.events:
            return f"no scheduled event found in {self.window_start}..{self.window_end} ({', '.join(self.sources_ok)})"
        return "; ".join(f"{e['event']} {e['event_date']} [{e['source']}]" for e in self.events)


def _in(d: date | None, a: date, b: date) -> bool:
    return d is not None and a <= d <= b


def load_verified(path=PATHS.events_manual) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def yahoo_calendar(symbol: str) -> list[dict]:
    import yfinance as yf

    cal = yf.Ticker(f"{symbol}.NS").calendar or {}
    ts = datetime.now(IST).isoformat(timespec="seconds")
    out = []
    for d in cal.get("Earnings Date", []) or []:
        out.append({"event": "Earnings (Yahoo date, may be an estimate)", "event_date": str(d),
                    "source": f"Yahoo Finance calendar {symbol}.NS", "retrieved_at": ts})
    if cal.get("Ex-Dividend Date"):
        out.append({"event": "Ex-dividend", "event_date": str(cal["Ex-Dividend Date"]),
                    "source": f"Yahoo Finance calendar {symbol}.NS", "retrieved_at": ts})
    return out


def check(eq: Equity, start: date, sessions: int = 15, use_yahoo: bool = True, scrip_file_date: str = "") -> EventCheck:
    end = start + timedelta(days=int(sessions * 7 / 5) + 3)  # sessions -> calendar days, with a holiday margin
    ec = EventCheck(eq.symbol, start, end)
    # 1. manually verified exchange events
    ver = [e for e in load_verified() if e.get("symbol") == eq.symbol]
    if ver:
        ec.sources_ok.append("company / exchange filings (manual web check)")
        ec.all_events += ver
    # 2. scrip master record date
    if eq.last_record_date:
        ec.sources_ok.append("Kotak scrip master")
        ec.all_events.append({"event": "Record date (scrip master)", "event_date": str(eq.last_record_date),
                              "source": f"Kotak Neo scrip master nse_cm {scrip_file_date}".strip(), "retrieved_at": scrip_file_date})
    # 3. Yahoo
    if use_yahoo:
        try:
            ec.all_events += yahoo_calendar(eq.symbol)
            ec.sources_ok.append("Yahoo Finance calendar")
        except Exception as e:  # network / parsing; reported, not hidden
            ec.sources_failed.append(f"Yahoo Finance: {type(e).__name__}")
            log.warning("yahoo calendar %s: %s", eq.symbol, e)
    for e in ec.all_events:
        try:
            d = date.fromisoformat(str(e["event_date"])[:10])
        except (TypeError, ValueError):
            continue
        if _in(d, start, end):
            ec.events.append(e)
    return ec
