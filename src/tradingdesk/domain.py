"""Validated boundary objects. All money and prices are Decimal (R-TECH-01)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class DisconnectPolicy(StrEnum):
    """R-EXE-04: there is no default and no third option."""

    FLATTEN = "FLATTEN"
    REST_ON_BROKER_STOPS = "REST_ON_BROKER_STOPS"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Instrument(Frozen):
    """An instrument as the risk engine needs to see it.

    For IG spread bets, `value_per_point` is GBP per point per 1 unit of size
    (normally 1). For futures it is the contract's currency value per point.
    """

    symbol: str  # IG epic, e.g. "IX.D.FTSE.DAILY.IP"
    venue: str = "IG"
    currency: str = "GBP"
    tick_size: Decimal = Decimal("1")
    value_per_point: Decimal = Decimal("1")
    min_size: Decimal = Decimal("0.5")
    size_step: Decimal = Decimal("0.01")
    correlation_group: str | None = None  # instruments sharing a group count as one bet
    finra_regulated: bool = False
    margin_factor: Decimal = Decimal("0.05")  # notional * factor = margin


class Intent(Frozen):
    """What the operator wants to do. A stop is mandatory (R-EXE-01).

    Quantity is never chosen here: the risk engine derives it (R-RISK-01).
    """

    intent_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    instrument: Instrument
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    order_type: OrderType = OrderType.MARKET
    reason: str = Field(min_length=1)
    pre_trade_note: str = Field(min_length=1)
    override_reason: str | None = None  # off-plan override (R-SCAN-02)

    @model_validator(mode="after")
    def _check_levels(self) -> Intent:
        if self.stop_price == self.entry_price:
            raise ValueError("stop_price must differ from entry_price")
        if self.side is Side.BUY:
            if not self.stop_price < self.entry_price < self.target_price:
                raise ValueError("BUY requires stop < entry < target")
        else:
            if not self.target_price < self.entry_price < self.stop_price:
                raise ValueError("SELL requires target < entry < stop")
        return self

    @property
    def stop_distance(self) -> Decimal:
        return abs(self.entry_price - self.stop_price)


class BracketOrder(Frozen):
    """Entry plus stop plus target, submitted as one unit."""

    client_order_id: str
    instrument: Instrument
    side: Side
    qty: Decimal
    order_type: OrderType
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal


class BrokerOrderIds(Frozen):
    client_order_id: str
    entry_id: str
    stop_id: str | None = None
    target_id: str | None = None
    deal_id: str | None = None


class AccountSnapshot(Frozen):
    ts_utc: datetime
    equity: Decimal
    cash: Decimal
    margin_used: Decimal = Decimal("0")
    currency: str = "GBP"


class Position(Frozen):
    deal_id: str
    symbol: str
    side: Side
    qty: Decimal
    avg_entry: Decimal
    stop_level: Decimal | None
    limit_level: Decimal | None
    opened_at: datetime | None = None


class WorkingOrder(Frozen):
    order_id: str
    symbol: str
    side: Side
    qty: Decimal
    level: Decimal
    order_type: OrderType


class Fill(Frozen):
    client_order_id: str
    broker_exec_id: str
    ts_utc: datetime
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")
