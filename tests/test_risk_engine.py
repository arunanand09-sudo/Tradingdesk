"""Risk engine: one test per R-RISK requirement, plus property tests on sizing."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.conftest import CLOSE, FTSE, NDX, NOW, OPEN, cfg, intent, state
from tradingdesk.domain import Instrument, Side
from tradingdesk.risk.config import RiskConfig, loosened_fields
from tradingdesk.risk.engine import (
    Approved,
    Code,
    OpenRisk,
    Rejected,
    check_limit_change,
    count_day_trades_5d,
    daily_loss_limit,
    evaluate,
    size_for_risk,
    weekly_loss_limit,
)


def rej(d: object) -> str:
    assert isinstance(d, Rejected), d
    return d.code


def test_happy_path_sizes_from_stop() -> None:
    d = evaluate(intent(), state(), cfg())
    assert isinstance(d, Approved)
    # 0.5% of 10k = 50 GBP; 20 point stop at 1 GBP/pt => 2.5 per point
    assert d.qty == D("2.50") and d.risk_gbp == D("50") and d.r_value_gbp == D("50")
    assert not d.off_plan_override and d.warnings == ()


def test_sell_side() -> None:
    d = evaluate(intent(side=Side.SELL, entry="8000", stop="8020", target="7960"), state(), cfg())
    assert isinstance(d, Approved) and d.qty == D("2.50")


# R-RISK-01 property test
@given(
    equity=st.decimals(min_value=1000, max_value=10_000_000, places=2),
    stop=st.decimals(min_value=D("0.5"), max_value=500, places=1),
    vpp=st.decimals(min_value=D("0.1"), max_value=50, places=1),
    pct=st.decimals(min_value=D("0.01"), max_value=1, places=2),
)
def test_sizing_never_exceeds_budget(equity: D, stop: D, vpp: D, pct: D) -> None:
    budget = equity * pct / 100
    step = D("0.01")
    qty = size_for_risk(budget, stop, vpp, step)
    assert qty * stop * vpp <= budget
    assert qty % step == 0
    assert (qty + step) * stop * vpp > budget  # rounded down, not more than needed


def test_size_for_zero_budget() -> None:
    assert size_for_risk(D("0"), D("10"), D("1"), D("0.01")) == 0
    assert size_for_risk(D("-5"), D("10"), D("1"), D("0.01")) == 0


def test_per_trade_hard_max() -> None:
    with pytest.raises(ValidationError):
        cfg(per_trade_risk_pct=D("1.5"))
    with pytest.raises(ValidationError):
        cfg(per_trade_risk_pct=D("0"))


def test_disconnect_policy_has_no_default() -> None:
    with pytest.raises(ValidationError):
        RiskConfig.model_validate({"kill_k1_absolute_loss_gbp": D("1000")})


def test_intent_requires_stop_on_correct_side() -> None:
    with pytest.raises(ValidationError):
        intent(stop="8000")
    with pytest.raises(ValidationError):
        intent(stop="8010")  # stop above entry for a BUY
    with pytest.raises(ValidationError):
        intent(side=Side.SELL, entry="8000", stop="7990", target="7960")


def test_size_below_min() -> None:
    d = evaluate(intent(stop="7000", target="9000"), state(), cfg())  # 1000pt stop
    assert rej(d) == Code.SIZE_BELOW_MIN


# R-RISK-02
def test_daily_loss_limit_blocks_all_further_entries() -> None:
    lim = daily_loss_limit(cfg(), D("10000"))
    assert lim == D("150")  # 3R (150) is smaller than 2% (200)
    for _ in range(10):
        assert rej(evaluate(intent(), state(day_pnl=-lim), cfg())) == Code.DAILY_LOSS_LIMIT


def test_daily_budget_sizes_down_then_refuses() -> None:
    d = evaluate(intent(), state(day_pnl=D("-120")), cfg())  # 30 left
    assert isinstance(d, Approved) and d.risk_gbp <= D("30")
    assert rej(evaluate(intent(), state(day_pnl=D("-149.5")), cfg())) == \
        Code.DAILY_BUDGET_EXCEEDED


# R-RISK-03
def test_weekly_halt_until_review() -> None:
    lim = weekly_loss_limit(cfg(), D("10000"))
    assert lim == D("500")
    assert rej(evaluate(intent(), state(week_pnl=-lim), cfg())) == Code.WEEKLY_HALT
    assert isinstance(evaluate(intent(), state(week_pnl=-lim, weekly_review_logged=True), cfg()),
                      Approved)


# R-RISK-04
def test_open_risk_cap() -> None:
    open_ = tuple(OpenRisk(f"X{i}", None, D("50"), D("0")) for i in range(4))  # 200 = cap
    assert rej(evaluate(intent(), state(open_risk=open_), cfg())) == Code.OPEN_RISK_CAP


# R-RISK-05
def test_correlated_positions_share_one_unit() -> None:
    spx_open = (OpenRisk("SPX", "us_index", D("50"), D("0")),)
    d = evaluate(intent(inst=NDX), state(open_risk=spx_open), cfg())
    assert rej(d) == Code.CORRELATED_RISK_CAP
    half = (OpenRisk("SPX", "us_index", D("25"), D("0")),)
    d2 = evaluate(intent(inst=NDX), state(open_risk=half), cfg())
    assert isinstance(d2, Approved) and d2.risk_gbp + D("25") <= D("50")
    # Uncorrelated instrument is unaffected
    assert isinstance(evaluate(intent(inst=FTSE), state(open_risk=spx_open), cfg()), Approved)


def test_group_defaults_to_symbol() -> None:
    o = OpenRisk("FOO", None, D("1"), D("0"))
    assert o.group == "FOO"


# R-RISK-06
def test_trade_count_cap() -> None:
    assert isinstance(evaluate(intent(), state(trades_today=4), cfg()), Approved)
    assert rej(evaluate(intent(), state(trades_today=5), cfg())) == Code.TRADE_COUNT_CAP


# R-SCAN-02
def test_off_plan_rejected_without_override() -> None:
    assert rej(evaluate(intent(), state(plan_symbols=frozenset()), cfg())) == Code.OFF_PLAN
    d = evaluate(intent(override_reason="news"), state(plan_symbols=frozenset()), cfg())
    assert isinstance(d, Approved) and d.off_plan_override and "OFF_PLAN" in d.warnings[0]


# R-RISK-07
def test_time_windows() -> None:
    c = cfg()
    assert rej(evaluate(intent(), state(now=OPEN - timedelta(minutes=1)), c)) == \
        Code.OUTSIDE_SESSION
    assert rej(evaluate(intent(), state(now=CLOSE), c)) == Code.OUTSIDE_SESSION
    assert rej(evaluate(intent(), state(now=OPEN + timedelta(minutes=5)), c)) == Code.TIME_WINDOW
    assert rej(evaluate(intent(), state(now=CLOSE - timedelta(minutes=5)), c)) == Code.TIME_WINDOW
    ok = cfg(open_close_windows_tradeable=True)
    assert isinstance(evaluate(intent(), state(now=OPEN + timedelta(minutes=5)), ok), Approved)


# R-RISK-08
def test_leverage_cap_binds_before_broker() -> None:
    big = (OpenRisk("X", None, D("0"), D("49000")),)
    assert rej(evaluate(intent(), state(open_risk=big), cfg())) == Code.LEVERAGE_CAP


# R-RISK-09
US = Instrument(symbol="AAPL", finra_regulated=True, min_size=D("1"), size_step=D("1"))


def test_pdt_guard() -> None:
    c = cfg()
    i = intent(inst=US, entry="200", stop="199", target="203")
    plan = frozenset({"AAPL"})
    d = evaluate(i, state(day_trades_5d=2, plan_symbols=plan), c)
    assert isinstance(d, Approved) and any("PDT" in w for w in d.warnings)
    assert rej(evaluate(i, state(day_trades_5d=3, plan_symbols=plan), c)) == Code.PDT_GUARD
    d0 = evaluate(i, state(day_trades_5d=0, plan_symbols=plan), c)
    assert isinstance(d0, Approved) and d0.warnings == ()
    rich = state(day_trades_5d=3, plan_symbols=plan, equity=D("30000"),
                 day_start_equity=D("30000"), week_start_equity=D("30000"))
    assert isinstance(evaluate(i, rich, c), Approved)


def test_pdt_not_applied_when_not_reported() -> None:
    i = intent(inst=US, entry="200", stop="199", target="203")
    d = evaluate(i, state(day_trades_5d=None, plan_symbols=frozenset({"AAPL"})), cfg())
    assert isinstance(d, Approved)


def test_rolling_business_days_with_holidays() -> None:
    fri, mon, tue = date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)
    trades = [date(2026, 11, 2), date(2026, 11, 3), fri, mon]  # Mon, Tue, Fri, Mon
    # window ending Tue 10th: Tue,Mon,Fri,Thu,Wed(4th) -> fri, mon = 2
    assert count_day_trades_5d(trades, tue) == 2
    # Holiday on Wed 4th pushes the window back to include Tue 3rd
    assert count_day_trades_5d(trades, tue, frozenset({date(2026, 11, 4)})) == 3


# R-RISK-10
def test_event_blackout() -> None:
    ev = (NOW + timedelta(minutes=3),)
    assert rej(evaluate(intent(), state(events=ev), cfg())) == Code.EVENT_BLACKOUT
    far = (NOW + timedelta(minutes=30),)
    assert isinstance(evaluate(intent(), state(events=far), cfg()), Approved)
    assert isinstance(evaluate(intent(), state(events=ev), cfg(is_event_strategy=True)), Approved)


# R-RISK-11
def test_halt_blocks() -> None:
    assert rej(evaluate(intent(), state(halted=True), cfg())) == Code.HALTED


def test_kill_k1() -> None:
    assert rej(evaluate(intent(), state(cumulative_live_pnl=D("-1000")), cfg())) == Code.KILL_K1


# R-RISK-13
def test_limit_change_locked_while_positions_open() -> None:
    old, looser = cfg(), cfg(per_trade_risk_pct=D("1.0"), no_entry_after_open_min=0,
                             is_event_strategy=True)
    r = check_limit_change(old, looser, positions_open=True)
    assert r is not None and r.code == Code.LIMIT_CHANGE_LOCKED
    assert check_limit_change(old, looser, positions_open=False) is None
    tighter = cfg(per_trade_risk_pct=D("0.25"))
    assert check_limit_change(old, tighter, positions_open=True) is None
    assert set(loosened_fields(old, looser)) == {"per_trade_risk_pct",
                                                 "no_entry_after_open_min", "is_event_strategy"}


def test_config_load(tmp_path: object) -> None:
    from pathlib import Path
    p = Path(str(tmp_path)) / "r.toml"
    p.write_text('[risk]\nper_trade_risk_pct = 0.25\ndisconnect_policy = "FLATTEN"\n'
                 "kill_k1_absolute_loss_gbp = 500\n")
    c = RiskConfig.load(p)
    assert c.per_trade_risk_pct == D("0.25") and c.kill_k1_absolute_loss_gbp == 500


def test_money_fields_are_never_float() -> None:
    """R-TECH-01: no float-typed field on any money/price model."""
    import typing

    from tradingdesk import domain
    from tradingdesk.risk import config, engine
    models = [domain.Instrument, domain.Intent, domain.BracketOrder, domain.AccountSnapshot,
              domain.Position, domain.Fill, domain.WorkingOrder, config.RiskConfig]
    for m in models:
        for name, f in m.model_fields.items():
            assert "float" not in str(f.annotation), f"{m.__name__}.{name} is float"
    for dc in (engine.RiskState, engine.Approved, engine.OpenRisk):
        for name, hint in typing.get_type_hints(dc).items():
            assert "float" not in str(hint), f"{dc.__name__}.{name} is float"
