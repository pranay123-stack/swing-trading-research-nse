"""Download / refresh everything the research needs: universe instruments, market + sector indices, stock history."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from config import BREADTH_INDEX_SET, MARKET_INDICES, PATHS, assert_paper_mode
from data.historical_data import HistoricalData
from data.instrument_master import InstrumentMaster, read_constituents
from data.kotak_client import KotakMarketDataClient
from database.repository import Repository

log = logging.getLogger("sync")

DEFAULT_HISTORY_START = date(2023, 1, 2)


def index_symbols() -> list[str]:
    return sorted(set(MARKET_INDICES) | set(BREADTH_INDEX_SET))


def sync_all(client: KotakMarketDataClient, repo: Repository, start: date = DEFAULT_HISTORY_START,
             only: list[str] | None = None, verbose: bool = True) -> dict:
    master = InstrumentMaster(client)
    universe = master.build_universe(read_constituents())
    hd = HistoricalData(client, repo)

    def progress(i, n, sym, k):
        if verbose:
            print(f"  [{i}/{n}] {sym}: {k} bars", flush=True)

    idx = [(s, f"nse_cm|{s}") for s in index_symbols()]
    stocks = [(e.symbol, e.neosymbol) for e in universe.values() if not only or e.symbol in only]
    if verbose:
        print(f"indices: {len(idx)}  stocks: {len(stocks)}  from {start}", flush=True)
    res_idx = hd.sync_many(idx, start, progress=progress)
    res_stk = hd.sync_many(stocks, start, progress=progress)
    return {"indices": res_idx, "stocks": res_stk, "errors": hd.errors, "universe": universe}


def main(argv: list[str] | None = None) -> int:
    assert_paper_mode()
    ap = argparse.ArgumentParser(description="Sync Kotak Neo daily history into SQLite")
    ap.add_argument("--start", default=DEFAULT_HISTORY_START.isoformat())
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    PATHS.logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=PATHS.logs / "sync.log",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    client = KotakMarketDataClient()
    repo = Repository()
    out = sync_all(client, repo, date.fromisoformat(a.start), a.only)
    print(f"done. errors: {len(out['errors'])}")
    for k, v in out["errors"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
