"""NSE equity identification from the Kotak scrip master + the official NSE index constituent lists.

Universe membership comes from NSE Indices' published constituent CSVs (data/universe/ind_*.csv, downloaded from
niftyindices.com/IndexConstituent/). Each constituent is matched to the Kotak nse_cm scrip master by ISIN (falling
back to symbol) to get the token used by the quotes/historical APIs.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config import BANK_INDEX, IST, PATHS, SECTOR_INDEX, UNIVERSE, UniverseConfig
from data.cache import DailyFileCache

# Kotak docs: "nse_fo and cde_fo: Add 315511200 to the epoch value and convert it to IST" (NSE epochs start 1980).
NSE_EPOCH_OFFSET = 315511200


def nse_epoch_to_date(v: str | float | int | None) -> date | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    return datetime.fromtimestamp(x + NSE_EPOCH_OFFSET, IST).date()


@dataclass
class Equity:
    symbol: str  # NSE symbol, e.g. RELIANCE
    company: str
    industry: str
    isin: str
    indices: list[str] = field(default_factory=list)
    token: str = ""  # Kotak pSymbol for nse_cm
    trading_symbol: str = ""  # RELIANCE-EQ
    tick: float = 0.05
    band_low: float | None = None
    band_high: float | None = None
    last_record_date: date | None = None
    futures: list[dict] = field(default_factory=list)  # [{trading_symbol, token, expiry, lot}]

    @property
    def neosymbol(self) -> str:
        return f"nse_cm|{self.token}"

    @property
    def sector_index(self) -> str:
        if self.industry == "Financial Services" and "BANK" in self.company.upper():
            return BANK_INDEX
        return SECTOR_INDEX.get(self.industry, ("Nifty 500", False))[0]

    @property
    def sector_index_exact(self) -> bool:
        if self.industry == "Financial Services" and "BANK" in self.company.upper():
            return True
        return SECTOR_INDEX.get(self.industry, ("Nifty 500", False))[1]

    def nearest_future(self, on: date, min_days: int = 5) -> dict | None:
        """The nearest stock-futures contract with at least `min_days` to expiry (a short needs room to run)."""
        ok = [f for f in self.futures if f["expiry"] and (f["expiry"] - on).days >= min_days]
        return min(ok, key=lambda f: f["expiry"]) if ok else None


def _clean(r: dict[str, str]) -> dict[str, str]:
    return {k.strip().rstrip(";").strip(): (v or "").strip() for k, v in r.items() if k}


def read_constituents(universe_dir: Path = PATHS.universe_dir, cfg: UniverseConfig = UNIVERSE) -> dict[str, Equity]:
    out: dict[str, Equity] = {}
    for fname in cfg.constituent_files:
        p = universe_dir / fname
        if not p.exists():
            raise FileNotFoundError(f"missing NSE constituent file {p}; run `python run_research.py --refresh-universe`")
        label = {"ind_nifty50list.csv": "NIFTY 50", "ind_niftynext50list.csv": "NIFTY NEXT 50",
                 "ind_nifty100list.csv": "NIFTY 100", "ind_niftymidcap150list.csv": "NIFTY MIDCAP 150"}.get(fname, fname)
        for r in csv.DictReader(io.StringIO(p.read_text())):
            r = _clean(r)
            if r.get("Series", "EQ") != "EQ":
                continue
            sym = r["Symbol"]
            e = out.get(sym) or Equity(symbol=sym, company=r["Company Name"], industry=r["Industry"], isin=r["ISIN Code"])
            if label not in e.indices:
                e.indices.append(label)
            out[sym] = e
    return out


class InstrumentMaster:
    def __init__(self, client=None, cache: DailyFileCache | None = None):
        self.client = client
        self.cache = cache or DailyFileCache()

    def _file(self, prefix: str) -> Path:
        name = {"nse_cm": "nse_cm-v1.csv", "nse_fo": "nse_fo.csv"}[prefix]
        if self.client is None:
            p = self.cache.latest(name)
            if p is None:
                raise FileNotFoundError(f"no cached {name} and no Kotak client to download it")
            return p

        def fetch() -> str:
            urls = [u for u in self.client.scrip_master_paths() if u.rsplit("/", 1)[-1] == name]
            if not urls:
                raise LookupError(f"Kotak scrip master has no {name}")
            return self.client.download_scrip_file(urls[0])

        return self.cache.get_or_fetch(name, fetch)

    def file_date(self, prefix: str = "nse_cm") -> str:
        return self._file(prefix).parent.name

    def equity_rows(self) -> pd.DataFrame:
        df = pd.read_csv(self._file("nse_cm"), dtype=str, keep_default_na=False)
        df.columns = [c.strip().rstrip(";").strip() for c in df.columns]
        return df[(df["pGroup"] == "EQ") & (df["pExchSeg"] == "nse_cm")]

    def futures_rows(self) -> pd.DataFrame:
        df = pd.read_csv(self._file("nse_fo"), dtype=str, keep_default_na=False,
                         usecols=lambda c: c.strip() in {"pSymbol", "pInstType", "pSymbolName", "pTrdSymbol", "lLotSize", "pExpiryDate"})
        df.columns = [c.strip() for c in df.columns]
        return df[df["pInstType"] == "FUTSTK"]

    def build_universe(self, constituents: dict[str, Equity] | None = None) -> dict[str, Equity]:
        cons = constituents or read_constituents()
        eq = self.equity_rows()
        by_isin = {r.pISIN: r for r in eq.itertuples() if r.pISIN}
        by_sym = {r.pSymbolName: r for r in eq.itertuples()}
        fut = self.futures_rows()
        fut_by = {k: g for k, g in fut.groupby("pSymbolName")}
        out: dict[str, Equity] = {}
        for sym, e in cons.items():
            r = by_isin.get(e.isin) or by_sym.get(sym)
            if r is None:
                continue  # not tradable on NSE today (suspended / renamed) -> excluded, reported by caller
            e.token = r.pSymbol
            e.trading_symbol = r.pTrdSymbol
            try:
                e.tick = float(getattr(r, "dTickSize")) / 100.0
                e.band_low = float(r.dLowPriceRange) / 100.0
                e.band_high = float(r.dHighPriceRange) / 100.0
            except (ValueError, AttributeError):
                pass
            e.last_record_date = nse_epoch_to_date(getattr(r, "pRecordDate", None))
            g = fut_by.get(sym)
            if g is not None:
                e.futures = sorted(
                    ({"trading_symbol": f.pTrdSymbol, "token": f.pSymbol, "expiry": nse_epoch_to_date(f.pExpiryDate),
                      "lot": int(float(f.lLotSize or 0))} for f in g.itertuples()),
                    key=lambda x: x["expiry"] or date.max)
            out[sym] = e
        return out


def missing_from_master(constituents: dict[str, Equity], universe: dict[str, Equity]) -> list[str]:
    return sorted(set(constituents) - set(universe))


def constituent_file_age_days(universe_dir: Path = PATHS.universe_dir) -> int:
    p = universe_dir / "ind_nifty100list.csv"
    if not p.exists():
        return 10**6
    return (datetime.now(IST) - datetime.fromtimestamp(p.stat().st_mtime, IST)) // timedelta(days=1)
