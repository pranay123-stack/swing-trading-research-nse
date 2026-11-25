"""Pydantic record models and the SQLite schema."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["LONG", "SHORT"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,          -- trading symbol (e.g. RELIANCE) or exact index name (e.g. 'Nifty 50')
    d TEXT NOT NULL,               -- session date, ISO, IST
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL,          -- 'kotak_neo_historical'
    fetched_at TEXT NOT NULL,      -- ISO timestamp IST
    PRIMARY KEY (symbol, d)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol TEXT NOT NULL, from_d TEXT NOT NULL, to_d TEXT NOT NULL, n INTEGER NOT NULL,
    fetched_at TEXT NOT NULL, PRIMARY KEY (symbol, from_d, to_d)
);
CREATE TABLE IF NOT EXISTS api_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, method TEXT, path TEXT,
    status INTEGER, ms REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT NOT NULL, ts TEXT NOT NULL, ltp REAL, open REAL, high REAL, low REAL, prev_close REAL,
    change_pct REAL, raw TEXT, PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, as_of TEXT, status TEXT,
    regime TEXT, n_universe INTEGER, n_candidates INTEGER, n_selected INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS screen_results (
    run_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT, setup TEXT, score REAL, passed INTEGER,
    reject_reason TEXT, payload TEXT, PRIMARY KEY (run_id, symbol, side, setup)
);
CREATE TABLE IF NOT EXISTS trade_ideas (
    run_id TEXT NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (run_id, rank)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY, idea_key TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, side TEXT NOT NULL,
    instrument TEXT NOT NULL, order_type TEXT NOT NULL, qty INTEGER NOT NULL, trigger_price REAL,
    limit_price REAL, stop_loss REAL NOT NULL, target1 REAL NOT NULL, target2 REAL NOT NULL,
    created_on TEXT NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode = 'PAPER'),
    notes TEXT
);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, d TEXT NOT NULL, action TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, costs REAL NOT NULL, reason TEXT NOT NULL,
    UNIQUE (order_id, action, reason, d)
);
CREATE TABLE IF NOT EXISTS paper_positions (
    order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, qty_open INTEGER NOT NULL,
    entry_price REAL NOT NULL, entry_d TEXT NOT NULL, stop REAL NOT NULL, target1 REAL NOT NULL, target2 REAL NOT NULL,
    t1_done INTEGER NOT NULL DEFAULT 0, realized REAL NOT NULL DEFAULT 0, costs REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL, last_d TEXT, last_close REAL, exit_d TEXT
);
"""


class Candle(BaseModel):
    d: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)


class EventRecord(BaseModel):
    symbol: str
    event: str
    event_date: date | None
    source: str
    retrieved_at: datetime
    detail: str = ""
