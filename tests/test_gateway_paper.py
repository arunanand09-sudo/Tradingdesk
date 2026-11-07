"""End-to-end through the gateway on the paper adapter, with zero live credentials."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from tradingdesk.adapters.base import BrokerErrorKind
from tradingdesk.adapters.paper import PaperAdapter
from tradingdesk.domain import Intent, OrderType, Side
from tradingdesk.execution.gateway import Gateway
from tradingdesk.settings import Settings
from tradingdesk.store import Store

ROOT = Path(__file__).resolve().parents[1]
FTSE = "IX.D.FTSE.DAILY.IP"
SPX = "IX.D.SPTRD.DAILY.IP"
NDX = "IX.D.NASDAQ.CASH.IP"
T = datetime(2026, 11, 9, 11, 0, tzinfo=UTC)


@pytest.fixture
def desk(tmp_path: Path) -> Gateway:
    settings = Settings.load(ROOT / "config/desk.toml")
    adapter = PaperAdapter(starting_equity=D("10000"), state_path=tmp_path / "paper.json")
    for sym, px in ((FTSE, "8000"), (SPX, "6000"), (NDX, "21000")):
        adapter.set_price(sym, D(px))
    gw = Gateway(Store(tmp_path / "desk.sqlite"), adapter, settings, tmp_path, clock=lambda: T)
    pid = gw.store.ensure_plan(gw.today, D("10000"))
    for s in (FTSE, SPX, NDX):
        gw.store.add_candidate(pid, s, thesis="t", invalidation="i")
    gw.store.freeze_plan(pid)
    return gw


def mk(gw: Gateway, sym: str = FTSE, entry: str = "8000", stop: str = "7980",
       target: str = "8040", **kw: object) -> Intent:
    return Intent(instrument=gw.settings.instrument(sym), side=Side.BUY, entry_price=D(entry),
                  stop_price=D(stop), target_price=D(target), reason="r",
                  pre_trade_note="pre", **kw)  # type: ignore[arg-type]


def test_end_to_end_trade_journalled(desk: Gateway) -> None:
    r = desk.submit(mk(desk))
    assert r.approved and r.qty == D("2.50")
    pos = desk.adapter.positions()
    assert len(pos) == 1 and pos[0].stop_level == D("7980")  # stop resting at broker
    assert desk.reconcile().ok
    fills = desk.adapter.set_price(FTSE, D("8045"))  # type: ignore[attr-defined]
    assert fills and fills[0].price == D("8040")  # target hit
    rep = desk.reconcile()
    assert rep.closed_at_broker == [r.intent_id]
    rm = desk.close_trade(r.intent_id, D("8000.5"), D("8040"), True, "", "went to plan")
    assert rm == D("1.98")
    j = desk.store.q("SELECT kind FROM journal WHERE intent_id=? ORDER BY id", [r.intent_id])
    assert [x["kind"] for x in j] == ["PRE", "POST"]


def test_off_plan_requires_override(desk: Gateway) -> None:
    gw2 = Gateway(Store(desk.data_dir / "other.sqlite"), desk.adapter, desk.settings,
                  desk.data_dir, clock=lambda: T)
    r = gw2.submit(mk(gw2))
    assert not r.approved and r.code == "OFF_PLAN"
    r2 = gw2.submit(mk(gw2, override_reason="breaking news setup"))
    assert r2.approved
    ev = gw2.store.q("SELECT kind FROM risk_event")
    assert {e["kind"] for e in ev} >= {"OFF_PLAN", "OFF_PLAN_OVERRIDE"}


def test_frozen_plan_is_immutable(desk: Gateway) -> None:
    pid = desk.store.plan_for(desk.today)["id"]  # type: ignore[index]
    with pytest.raises(sqlite3.IntegrityError):
        desk.store.add_candidate(pid, "IX.D.DAX.DAILY.IP")


def test_pre_trade_notes_are_append_only(desk: Gateway) -> None:
    r = desk.submit(mk(desk))
    with pytest.raises(sqlite3.IntegrityError):
        desk.store.x("UPDATE journal SET note='edited' WHERE intent_id=?", [r.intent_id])
    desk.store.risk_event("TEST", "info", "x")
    with pytest.raises(sqlite3.IntegrityError):
        desk.store.x("DELETE FROM risk_event")


def test_sixth_trade_rejected(desk: Gateway) -> None:
    for _ in range(5):
        assert desk.submit(mk(desk)).approved
        desk.adapter.flatten_all()
        for row in desk.open_intents():
            desk.close_trade(row["id"], D(row["entry_price"]), D(row["entry_price"]), True, "",
                             "flat")
    assert desk.submit(mk(desk)).code == "TRADE_COUNT_CAP"


def test_daily_loss_limit_halts_and_cancels(desk: Gateway) -> None:
    lim = mk(desk, entry="7990", stop="7970", target="8030").model_copy(
        update={"order_type": OrderType.LIMIT})
    assert desk.submit(lim).approved
    assert desk.adapter.working_orders()
    desk.adapter.s["cash"] = "9800"  # type: ignore[attr-defined]  # simulate a -200 day
    r = desk.submit(mk(desk))
    assert r.code == "DAILY_LOSS_LIMIT"
    assert desk.adapter.working_orders() == []  # cancelled on breach
    desk.adapter.s["cash"] = "10000"  # type: ignore[attr-defined]  # even if equity recovers
    assert desk.submit(mk(desk)).code == "HALTED"


def test_idempotent_after_timeout(desk: Gateway) -> None:
    desk.adapter.fail_next_with_timeout = True  # type: ignore[attr-defined]
    r = desk.submit(mk(desk))
    assert r.approved
    assert len(desk.adapter.positions()) == 1  # one position, not two


def test_unknown_broker_error_halts(desk: Gateway) -> None:
    from tradingdesk.adapters.base import BrokerError

    def boom(_: object) -> None:
        raise BrokerError(BrokerErrorKind.UNKNOWN, "weird")
    desk.adapter.place_bracket = boom  # type: ignore[method-assign,assignment]
    r = desk.submit(mk(desk))
    assert not r.approved and r.code == "BROKER_UNKNOWN"
    assert desk.store.is_halted(desk.today)[0]


def test_broker_reject_does_not_halt(desk: Gateway) -> None:
    desk.adapter.reject_next = "MARKET_CLOSED"  # type: ignore[attr-defined]
    r = desk.submit(mk(desk))
    assert r.code == "BROKER_REJECTED_BY_BROKER"
    assert not desk.store.is_halted(desk.today)[0]


def test_reconcile_divergence_halts(desk: Gateway) -> None:
    from tradingdesk.domain import BracketOrder
    rogue = BracketOrder(client_order_id="rogue", instrument=desk.settings.instrument(FTSE),
                         side=Side.BUY, qty=D("1"), order_type=OrderType.MARKET,
                         entry_price=D("8000"), stop_price=D("7900"), target_price=D("8100"))
    desk.adapter.place_bracket(rogue)  # placed outside the system
    rep = desk.reconcile()
    assert not rep.ok and rep.unknown_at_broker
    assert desk.submit(mk(desk)).code == "HALTED"


def test_kill_switch_persists_across_restart(desk: Gateway, tmp_path: Path) -> None:
    assert desk.submit(mk(desk)).approved
    desk.kill(flatten=True, reason="test")
    assert desk.adapter.positions() == []
    # "restart": new gateway, new adapter, same data dir
    adapter2 = PaperAdapter(state_path=tmp_path / "paper.json")
    gw2 = Gateway(Store(tmp_path / "desk.sqlite"), adapter2, desk.settings, tmp_path,
                  clock=lambda: T)
    assert gw2.submit(mk(gw2)).code == "HALTED"
    with pytest.raises(ValueError):
        gw2.clear_halt("  ")
    gw2.clear_halt("reviewed, resuming")
    assert gw2.submit(mk(gw2)).approved


def test_stop_survives_process_death(desk: Gateway, tmp_path: Path) -> None:
    """R-EXE-04: kill the 'process' with an open position; the stop is still at the broker."""
    assert desk.submit(mk(desk)).approved
    del desk
    broker = PaperAdapter(state_path=tmp_path / "paper.json")
    [p] = broker.positions()
    assert p.stop_level == D("7980")
    fills = broker.set_price(FTSE, D("7950"))  # gaps through the stop
    assert fills[0].price == D("7949.5")  # filled at the gapped bid, not the stop
    assert broker.positions() == []


def test_close_requires_deviation_reason(desk: Gateway) -> None:
    r = desk.submit(mk(desk))
    with pytest.raises(ValueError):
        desk.close_trade(r.intent_id, D("8000"), D("7980"), False, "", "note")
    with pytest.raises(ValueError):
        desk.close_trade(r.intent_id, D("8000"), D("7980"), True, "", " ")
    with pytest.raises(KeyError):
        desk.close_trade("nope", D("8000"), D("7980"), True, "", "n")


def test_correlated_cap_end_to_end(desk: Gateway) -> None:
    assert desk.submit(mk(desk, SPX, "6000", "5980", "6040")).approved
    r = desk.submit(mk(desk, NDX, "21000", "20950", "21100"))
    assert r.code == "CORRELATED_RISK_CAP"
