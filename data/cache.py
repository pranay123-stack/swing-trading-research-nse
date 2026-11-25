"""Dated on-disk cache for files that change at most daily (scrip master CSVs)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Callable

from config import IST, PATHS


def today_ist() -> date:
    return datetime.now(IST).date()


class DailyFileCache:
    def __init__(self, root: Path = PATHS.cache_dir, namespace: str = "scrip"):
        self.root = root / namespace

    def path(self, name: str, day: date | None = None) -> Path:
        return self.root / (day or today_ist()).isoformat() / name

    def get_or_fetch(self, name: str, fetch: Callable[[], str], day: date | None = None) -> Path:
        p = self.path(name, day)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            text = fetch()
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_text(text)
            tmp.rename(p)
        return p

    def latest(self, name: str) -> Path | None:
        if not self.root.exists():
            return None
        for d in sorted((x for x in self.root.iterdir() if x.is_dir()), reverse=True):
            if (d / name).exists():
                return d / name
        return None
