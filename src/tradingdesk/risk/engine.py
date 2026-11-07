"""The risk engine (PRD Section 10).

A pure function: no I/O, no network, no clock reads. Everything it needs is
passed in. It returns Approved(qty) or Rejected(code). It is the only path to a
broker (enforced in execution.gateway and by tests/test_architecture.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from tradingdesk.domain import Intent
from tradingdesk.risk.config import RiskConfig, loosened_fields

HUNDRED = Decimal("100")


class Code:
    HALTED = "HALTED"
    KILL_K1 = "KILL_K1"
    WEEKLY_HALT = "WEEKLY_HALT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"
    TRADE_COUNT_CAP = "TRADE_COUNT_CAP"
    OFF_PLAN = "OFF_PLAN"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    TIME_WINDOW = "TIME_WINDOW"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    PDT_GUARD = "PDT_GUARD"
    SIZE_BELOW_MIN = "SIZE_BELOW_MIN"
    OPEN_RISK_CAP = "OPEN_RISK_CAP"
    CORRELATED_RISK_CAP = "CORRELATED_RISK_CAP"
    LEVERAGE_CAP = "LEVERAGE_CAP"
    LIMIT_CHANGE_LOCKED = "LIMIT_CHANGE_LOCKED"


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    correlation_group: str | None
    risk_gbp: Decimal
    notional_gbp: Decimal

    @property
    def group(self) -> str:
        return self.correlation_group or self.symbol


@dataclass(frozen=True)
class RiskState:
    """Everything the engine is allowed to know. Built by the gateway."""

    now: datetime  # UTC
    equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    day_pnl: Decimal  # realised + unrealised since session start
    week_pnl: Decimal
    cumulative_live_pnl: Decimal
    trades_today: int
    session_open: datetime  # UTC
    session_close: datetime  # UTC
    plan_symbols: frozenset[str]
    open_risk: tuple[OpenRisk, ...] = ()
    day_trades_5d: int | None = None
    halted: bool = False
    weekly_review_logged: bool = False
    events: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class Approved:
    qty: Decimal
    risk_gbp: Decimal
    r_value_gbp: Decimal
    warnings: tuple[str, ...] = ()
    off_plan_override: bool = False


@dataclass(frozen=True)
class Rejected:
    code: str
    detail: str
    context: dict[str, str] = field(default_factory=dict)


Decision = Approved | Rejected


def r_value(cfg: RiskConfig, equity: Decimal) -> Decimal:
    """1R in GBP."""
    return equity * cfg.per_trade_risk_pct / HUNDRED


def floor_to_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def size_for_risk(budget_gbp: Decimal, stop_distance: Decimal, value_per_point: Decimal,
                  step: Decimal) -> Decimal:
    """Largest size, rounded down to `step`, whose stop-out loss is <= budget (R-RISK-01)."""
    per_unit = stop_distance * value_per_point
    if budget_gbp <= 0:
        return Decimal("0")
    return floor_to_step(budget_gbp / per_unit, step)


def daily_loss_limit(cfg: RiskConfig, day_start_equity: Decimal) -> Decimal:
    by_pct = day_start_equity * cfg.daily_loss_pct / HUNDRED
    by_r = r_value(cfg, day_start_equity) * cfg.daily_loss_r
    return min(by_pct, by_r)


def weekly_loss_limit(cfg: RiskConfig, week_start_equity: Decimal) -> Decimal:
    by_pct = week_start_equity * cfg.weekly_loss_pct / HUNDRED
    by_r = r_value(cfg, week_start_equity) * cfg.weekly_loss_r
    return min(by_pct, by_r)


def evaluate(intent: Intent, state: RiskState, cfg: RiskConfig) -> Decision:
    inst = intent.instrument
    warnings: list[str] = []
    one_r = r_value(cfg, state.equity)

    # R-RISK-11: a persisted halt blocks everything.
    if state.halted:
        return Rejected(Code.HALTED, "system is halted; clear with an explicit logged command")

    # K1: absolute cumulative live loss.
    if state.cumulative_live_pnl <= -cfg.kill_k1_absolute_loss_gbp:
        return Rejected(Code.KILL_K1, "cumulative live loss reached kill criterion K1",
                        {"cumulative": str(state.cumulative_live_pnl)})

    # R-RISK-03
    wk_limit = weekly_loss_limit(cfg, state.week_start_equity)
    if state.week_pnl <= -wk_limit and not state.weekly_review_logged:
        return Rejected(Code.WEEKLY_HALT, "weekly loss limit breached; log a written review",
                        {"week_pnl": str(state.week_pnl), "limit": str(wk_limit)})

    # R-RISK-02
    day_limit = daily_loss_limit(cfg, state.day_start_equity)
    if state.day_pnl <= -day_limit:
        return Rejected(Code.DAILY_LOSS_LIMIT, "daily loss limit breached",
                        {"day_pnl": str(state.day_pnl), "limit": str(day_limit)})

    # R-RISK-06
    if state.trades_today >= cfg.max_trades_per_session:
        return Rejected(Code.TRADE_COUNT_CAP,
                        f"{state.trades_today} trades already this session")

    # R-SCAN-02
    off_plan = inst.symbol not in state.plan_symbols
    if off_plan:
        if not intent.override_reason:
            return Rejected(Code.OFF_PLAN, f"{inst.symbol} is not in the frozen session plan")
        warnings.append("OFF_PLAN override: " + intent.override_reason)

    # R-RISK-07
    if not (state.session_open <= state.now < state.session_close):
        return Rejected(Code.OUTSIDE_SESSION, "outside the configured trading session")
    if not cfg.open_close_windows_tradeable:
        early = state.now < state.session_open + timedelta(minutes=cfg.no_entry_after_open_min)
        late = state.now >= state.session_close - timedelta(minutes=cfg.no_entry_before_close_min)
        if early or late:
            return Rejected(Code.TIME_WINDOW, "inside the opening or closing no-entry window")

    # R-RISK-10
    if not cfg.is_event_strategy:
        window = timedelta(minutes=cfg.event_blackout_min)
        for ev in state.events:
            if abs(state.now - ev) <= window:
                return Rejected(Code.EVENT_BLACKOUT,
                                f"within blackout of event at {ev.isoformat()}")

    # R-RISK-09
    if inst.finra_regulated and state.day_trades_5d is not None:
        equity_usd = state.equity * cfg.gbp_usd
        if equity_usd < cfg.pdt_equity_threshold_usd:
            if state.day_trades_5d >= cfg.pdt_max_day_trades_5d:
                return Rejected(Code.PDT_GUARD,
                                f"{state.day_trades_5d} day trades in 5 business days")
            if state.day_trades_5d == cfg.pdt_max_day_trades_5d - 1:
                warnings.append("PDT: this is the last permitted day trade in the window")

    # R-RISK-01: size from the stop distance.
    qty = size_for_risk(one_r, intent.stop_distance, inst.value_per_point, inst.size_step)

    # R-RISK-02: never risk more than what is left of today's loss budget.
    day_remaining = day_limit + min(state.day_pnl, Decimal("0"))
    if one_r > day_remaining:
        qty = min(qty, size_for_risk(day_remaining, intent.stop_distance,
                                     inst.value_per_point, inst.size_step))
        if qty < inst.min_size:
            return Rejected(Code.DAILY_BUDGET_EXCEEDED,
                            "remaining daily loss budget is smaller than the minimum size risk")

    # R-RISK-05: correlated positions share one unit of risk.
    group = inst.correlation_group or inst.symbol
    group_used = sum((o.risk_gbp for o in state.open_risk if o.group == group), Decimal("0"))
    if group_used > 0:
        qty = min(qty, size_for_risk(one_r - group_used, intent.stop_distance,
                                     inst.value_per_point, inst.size_step))
        if qty < inst.min_size:
            return Rejected(Code.CORRELATED_RISK_CAP,
                            f"group '{group}' already carries {group_used} GBP of risk")

    if qty < inst.min_size:
        return Rejected(Code.SIZE_BELOW_MIN,
                        f"risk-derived size {qty} below instrument minimum {inst.min_size}")

    risk_gbp = qty * intent.stop_distance * inst.value_per_point

    # R-RISK-04
    open_total = sum((o.risk_gbp for o in state.open_risk), Decimal("0"))
    cap = state.equity * cfg.max_open_risk_pct / HUNDRED
    if open_total + risk_gbp > cap:
        return Rejected(Code.OPEN_RISK_CAP, "aggregate open risk would exceed cap",
                        {"open": str(open_total), "new": str(risk_gbp), "cap": str(cap)})

    # R-RISK-08
    notional = qty * intent.entry_price * inst.value_per_point
    open_notional = sum((o.notional_gbp for o in state.open_risk), Decimal("0"))
    if open_notional + notional > state.equity * cfg.max_leverage:
        return Rejected(Code.LEVERAGE_CAP, "notional exposure would exceed internal leverage cap",
                        {"notional": str(open_notional + notional)})

    return Approved(qty=qty, risk_gbp=risk_gbp, r_value_gbp=one_r,
                    warnings=tuple(warnings), off_plan_override=off_plan)


def check_limit_change(old: RiskConfig, new: RiskConfig, positions_open: bool) -> Rejected | None:
    """R-RISK-13: no loosening of a limit while positions are open."""
    loosened = loosened_fields(old, new)
    if loosened and positions_open:
        return Rejected(Code.LIMIT_CHANGE_LOCKED, "cannot loosen limits with open positions",
                        {"fields": ",".join(loosened)})
    return None


def count_day_trades_5d(day_trade_dates: list[date], today: date,
                        holidays: frozenset[date] = frozenset()) -> int:
    """Day trades in the rolling 5 business days ending today (R-RISK-09)."""
    window: list[date] = []
    d = today
    while len(window) < 5:
        if d.weekday() < 5 and d not in holidays:
            window.append(d)
        d -= timedelta(days=1)
    start = window[-1]
    return sum(1 for t in day_trade_dates if start <= t <= today)
