"""Regenerate the offline test fixtures from REAL data (no synthetic values).

    python tests/fixtures/make_fixtures.py            # candles from the local DB + scrip master subset
    python tests/fixtures/make_fixtures.py --record   # also records raw Kotak API responses (needs credentials)

  * candles/*.csv     : daily OHLCV exported from the SQLite cache (originally fetched from the Kotak Neo historical API)
  * scrip/*.csv       : real rows cut from Kotak's scrip master files for the fixture symbols
  * kotak/*.json      : raw Kotak API responses (historical, index historical, a 422 error, quotes, scrip paths)
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from data.cache import DailyFileCache  # noqa: E402
from data.kotak_client import KotakMarketDataClient, redact  # noqa: E402
from database.repository import Repository  # noqa: E402

HERE = Path(__file__).parent
STOCKS = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "LT", "TATASTEEL", "HINDALCO", "SBIN", "BHARTIARTL"]
INDICES = ["Nifty 50", "Nifty Bank", "NIFTY MIDCAP 100", "INDIA VIX", "Nifty IT", "Nifty Fin Service", "Nifty Metal",
           "NIFTY OIL AND GAS", "Nifty Infra", "Nifty 500"]


def candles() -> None:
    repo = Repository()
    out = HERE / "candles"
    out.mkdir(exist_ok=True)
    for s in STOCKS + INDICES:
        df = repo.candles(s)
        if df.empty:
            print("missing", s)
            continue
        df.to_csv(out / f"{s.replace(' ', '_')}.csv")
        print(s, len(df))


def scrip_subset() -> None:
    cache = DailyFileCache()
    out = HERE / "scrip"
    out.mkdir(exist_ok=True)
    cm = cache.latest("nse_cm-v1.csv")
    fo = cache.latest("nse_fo.csv")
    lines = cm.read_text().splitlines()
    keep = [lines[0]] + [ln for ln in lines[1:] if any(f",{s}-EQ," in ln for s in STOCKS)]
    (out / "nse_cm-v1.csv").write_text("\n".join(keep) + "\n")
    lines = fo.read_text().splitlines()
    keep = [lines[0]] + [ln for ln in lines[1:] if ",FUTSTK," in ln and any(f",{s}," in ln for s in STOCKS)]
    (out / "nse_fo.csv").write_text("\n".join(keep) + "\n")
    print("scrip subset from", cm.parent.name)


def record() -> None:
    c = KotakMarketDataClient()
    out = HERE / "kotak"
    out.mkdir(exist_ok=True)

    def raw(path, params=None, name=""):
        url = f"{c.base_url}{path}"
        r = c._http.get(url, params=params, headers={"Authorization": c.cfg.access_token, "Content-Type": "application/json"})
        c._throttle.wait()
        body = {"status_code": r.status_code, "body": json.loads(redact(r.text))}
        (out / f"{name}.json").write_text(json.dumps(body, indent=1))
        print(name, r.status_code)

    raw("/market-data/1.0/historical/details",
        {"neosymbol": "nse_cm|2885", "fromdate": "2026-06-01", "todate": "2026-11-25", "interval": "D"}, "historical_reliance")
    raw("/market-data/1.0/historical/details",
        {"neosymbol": "nse_cm|Nifty 50", "fromdate": "2026-06-01", "todate": "2026-11-25", "interval": "D"}, "historical_nifty50")
    raw("/market-data/1.0/historical/details",
        {"neosymbol": "nse_cm|2885", "fromdate": "2025-12-01", "todate": "2026-06-01", "interval": "D"}, "historical_range_error")
    raw("/market-data/1.0/historical/details",
        {"neosymbol": "nse_cm|26000", "fromdate": "2026-06-01", "todate": "2026-11-25", "interval": "D"}, "historical_bad_symbol")
    raw("/script-details/1.0/quotes/neosymbol/nse_cm|2885,nse_cm|Nifty 50/all", None, "quotes_all")
    raw("/script-details/1.0/masterscrip/file-paths", None, "scrip_paths")
    (out / "RECORDED.txt").write_text(f"recorded {date.today()} from the live Kotak Neo API; tokens redacted\n")


if __name__ == "__main__":
    candles()
    scrip_subset()
    if "--record" in sys.argv:
        record()
