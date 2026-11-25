"""Proof that this system cannot place, modify or cancel a real order.

Layers checked:
  1. Static: no module defines or calls order-placement methods; execution/ and reporting/ import no network library or
     broker client; the Kotak client exposes no order method.
  2. Endpoint allowlist: every Kotak trading / order / position path is refused before any I/O.
  3. Runtime: the complete paper flow runs with all network sends trapped -> zero network calls.
  4. Fail-closed configuration: EXECUTION_MODE is PAPER, and any live-trading flag in the environment stops the app.
  5. Database: paper_orders.mode has a CHECK constraint allowing only 'PAPER'.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest

import config
from config import UnsafeExecutionConfig, assert_paper_mode
from data.kotak_client import ForbiddenEndpoint, KotakMarketDataClient
from execution.paper_broker import PaperBroker
from execution.paper_orders import PaperOrder, idea_key
from tests.conftest import replay_transport

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [p for p in ROOT.rglob("*.py") if ".venv" not in p.parts and "tests" not in p.parts]
ORDER_METHODS = {"place_order", "modify_order", "cancel_order", "buy", "sell", "placeorder", "modifyorder", "cancelorder",
                 "square_off", "exit_order"}
NETWORK_MODULES = {"httpx", "requests", "urllib", "urllib3", "aiohttp", "websockets", "socket", "neo_api_client",
                   "data.kotak_client", "data.sync", "data.historical_data", "data.instrument_master"}


def _tree(p: Path) -> ast.AST:
    return ast.parse(p.read_text(), str(p))


def test_sources_found():
    assert len(SOURCES) > 25


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_order_methods_defined_or_called(path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name.lower() not in ORDER_METHODS, f"{path}: defines {node.name}"
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            assert name.lower() not in ORDER_METHODS, f"{path}:{node.lineno} calls {name}()"


@pytest.mark.parametrize("path", [p for p in SOURCES if p.parent.name in ("execution", "backtesting")] + [ROOT / "app.py"],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_execution_layer_has_no_network_or_broker_imports(path):
    for node in ast.walk(_tree(path)):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        for m in mods:
            assert not any(m == n or m.startswith(n + ".") for n in NETWORK_MODULES), f"{path} imports {m}"


def test_kotak_client_has_no_trading_surface():
    public = {n.lower() for n in dir(KotakMarketDataClient) if not n.startswith("_")}
    assert not (public & ORDER_METHODS)
    assert not any(k in n for n in public for k in ("order", "position", "fund", "margin", "trade", "holding"))


@pytest.mark.parametrize("path", [
    "/quick/order/rule/ms/place", "/quick/order/vr/modify", "/quick/order/cancel", "/quick/order/co/exit",
    "/quick/user/positions", "/quick/user/orders", "/portfolio/v1/holdings", "/quick/user/limits",
    "/script-details/1.0/quotes/neosymbol/../../quick/order/rule/ms/place",
])
def test_order_paths_refused_before_io(kcfg, path):
    sent = []
    c = KotakMarketDataClient(kcfg, transport=replay_transport({}, sent), sleep=lambda s: None)
    with pytest.raises(ForbiddenEndpoint):
        c._get(path)
    assert sent == []


def test_scrip_download_host_pinned(kcfg):
    c = KotakMarketDataClient(kcfg, transport=replay_transport({}), sleep=lambda s: None)
    with pytest.raises(ForbiddenEndpoint):
        c.download_scrip_file("https://e22.kotaksecurities.com/quick/order/rule/ms/place")


def test_paper_flow_makes_zero_network_calls(monkeypatch, repo, candles):
    calls = []

    def trap(self, *a, **k):
        calls.append(a)
        raise AssertionError("network access attempted from the paper engine")

    monkeypatch.setattr(httpx.Client, "send", trap)
    monkeypatch.setattr(httpx.AsyncClient, "send", trap)
    import socket
    monkeypatch.setattr(socket.socket, "connect", trap)
    repo.upsert_candles("RELIANCE", candles["RELIANCE"], "kotak_neo_historical")
    pb = PaperBroker(repo)
    nxt = candles["RELIANCE"].loc["2026-06-10":].iloc[1]
    trig = round(float(nxt.high) - 1, 2)
    pb.place(PaperOrder(idea_key=idea_key("RELIANCE", "LONG", "Breakout", "2026-06-10"), symbol="RELIANCE", side="LONG",
                        instrument="EQ_DELIVERY", qty=10, trigger_price=trig, stop_loss=round(trig * 0.95, 2),
                        target1=round(trig * 1.04, 2), target2=round(trig * 1.08, 2), created_on=date(2026, 6, 10)))
    pb.update()
    from execution.portfolio import summary
    summary(pb)
    assert calls == []
    assert len(pb.fills()) >= 1


def test_execution_mode_constant():
    assert config.EXECUTION_MODE == "PAPER"
    assert_paper_mode()


@pytest.mark.parametrize("flag", ["KOTAK_LIVE_TRADING_ENABLED", "LIVE_TRADING", "EXECUTION_MODE", "SWING_EXECUTION_MODE"])
def test_live_flags_fail_closed(monkeypatch, flag):
    monkeypatch.setenv(flag, "yes" if "ENABLED" in flag or flag == "LIVE_TRADING" else "LIVE")
    with pytest.raises(UnsafeExecutionConfig):
        assert_paper_mode()
    with pytest.raises(UnsafeExecutionConfig):
        PaperBroker.__init__(object.__new__(PaperBroker), None)


def test_mode_tampering_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_MODE", "LIVE")
    with pytest.raises(UnsafeExecutionConfig):
        assert_paper_mode()


def test_database_rejects_non_paper_orders(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.conn.execute(
            "INSERT INTO paper_orders (order_id, idea_key, symbol, side, instrument, order_type, qty, trigger_price, limit_price, "
            "stop_loss, target1, target2, created_on, valid_until, status, mode) VALUES "
            "('x','k','S','LONG','EQ_DELIVERY','STOP_ENTRY',1,1,NULL,0.5,2,3,'2026-01-01','3|15|1','PENDING','LIVE')")


def test_app_offers_no_live_execution():
    src = (ROOT / "app.py").read_text().lower()
    assert "kotak_client" not in src
    for word in ("place real", "live order", "send order", "execute live"):
        assert word not in src
