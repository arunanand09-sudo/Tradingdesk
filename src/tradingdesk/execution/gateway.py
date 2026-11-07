"""The single path from intent to broker (R-ARCH-01).

This is the ONLY module allowed to import from tradingdesk.adapters. Every order
goes: intent -> risk engine -> journal -> adapter. There is no other route.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradingdesk.adapters import BrokerAdapter, BrokerError, BrokerErrorKind, make_adapter
from tradingdesk.adapters.base import HALTING_KINDS
from tradingdesk.domain import BracketOrder, Intent, Side
from tradingdesk.risk.engine import Approved, Code, OpenRisk, Rejected, RiskState, evaluate
from tradingdesk.settings import Settings
from tradingdesk.store import Store

KILL_FLAG = "KILL"


@dataclass(frozen=True)
class SubmitResult:
    intent_id: str
    approved: bool
    code: str | None = None
    detail: str = ""
    qty: Decimal | None = None
    risk_gbp: Decimal | None = None
    deal_id: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileReport:
    ok: bool
    unknown_at_broker: list[str]
    missing_stop: list[str]
    closed_at_broker: list[str]  # internal open, broker flat: needs `tdesk close`


class Gateway:
    def __init__(self, store: Store, adapter: BrokerAdapter, settings: Settings, data_dir: Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 mode: str = "paper") -> None:
        self.store, self.adapter, self.settings = store, adapter, settings
        self.data_dir, self.clock, self.mode = data_dir, clock, mode

    @classmethod
    def open(cls, settings: Settings, data_dir: Path, broker: str,
             clock: Callable[[], datetime] | None = None) -> Gateway:
        data_dir.mkdir(parents=True, exist_ok=True)
        adapter = make_adapter(broker, data_dir)
        adapter.connect()
        mode = "paper" if broker == "paper" else adapter.creds.environment
        return cls(Store(data_dir / "desk.sqlite"), adapter, settings, data_dir,
                   clock or (lambda: datetime.now(UTC)), mode)

    # ---- state ------------------------------------------------------------------
    @property
    def today(self) -> date:
        return self.clock().astimezone(UTC).date()

    def kill_flag_set(self) -> bool:
        return (self.data_dir / KILL_FLAG).exists()

    def open_intents(self) -> list[dict[str, str]]:
        rows = self.store.q(
            "SELECT i.*, o.deal_id FROM intent i JOIN broker_order o ON o.intent_id=i.id"
            " LEFT JOIN trade t ON t.intent_id=i.id"
            " WHERE o.status='PLACED' AND t.id IS NULL AND i.broker=?", [self.mode])
        return [dict(r) for r in rows]

    def build_state(self) -> RiskState:
        now, today = self.clock(), self.today
        acct = self.adapter.account()
        self.store.snapshot(today, acct.equity, acct.cash, acct.margin_used, self.mode)
        week_start = today - timedelta(days=today.weekday())
        day_eq = self.store.first_equity_since(today, self.mode) or acct.equity
        week_eq = self.store.first_equity_since(week_start, self.mode) or acct.equity
        halted, _ = self.store.is_halted(today)
        reviewed = bool(self.store.q("SELECT 1 FROM weekly_review WHERE week_start=?",
                                     [week_start.isoformat()]))
        trades_today = self.store.q(
            "SELECT COUNT(*) n FROM intent WHERE decision='APPROVED' AND trade_date=?"
            " AND broker=?", [today.isoformat(), self.mode])[0]["n"]
        cum = self.store.q("SELECT pnl_gbp FROM trade WHERE broker='live'")
        # Broker is truth (R-EXE-03): only intents still live at the broker carry risk.
        live_ids = {p.deal_id for p in self.adapter.positions()} | {
            w.order_id for w in self.adapter.working_orders()}
        open_risk = []
        for r in self.open_intents():
            if r["deal_id"] not in live_ids:
                continue
            inst = self.settings.instruments.get(r["symbol"])
            open_risk.append(OpenRisk(
                symbol=r["symbol"],
                correlation_group=inst.correlation_group if inst else None,
                risk_gbp=Decimal(r["risk_gbp"]),
                notional_gbp=Decimal(r["qty"]) * Decimal(r["entry_price"]) *
                (inst.value_per_point if inst else Decimal("1")),
            ))
        s_open, s_close = self.settings.session.bounds_utc(today)
        return RiskState(
            now=now, equity=acct.equity, day_start_equity=day_eq, week_start_equity=week_eq,
            day_pnl=acct.equity - day_eq, week_pnl=acct.equity - week_eq,
            cumulative_live_pnl=sum((Decimal(r["pnl_gbp"]) for r in cum), Decimal("0")),
            trades_today=int(trades_today), session_open=s_open, session_close=s_close,
            plan_symbols=self.store.frozen_symbols(today), open_risk=tuple(open_risk),
            day_trades_5d=self.adapter.day_trade_count_5d(),
            halted=halted or self.kill_flag_set(), weekly_review_logged=reviewed,
            events=(),
        )

    # ---- submit -----------------------------------------------------------------
    def submit(self, intent: Intent) -> SubmitResult:
        state = self.build_state()
        state = RiskState(**{**state.__dict__,
                             "events": self.store.events_for(intent.instrument.symbol)})
        decision = evaluate(intent, state, self.settings.risk)
        self._record_intent(intent, decision)
        self.store.x("INSERT INTO journal(intent_id, ts_utc, kind, note) VALUES(?,?,?,?)",
                     [intent.intent_id, self.clock().isoformat(), "PRE", intent.pre_trade_note])

        if isinstance(decision, Rejected):
            self.store.risk_event(decision.code, "warning",
                                  {"intent": intent.intent_id, "symbol": intent.instrument.symbol,
                                   "detail": decision.detail, **decision.context},
                                  action="REJECTED")
            if decision.code == Code.DAILY_LOSS_LIMIT:
                self._safe(self.adapter.cancel_all)
                self.store.halt("DAILY", "daily loss limit breached", self.today)
            return SubmitResult(intent.intent_id, False, decision.code, decision.detail)

        if decision.off_plan_override:
            self.store.risk_event("OFF_PLAN_OVERRIDE", "info",
                                  {"intent": intent.intent_id, "reason": intent.override_reason})
        client_id = intent.intent_id[:24]
        bracket = BracketOrder(
            client_order_id=client_id, instrument=intent.instrument, side=intent.side,
            qty=decision.qty, order_type=intent.order_type, entry_price=intent.entry_price,
            stop_price=intent.stop_price, target_price=intent.target_price)
        try:
            ids = self._place_idempotent(bracket)
        except BrokerError as e:
            self._order_row(intent.intent_id, client_id, None, "FAILED", str(e))
            sev = "critical" if e.kind in HALTING_KINDS | {BrokerErrorKind.TIMEOUT} else "warning"
            self.store.risk_event(f"BROKER_{e.kind}", sev, {"intent": intent.intent_id,
                                                           "detail": e.detail})
            if sev == "critical":
                self.store.halt("GLOBAL", f"broker error {e.kind}: {e.detail}")
            return SubmitResult(intent.intent_id, False, f"BROKER_{e.kind}", e.detail)

        self._order_row(intent.intent_id, client_id, ids.deal_id, "PLACED", None)
        return SubmitResult(intent.intent_id, True, None, "placed", decision.qty,
                            decision.risk_gbp, ids.deal_id, decision.warnings)

    def _place_idempotent(self, bracket: BracketOrder) -> object:
        """Place; on timeout, ask the broker by client id, then resubmit once (R-EXE-02).

        Resubmission is safe because adapters dedupe on client_order_id.
        """
        try:
            return self.adapter.place_bracket(bracket)
        except BrokerError as e:
            if e.kind is not BrokerErrorKind.TIMEOUT:
                raise
        found = self.adapter.find_by_client_id(bracket.client_order_id)
        if found:
            return found
        return self.adapter.place_bracket(bracket)

    def _record_intent(self, intent: Intent, d: Approved | Rejected) -> None:
        ok = isinstance(d, Approved)
        self.store.x(
            "INSERT INTO intent(id, created_at, trade_date, symbol, side, order_type, entry_price,"
            " stop_price, target_price, qty, risk_gbp, r_value_gbp, reason, decision,"
            " rejected_code, rejected_detail, override_reason, broker)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [intent.intent_id, self.clock().isoformat(), self.today.isoformat(),
             intent.instrument.symbol, intent.side.value, intent.order_type.value,
             str(intent.entry_price), str(intent.stop_price), str(intent.target_price),
             str(d.qty) if ok else None, str(d.risk_gbp) if ok else None,
             str(d.r_value_gbp) if ok else None, intent.reason,
             "APPROVED" if ok else "REJECTED", None if ok else d.code,
             None if ok else d.detail, intent.override_reason, self.mode])

    def _order_row(self, intent_id: str, client_id: str, deal_id: str | None, status: str,
                   detail: str | None) -> None:
        self.store.x("INSERT INTO broker_order(intent_id, client_order_id, deal_id, status,"
                     " submitted_at, detail) VALUES(?,?,?,?,?,?)",
                     [intent_id, client_id, deal_id, status, self.clock().isoformat(), detail])

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except BrokerError as e:
            self.store.risk_event("BROKER_ERROR", "critical", str(e))

    # ---- reconcile (R-EXE-03) ------------------------------------------------------
    def reconcile(self) -> ReconcileReport:
        broker = {p.deal_id: p for p in self.adapter.positions()}
        internal = {r["deal_id"]: r for r in self.open_intents()}
        unknown = [d for d in broker if d not in internal]
        missing_stop = [d for d, p in broker.items() if p.stop_level is None]
        closed = [r["id"] for d, r in internal.items() if d not in broker]
        ok = not unknown and not missing_stop
        if not ok:
            self.store.risk_event("RECONCILE_DIVERGENCE", "critical",
                                  {"unknown_at_broker": unknown, "missing_stop": missing_stop},
                                  action="HALT_NEW_ENTRIES")
            self.store.halt("GLOBAL", "reconciliation divergence")
        return ReconcileReport(ok, unknown, missing_stop, closed)

    # ---- close + journal (R-DATA-07) ----------------------------------------------
    def close_trade(self, intent_id: str, entry_fill: Decimal, exit_price: Decimal,
                    rule_followed: bool, deviation_reason: str, post_note: str) -> Decimal:
        if not rule_followed and not deviation_reason.strip():
            raise ValueError("deviation_reason is required when the rule was not followed")
        if not post_note.strip():
            raise ValueError("a post-trade note is required")
        rows = self.store.q("SELECT * FROM intent WHERE id=? AND decision='APPROVED'",
                            [intent_id])
        if not rows:
            raise KeyError(f"no approved intent {intent_id}")
        r = rows[0]
        inst = self.settings.instrument(r["symbol"])
        sign = Decimal(1) if r["side"] == Side.BUY.value else Decimal(-1)
        pnl = (exit_price - entry_fill) * Decimal(r["qty"]) * inst.value_per_point * sign
        r_mult = (pnl / Decimal(r["risk_gbp"])).quantize(Decimal("0.01"))
        self.store.x("INSERT INTO trade(intent_id, closed_at, entry_fill, exit_price, pnl_gbp,"
                     " r_multiple, rule_followed, deviation_reason, broker)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     [intent_id, self.clock().isoformat(), str(entry_fill), str(exit_price),
                      str(pnl), str(r_mult), int(rule_followed), deviation_reason or None,
                      r["broker"]])
        self.store.x("INSERT INTO journal(intent_id, ts_utc, kind, note) VALUES(?,?,?,?)",
                     [intent_id, self.clock().isoformat(), "POST", post_note])
        return r_mult

    # ---- kill switch (R-EXE-05, R-RISK-11) ------------------------------------------
    def kill(self, flatten: bool, reason: str) -> list[str]:
        """Flag file first (works even if the DB is broken), then the broker, then the log."""
        (self.data_dir / KILL_FLAG).write_text(f"{self.clock().isoformat()} {reason}\n")
        errors = []
        for fn in [self.adapter.cancel_all] + ([self.adapter.flatten_all] if flatten else []):
            try:
                fn()
            except BrokerError as e:
                errors.append(str(e))
        self.store.halt("GLOBAL", f"kill switch: {reason}")
        self.store.risk_event("KILL_SWITCH", "critical", {"flatten": flatten, "errors": errors},
                              action="CANCEL_ALL" + ("+FLATTEN" if flatten else ""))
        return errors

    def clear_halt(self, reason: str) -> None:
        self.store.clear_halt(reason)
        (self.data_dir / KILL_FLAG).unlink(missing_ok=True)
        self.store.risk_event("HALT_CLEARED", "info", reason)
