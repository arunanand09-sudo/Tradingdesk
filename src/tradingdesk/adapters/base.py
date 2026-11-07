"""BrokerAdapter interface (PRD 9.5) and the single internal error taxonomy (R-ADP-02).

Only `tradingdesk.execution.gateway` may import from this package (R-ARCH-01).
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Protocol

from tradingdesk.domain import (
    AccountSnapshot,
    BracketOrder,
    BrokerOrderIds,
    Instrument,
    Position,
    WorkingOrder,
)


class BrokerErrorKind(StrEnum):
    REJECTED_BY_BROKER = "REJECTED_BY_BROKER"  # broker said no; nothing was placed
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    MARKET_CLOSED = "MARKET_CLOSED"
    INVALID_LEVEL = "INVALID_LEVEL"  # stop/limit too close etc.
    RATE_LIMITED = "RATE_LIMITED"
    AUTH = "AUTH"
    TIMEOUT = "TIMEOUT"  # outcome unknown: reconcile before doing anything else
    UNKNOWN = "UNKNOWN"  # always maps to HALT_NEW_ENTRIES, never retry


HALTING_KINDS = frozenset({BrokerErrorKind.UNKNOWN, BrokerErrorKind.AUTH})


class BrokerError(Exception):
    def __init__(self, kind: BrokerErrorKind, detail: str, raw: str | None = None) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.raw = raw


class MarketEvent(Protocol):
    symbol: str


class BrokerAdapter(Protocol):
    name: str

    def connect(self) -> None: ...
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> list[Position]: ...
    def working_orders(self) -> list[WorkingOrder]: ...
    def place_bracket(self, group: BracketOrder) -> BrokerOrderIds: ...
    def find_by_client_id(self, client_order_id: str) -> BrokerOrderIds | None: ...
    def modify(self, broker_order_id: str, stop: str | None, limit: str | None) -> None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def cancel_all(self) -> None: ...
    def flatten_all(self) -> None: ...
    def subscribe(self, instruments: list[Instrument]) -> Iterator[MarketEvent]: ...
    def day_trade_count_5d(self) -> int | None: ...
