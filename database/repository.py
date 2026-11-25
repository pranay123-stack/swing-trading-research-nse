"""SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config import IST, PATHS
from database.models import SCHEMA


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


class Repository:
    def __init__(self, path: Path | str = PATHS.db):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ candles
    def upsert_candles(self, symbol: str, df: pd.DataFrame, source: str) -> int:
        if df.empty:
            return 0
        ts = now_ist()
        rows = [
            (symbol, pd.Timestamp(d).date().isoformat(), float(r.open), float(r.high), float(r.low),
             float(r.close), float(r.volume), source, ts)
            for d, r in df.iterrows()
        ]
        with self.tx() as c:
            c.executemany(
                "INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,d) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                "volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at",
                rows,
            )
        return len(rows)

    def candles(self, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        q = "SELECT d, open, high, low, close, volume FROM candles WHERE symbol=?"
        args: list[Any] = [symbol]
        if start:
            q += " AND d>=?"
            args.append(start.isoformat())
        if end:
            q += " AND d<=?"
            args.append(end.isoformat())
        df = pd.read_sql_query(q + " ORDER BY d", self.conn, params=args)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], name="d"))
        df["d"] = pd.to_datetime(df["d"])
        return df.set_index("d")

    def candle_symbols(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT symbol FROM candles")]

    def candle_coverage(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT symbol, MIN(d) first_d, MAX(d) last_d, COUNT(*) n, MAX(fetched_at) fetched_at "
            "FROM candles GROUP BY symbol ORDER BY symbol", self.conn)

    def log_fetch(self, symbol: str, f: date, t: date, n: int) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)", (symbol, f.isoformat(), t.isoformat(), n, now_ist()))

    def fetched_windows(self, symbol: str) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self.conn.execute("SELECT from_d, to_d FROM fetch_log WHERE symbol=?", (symbol,))}

    # ------------------------------------------------------------------ api log / quotes
    def log_api_calls(self, records: Iterable[Any]) -> None:
        rows = [(datetime.fromtimestamp(r.ts, IST).isoformat(timespec="seconds"), r.method, r.path, r.status, r.ms, r.error)
                for r in records]
        if rows:
            with self.tx() as c:
                c.executemany("INSERT INTO api_log (ts, method, path, status, ms, error) VALUES (?,?,?,?,?,?)", rows)

    def api_log(self, limit: int = 500) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM api_log ORDER BY id DESC LIMIT ?", self.conn, params=[limit])

    def save_quotes(self, rows: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?,?)", [
                (r["symbol"], r["ts"], r.get("ltp"), r.get("open"), r.get("high"), r.get("low"), r.get("prev_close"),
                 r.get("change_pct"), json.dumps(r.get("raw", {}))) for r in rows])

    def latest_quotes(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT q.* FROM quotes q JOIN (SELECT symbol, MAX(ts) ts FROM quotes GROUP BY symbol) m "
            "USING (symbol, ts) ORDER BY symbol", self.conn)

    # ------------------------------------------------------------------ research runs
    def start_run(self, run_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO research_runs (run_id, started_at, status) VALUES (?,?,?)", (run_id, now_ist(), "RUNNING"))

    def finish_run(self, run_id: str, **kw: Any) -> None:
        kw.setdefault("finished_at", now_ist())
        cols = ", ".join(f"{k}=?" for k in kw)
        with self.tx() as c:
            c.execute(f"UPDATE research_runs SET {cols} WHERE run_id=?", (*kw.values(), run_id))

    def runs(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM research_runs ORDER BY started_at DESC", self.conn)

    def latest_run_id(self, status: str = "DONE") -> str | None:
        r = self.conn.execute("SELECT run_id FROM research_runs WHERE status=? ORDER BY started_at DESC LIMIT 1", (status,)).fetchone()
        return r[0] if r else None

    def save_screen(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO screen_results VALUES (?,?,?,?,?,?,?,?)", [
                (run_id, r["symbol"], r.get("side"), r.get("setup"), r.get("score"), int(bool(r.get("passed"))),
                 r.get("reject_reason", ""), json.dumps(r, default=str)) for r in rows])

    def screen(self, run_id: str) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM screen_results WHERE run_id=? ORDER BY score DESC", self.conn, params=[run_id])

    def save_ideas(self, run_id: str, ideas: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM trade_ideas WHERE run_id=?", (run_id,))
            c.executemany("INSERT INTO trade_ideas VALUES (?,?,?,?)",
                          [(run_id, i + 1, d["symbol"], json.dumps(d, default=str)) for i, d in enumerate(ideas)])

    def ideas(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM trade_ideas WHERE run_id=? ORDER BY rank", (run_id,))]
