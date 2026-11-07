"""Operator CLI: `tdesk <command>`. Every trade goes through the gateway."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tradingdesk.analytics import weekly_report
from tradingdesk.domain import Intent, OrderType, Side
from tradingdesk.execution.gateway import KILL_FLAG, Gateway
from tradingdesk.settings import Settings
from tradingdesk.store import Store


def _gw(a: argparse.Namespace) -> Gateway:
    return Gateway.open(Settings.load(a.config), Path(a.data), a.broker)


def cmd_plan(a: argparse.Namespace) -> int:
    gw = _gw(a)
    pid = gw.store.ensure_plan(gw.today, gw.adapter.account().equity)
    if a.action == "add":
        n = len(gw.store.q("SELECT 1 FROM candidate WHERE session_plan_id=?", [pid]))
        if n >= gw.settings.risk.max_candidates:
            print(f"candidate cap {gw.settings.risk.max_candidates} reached", file=sys.stderr)
            return 2
        gw.settings.instrument(a.symbol)
        gw.store.add_candidate(pid, a.symbol, setup_type=a.setup, entry_trigger=a.trigger,
                               stop_level=a.stop, target_level=a.target, thesis=a.thesis,
                               invalidation=a.invalidation)
    elif a.action == "freeze":
        gw.store.freeze_plan(pid)
    p = gw.store.plan_for(gw.today)
    assert p is not None
    print(f"Plan {p['trade_date']}  equity {p['account_equity']}  "
          f"{'FROZEN ' + p['frozen_at'] if p['frozen_at'] else 'DRAFT (freeze before trading)'}")
    for c in gw.store.q("SELECT * FROM candidate WHERE session_plan_id=? ORDER BY rank", [pid]):
        print(f"  {c['rank']}. {c['symbol']} {c['setup_type'] or ''} trigger={c['entry_trigger']}"
              f" stop={c['stop_level']} target={c['target_level']}\n"
              f"     thesis: {c['thesis']}\n     invalid if: {c['invalidation']}")
    return 0


def cmd_submit(a: argparse.Namespace) -> int:
    gw = _gw(a)
    intent = Intent(
        instrument=gw.settings.instrument(a.symbol), side=Side(a.side.upper()),
        entry_price=Decimal(a.entry), stop_price=Decimal(a.stop), target_price=Decimal(a.target),
        order_type=OrderType(a.order_type.upper()), reason=a.reason, pre_trade_note=a.note,
        override_reason=a.override)
    r = gw.submit(intent)
    if r.approved:
        print(f"PLACED {intent.instrument.symbol} {intent.side} size={r.qty} "
              f"risk=GBP {r.risk_gbp:.2f} deal={r.deal_id} intent={r.intent_id}")
        for w in r.warnings:
            print(f"  warning: {w}")
        return 0
    print(f"REJECTED {r.code}: {r.detail} (intent={r.intent_id})", file=sys.stderr)
    return 1


def cmd_status(a: argparse.Namespace) -> int:
    gw = _gw(a)
    st = gw.build_state()
    halted, why = gw.store.is_halted(gw.today)
    print(f"broker={gw.mode} equity={st.equity} day_pnl={st.day_pnl} week_pnl={st.week_pnl}")
    print(f"trades today={st.trades_today}/{gw.settings.risk.max_trades_per_session}  "
          f"plan={sorted(st.plan_symbols) or 'NOT FROZEN'}")
    print(f"halted={halted or gw.kill_flag_set()} {why}")
    for p in gw.adapter.positions():
        print(f"  {p.symbol} {p.side} {p.qty} @ {p.avg_entry} stop={p.stop_level} "
              f"limit={p.limit_level} deal={p.deal_id}")
    for w in gw.adapter.working_orders():
        print(f"  working {w.symbol} {w.side} {w.qty} @ {w.level} {w.order_type} id={w.order_id}")
    return 0


def cmd_reconcile(a: argparse.Namespace) -> int:
    rep = _gw(a).reconcile()
    print("OK" if rep.ok else "DIVERGENCE: system halted for new entries")
    if rep.unknown_at_broker:
        print(f"  positions at broker not known internally: {rep.unknown_at_broker}")
    if rep.missing_stop:
        print(f"  positions WITHOUT a resting stop: {rep.missing_stop}")
    for i in rep.closed_at_broker:
        print(f"  closed at broker, journal it: tdesk close {i} ...")
    return 0 if rep.ok else 3


def cmd_close(a: argparse.Namespace) -> int:
    r = _gw(a).close_trade(a.intent, Decimal(a.entry_fill), Decimal(a.exit), a.rule == "yes",
                           a.deviation or "", a.note)
    print(f"closed {a.intent}: {r:+}R")
    return 0


def cmd_kill(a: argparse.Namespace) -> int:
    # Write the flag before anything else can fail (R-EXE-05).
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    (data / KILL_FLAG).write_text(f"{datetime.now(UTC).isoformat()} {a.reason}\n")
    errors = _gw(a).kill(a.flatten, a.reason)
    print("KILL: working orders cancelled" + (" and positions flattened" if a.flatten else ""))
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    return 0 if not errors else 4


def cmd_clear(a: argparse.Namespace) -> int:
    _gw(a).clear_halt(a.reason)
    print("halt cleared")
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    gw = _gw(a)
    from datetime import timedelta
    ws = gw.today - timedelta(days=gw.today.weekday())
    gw.store.x("INSERT INTO weekly_review(ts_utc, week_start, text) VALUES(?,?,?)",
               [datetime.now(UTC).isoformat(), ws.isoformat(), a.text])
    print(f"review logged for week {ws}")
    return 0


def cmd_event(a: argparse.Namespace) -> int:
    s = Store(Path(a.data) / "desk.sqlite")
    ts = datetime.fromisoformat(a.ts).astimezone(UTC)
    s.x("INSERT INTO economic_event(ts_utc, symbol, name) VALUES(?,?,?)",
        [ts.isoformat(), a.symbol, a.name])
    print(f"event {a.name} at {ts.isoformat()} ({a.symbol or 'all instruments'})")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    print(weekly_report(Store(Path(a.data) / "desk.sqlite"), a.broker))
    return 0


def cmd_price(a: argparse.Namespace) -> int:
    gw = _gw(a)
    if a.broker != "paper":
        print("price is for the paper broker only", file=sys.stderr)
        return 2
    for f in gw.adapter.set_price(a.symbol, Decimal(a.mid)):  # type: ignore[attr-defined]
        print(f"  fill {f.symbol} {f.side} {f.qty} @ {f.price} ({f.client_order_id})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tdesk", description=__doc__)
    p.add_argument("--config", default=os.getenv("TDESK_CONFIG", "config/desk.toml"))
    p.add_argument("--data", default=os.getenv("TDESK_DATA", "data"))
    p.add_argument("--broker", default=os.getenv("TDESK_BROKER", "paper"),
                   choices=["paper", "ig"])
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="show, add to, or freeze today's session plan")
    pl.add_argument("action", choices=["show", "add", "freeze"])
    pl.add_argument("symbol", nargs="?")
    for f in ("setup", "trigger", "stop", "target", "thesis", "invalidation"):
        pl.add_argument(f"--{f}")
    pl.set_defaults(fn=cmd_plan)

    s = sub.add_parser("submit", help="submit a trade intent through the risk engine")
    s.add_argument("symbol")
    s.add_argument("side", choices=["buy", "sell", "BUY", "SELL"])
    s.add_argument("--entry", required=True)
    s.add_argument("--stop", required=True)
    s.add_argument("--target", required=True)
    s.add_argument("--order-type", default="MARKET", choices=["MARKET", "LIMIT", "STOP"])
    s.add_argument("--reason", required=True)
    s.add_argument("--note", required=True, help="pre-trade note, frozen at submit time")
    s.add_argument("--override", help="reason for trading an instrument not in the frozen plan")
    s.set_defaults(fn=cmd_submit)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)

    c = sub.add_parser("close", help="journal a closed trade (mandatory fields)")
    c.add_argument("intent")
    c.add_argument("--entry-fill", required=True)
    c.add_argument("--exit", required=True)
    c.add_argument("--rule", required=True, choices=["yes", "no"])
    c.add_argument("--deviation")
    c.add_argument("--note", required=True)
    c.set_defaults(fn=cmd_close)

    k = sub.add_parser("kill", help="cancel all working orders, optionally flatten, and halt")
    k.add_argument("--flatten", action="store_true")
    k.add_argument("--reason", default="operator kill")
    k.set_defaults(fn=cmd_kill)

    cl = sub.add_parser("clear-halt", help="clear a halt (reason is logged)")
    cl.add_argument("--reason", required=True)
    cl.set_defaults(fn=cmd_clear)

    rv = sub.add_parser("review", help="log the written weekly review that lifts a WEEKLY_HALT")
    rv.add_argument("--text", required=True)
    rv.set_defaults(fn=cmd_review)

    ev = sub.add_parser("event", help="add a scheduled high-impact event (blackout)")
    ev.add_argument("--ts", required=True, help="ISO time with offset, e.g. 2026-11-07T13:30Z")
    ev.add_argument("--name", required=True)
    ev.add_argument("--symbol")
    ev.set_defaults(fn=cmd_event)

    sub.add_parser("report").set_defaults(fn=cmd_report)

    pr = sub.add_parser("price", help="paper only: move the simulated market")
    pr.add_argument("symbol")
    pr.add_argument("mid")
    pr.set_defaults(fn=cmd_price)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    rc: int = a.fn(a)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
