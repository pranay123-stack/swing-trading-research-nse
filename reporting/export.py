"""CSV / JSON exports."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

IDEA_COLUMNS = ["rank", "symbol", "company", "exchange", "side", "setup", "instrument", "executable", "cmp", "cmp_time",
                "entry", "stop", "t1", "t2", "risk_per_share", "reward_t1", "reward_t2", "rr_t1", "rr_t2", "atr",
                "stop_atr", "qty", "lots", "notional", "capital_pct", "risk_amount", "risk_pct_capital",
                "score", "confidence", "holding", "signal_date", "invalidation", "event_summary"]


def ideas_csv(ideas: list[dict], path: Path) -> Path:
    rows = []
    for i, d in enumerate(ideas, 1):
        sz = d.get("sizing") or {}
        rows.append({"rank": i, **{k: d.get(k) for k in IDEA_COLUMNS if k in d},
                     "instrument": sz.get("instrument"), "executable": sz.get("executable"), "qty": sz.get("qty"),
                     "lots": sz.get("lots"), "notional": round(sz.get("notional", 0), 2),
                     "capital_pct": round(sz.get("capital_pct", 0), 2), "risk_amount": round(sz.get("risk_amount", 0), 2),
                     "risk_pct_capital": round(sz.get("risk_pct_capital", 0), 2),
                     "event_summary": (d.get("events") or {}).get("summary")})
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=IDEA_COLUMNS).to_csv(path, index=False)
    return path


def json_dump(obj, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))
    return path
