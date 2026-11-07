from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from tradingdesk.domain import DisconnectPolicy, Instrument, Intent, Side
from tradingdesk.risk.config import RiskConfig
from tradingdesk.risk.engine import RiskState

NOW = datetime(2026, 11, 9, 11, 0, tzinfo=UTC)  # Monday, mid-session
OPEN = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
CLOSE = datetime(2026, 11, 9, 16, 30, tzinfo=UTC)

FTSE = Instrument(symbol="FTSE", correlation_group="uk_index", min_size=D("0.5"),
                  size_step=D("0.01"))
SPX = Instrument(symbol="SPX", correlation_group="us_index")
NDX = Instrument(symbol="NDX", correlation_group="us_index")


def cfg(**kw: object) -> RiskConfig:
    base: dict[str, object] = {"disconnect_policy": DisconnectPolicy.REST_ON_BROKER_STOPS,
                               "kill_k1_absolute_loss_gbp": D("1000")}
    base.update(kw)
    return RiskConfig.model_validate(base)


def state(**kw: object) -> RiskState:
    base: dict[str, object] = dict(
        now=NOW, equity=D("10000"), day_start_equity=D("10000"), week_start_equity=D("10000"),
        day_pnl=D("0"), week_pnl=D("0"), cumulative_live_pnl=D("0"), trades_today=0,
        session_open=OPEN, session_close=CLOSE, plan_symbols=frozenset({"FTSE", "SPX", "NDX"}),
    )
    base.update(kw)
    return RiskState(**base)  # type: ignore[arg-type]


def intent(inst: Instrument = FTSE, side: Side = Side.BUY, entry: str = "8000",
           stop: str = "7980", target: str = "8040", **kw: object) -> Intent:
    return Intent(instrument=inst, side=side, entry_price=D(entry), stop_price=D(stop),
                  target_price=D(target), reason="test", pre_trade_note="note", **kw)  # type: ignore[arg-type]


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return cfg()
