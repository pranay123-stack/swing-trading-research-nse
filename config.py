"""Central configuration.

Every tunable number lives here so the research, the backtest and the report all read the same values.
Secrets come only from the environment (.env); nothing secret is ever written to disk by this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

IST = ZoneInfo("Asia/Kolkata")

# --------------------------------------------------------------------------------------------------
# SAFETY INVARIANT. This system is research + paper simulation only.
# It is a module-level constant on purpose: there is no environment variable, CLI flag or config file
# that can change it. `assert_paper_mode()` is called at every entry point and fails closed.
# --------------------------------------------------------------------------------------------------
EXECUTION_MODE = "PAPER"

# Environment variables that would only exist if someone tried to enable real trading. If any is set to a
# truthy value, the application refuses to start rather than silently ignoring it.
FORBIDDEN_LIVE_FLAGS = (
    "KOTAK_LIVE_TRADING_ENABLED",
    "SWING_LIVE_TRADING",
    "SWING_EXECUTION_MODE",
    "LIVE_TRADING",
    "EXECUTION_MODE",
)


class UnsafeExecutionConfig(RuntimeError):
    """Raised when anything other than paper execution is configured."""


def assert_paper_mode() -> None:
    if EXECUTION_MODE != "PAPER":
        raise UnsafeExecutionConfig(f"EXECUTION_MODE is {EXECUTION_MODE!r}; only 'PAPER' is supported")
    for name in FORBIDDEN_LIVE_FLAGS:
        val = os.environ.get(name, "").strip().lower()
        if val and val not in ("0", "false", "no", "off", "paper"):
            raise UnsafeExecutionConfig(
                f"environment variable {name}={val!r} requests non-paper execution; this system is paper-only"
            )


# --------------------------------------------------------------------------------------------------
# Kotak Neo market-data access (read-only)
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class KotakConfig:
    # The "token provided in your NEO API dashboard". Market-data endpoints (scrip master, quotes,
    # historical) authenticate with this token alone (verified live 2026-11-25).
    access_token: str = field(default_factory=lambda: os.environ.get("KOTAK_ACCESS_TOKEN", "").strip())
    # Base URL normally comes from /tradeApiValidate. Market-data calls only need a working base host,
    # so it can be pinned here; if empty, the client logs in (mobile + UCC + TOTP + MPIN) to discover it.
    base_url: str = field(default_factory=lambda: os.environ.get("KOTAK_BASE_URL", "").strip().rstrip("/"))
    mobile: str = field(default_factory=lambda: os.environ.get("KOTAK_MOBILE", "").strip())
    ucc: str = field(default_factory=lambda: os.environ.get("KOTAK_UCC", "").strip())
    mpin: str = field(default_factory=lambda: os.environ.get("KOTAK_MPIN", "").strip())
    totp_secret: str = field(default_factory=lambda: os.environ.get("KOTAK_TOTP_SECRET", "").strip())
    login_host: str = "https://mis.kotaksecurities.com"
    # Measured 2026-11-25: back-to-back historical calls get HTTP 429 after the first; ~1.6 req/s sustained
    # still sees occasional 429. 1.0 req/s plus backoff is reliable.
    requests_per_second: float = 1.0
    max_retries: int = 6
    timeout_s: float = 30.0
    # Historical API limits (docs + live 422 replies): daily range <= 180 days inclusive, from-date <= 5 years old.
    max_daily_window_days: int = 179
    max_lookback_years: int = 5

    @property
    def configured(self) -> bool:
        return bool(self.access_token)


# --------------------------------------------------------------------------------------------------
# Universe and liquidity
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UniverseConfig:
    # Official NSE Indices constituent files (niftyindices.com/IndexConstituent/...).
    constituent_files: tuple[str, ...] = (
        "ind_nifty50list.csv",
        "ind_niftynext50list.csv",
        "ind_nifty100list.csv",
        "ind_niftymidcap150list.csv",  # the "selected liquid NIFTY 500" extension; filtered by liquidity below
    )
    min_price: float = 50.0
    min_median_traded_value_cr: float = 40.0  # median 20-day daily traded value, Rs crore
    min_median_volume: int = 100_000
    min_history_bars: int = 220  # needs SMA200 plus a little warm-up
    max_zero_volume_days_60: int = 0  # any zero-volume day in the last 60 sessions -> treated as suspended/illiquid


# Industry (as published in the NSE constituent CSV) -> sector index available through Kotak quotes/historical.
# Where NSE has no exact sector index in the Kotak list, the nearest broad sector index is used and flagged.
SECTOR_INDEX = {
    "Financial Services": ("Nifty Fin Service", True),
    "Information Technology": ("Nifty IT", True),
    "Automobile and Auto Components": ("Nifty Auto", True),
    "Fast Moving Consumer Goods": ("Nifty FMCG", True),
    "Healthcare": ("NIFTY HEALTHCARE", True),
    "Metals & Mining": ("Nifty Metal", True),
    "Oil Gas & Consumable Fuels": ("NIFTY OIL AND GAS", True),
    "Power": ("Nifty Energy", True),
    "Realty": ("Nifty Realty", True),
    "Consumer Durables": ("NIFTY CONSR DURBL", True),
    "Media Entertainment & Publication": ("Nifty Media", True),
    "Capital Goods": ("Nifty Infra", False),
    "Construction": ("Nifty Infra", False),
    "Construction Materials": ("Nifty Infra", False),
    "Telecommunication": ("Nifty Infra", False),
    "Consumer Services": ("Nifty Consumption", False),
    "Textiles": ("Nifty Consumption", False),
    "Services": ("Nifty Serv Sector", False),
    "Chemicals": ("Nifty Commodities", False),
    "Diversified": ("Nifty 500", False),
}
# Banks get the bank index rather than the broad financial-services index.
BANK_INDEX = "Nifty Bank"

MARKET_INDICES = ("Nifty 50", "Nifty Bank", "NIFTY MIDCAP 100", "Nifty 500", "INDIA VIX")
BREADTH_INDEX_SET = tuple(sorted({v[0] for v in SECTOR_INDEX.values()} | {BANK_INDEX, "Nifty PSU Bank", "Nifty Pharma", "Nifty Pvt Bank"}))


# --------------------------------------------------------------------------------------------------
# Strategy, risk and costs
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyConfig:
    min_score: float = 60.0  # a candidate below this is not a trade idea, whatever the ranking
    # T1 is the partial-profit level at the first MAJOR level in the trade's direction: it must give at least 1R, otherwise
    # supply/demand sits right on top of the entry. The headline requirement is T2 >= 2R, at a structural level.
    min_rr_t1: float = 1.0
    min_rr_t2: float = 2.0
    stop_atr_buffer: float = 0.25  # stop sits this many ATRs beyond the structural invalidation level
    min_stop_atr: float = 1.0  # a stop closer than 1 ATR is inside daily noise -> widened to 1 ATR
    max_stop_atr: float = 3.5  # a stop further than this is not a swing trade; reject
    max_extension_atr: float = 3.0  # entry more than 3 ATR above EMA20 (long) is chasing; reject
    breakout_lookback: int = 20
    volume_confirm_rvol: float = 1.5
    max_holding_days: int = 15  # trading sessions, ~3 weeks
    min_holding_days: int = 5
    entry_valid_days: int = 3  # a limit/stop entry not triggered within 3 sessions expires
    max_pair_correlation: float = 0.75  # 60-day daily return correlation between selected ideas
    max_ideas_per_sector: int = 1


@dataclass(frozen=True)
class RiskConfig:
    paper_capital: float = 1_000_000.0  # Rs 10 lakh virtual capital
    risk_per_trade_pct: float = 1.0  # % of capital lost if the stop is hit (before gap risk)
    max_position_pct: float = 25.0  # cap on notional allocation per cash (delivery) idea
    # Stock futures: one lot is often Rs 5-10 lakh notional, so the cash cap would make every short impossible. Futures
    # notional is capped at 1x capital (no leverage beyond fully-funded exposure); only margin is actually blocked.
    max_futures_notional_pct: float = 100.0
    max_open_positions: int = 5


@dataclass(frozen=True)
class CostConfig:
    """Indian cash-equity (delivery) and stock-futures costs.

    Sources (checked 2026-11-25):
      * Brokerage: Kotak Neo "Trade Free" plan page (kotakneo.com/pricing/trade-free-plan): delivery 0.20% per executed
        order, F&O Rs 10 per executed order. Plans differ; change these if yours differs.
      * STT: delivery 0.1% on buy and sell; futures 0.05% on sell (raised from 0.02% by Budget 2026, effective 2026-04-01,
        NSE notice 2026-03-31, per Business Today / ICICI Direct coverage).
      * NSE transaction charges: 0.00297% cash, 0.00173% futures (NSE revision effective 2024-10-01).
      * SEBI turnover fee Rs 10 / crore. Stamp duty: 0.015% delivery buy, 0.002% futures buy. GST 18% on brokerage +
        exchange + SEBI fees. DP charge per scrip sold from demat (approximate).
    """

    delivery_brokerage_pct: float = 0.20
    delivery_brokerage_cap: float | None = None
    futures_brokerage_flat: float = 10.0
    stt_delivery_pct: float = 0.10
    stt_futures_sell_pct: float = 0.05
    exch_txn_cash_pct: float = 0.00297
    exch_txn_fut_pct: float = 0.00173
    sebi_fee_per_crore: float = 10.0
    stamp_delivery_buy_pct: float = 0.015
    stamp_futures_buy_pct: float = 0.002
    gst_pct: float = 18.0
    dp_charge_per_sell: float = 20.0
    slippage_bps: float = 5.0  # per fill, against us, on top of gap-aware fill prices


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    db: Path = ROOT / "database" / "swing_research.sqlite"
    universe_dir: Path = ROOT / "data" / "universe"
    cache_dir: Path = ROOT / "cache"
    reports: Path = ROOT / "reports"
    charts: Path = ROOT / "charts"
    logs: Path = ROOT / "logs"
    events_manual: Path = ROOT / "data" / "events_verified.json"


KOTAK = KotakConfig()
UNIVERSE = UniverseConfig()
STRATEGY = StrategyConfig()
RISK = RiskConfig()
COSTS = CostConfig()
PATHS = Paths()

# NSE closes at 15:30 IST. Kotak serves the current session's bar while it is still forming (timestamped 09:15
# instead of 00:00); a bar for today is only treated as final after this time.
SESSION_FINAL_AFTER = (15, 45)
