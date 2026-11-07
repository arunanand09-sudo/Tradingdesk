"""Paper adapter (R-ADP-01): a first-class broker simulation, not a stub.

Models spread, slippage, gaps through stops, limit orders that fill only when
price trades through the level, partial fills, broker rejects (stop too close),
simulated timeouts, and idempotency by client order id. Stops and targets rest
"at the broker" (inside this adapter's persisted state), so killing the calling
process leaves them in force, as with a real broker (R-EXE-04).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingdesk.adapters.base import BrokerError, BrokerErrorKind
from tradingdesk.domain import (
    AccountSnapshot,
    BracketOrder,
    BrokerOrderIds,
    Fill,
    Instrument,
    OrderType,
    Position,
    Side,
    WorkingOrder,
)

D = Decimal


@dataclass
class PriceTick:
    symbol: str
    bid: Decimal
    ask: Decimal


class PaperAdapter:
    name = "paper"

    def __init__(
        self,
        starting_equity: Decimal = D("10000"),
        state_path: str | Path | None = None,
        spread_points: Decimal = D("1"),
        slippage_points: Decimal = D("0"),
        min_stop_distance: Decimal = D("2"),
        partial_fill_fraction: Decimal = D("1"),
    ) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.spread = spread_points
        self.slippage = slippage_points
        self.min_stop_distance = min_stop_distance
        self.partial_fill_fraction = partial_fill_fraction
        self.fail_next_with_timeout = False  # test hook: place the order, then "lose" the reply
        self.reject_next: str | None = None  # test hook: broker rejects next order
        self.s: dict[str, Any] = {
            "cash": str(starting_equity),
            "prices": {},
            "positions": {},
            "working": {},
            "by_client": {},
            "fills": [],
        }
        if self.state_path and self.state_path.exists():
            self.s = json.loads(self.state_path.read_text())

    # ---- persistence -------------------------------------------------------------
    def _save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1))
        os.replace(tmp, self.state_path)

    # ---- prices ------------------------------------------------------------------
    def _quote(self, symbol: str) -> tuple[Decimal, Decimal]:
        if symbol not in self.s["prices"]:
            raise BrokerError(BrokerErrorKind.MARKET_CLOSED, f"no price for {symbol}")
        mid = D(self.s["prices"][symbol])
        half = self.spread / 2
        return mid - half, mid + half

    def set_price(self, symbol: str, mid: Decimal) -> list[Fill]:
        """Move the market. Triggers resting entries, stops and targets."""
        self.s["prices"][symbol] = str(mid)
        fills = self._process_working(symbol) + self._process_positions(symbol)
        self._save()
        return fills

    # ---- BrokerAdapter -----------------------------------------------------------
    def connect(self) -> None:
        return None

    def account(self) -> AccountSnapshot:
        cash = D(self.s["cash"])
        upl = sum((self._upl(p) for p in self.s["positions"].values()), D("0"))
        return AccountSnapshot(ts_utc=datetime.now(UTC), equity=cash + upl, cash=cash)

    def positions(self) -> list[Position]:
        return [
            Position(
                deal_id=did, symbol=p["symbol"], side=Side(p["side"]), qty=D(p["qty"]),
                avg_entry=D(p["entry"]), stop_level=D(p["stop"]), limit_level=D(p["limit"]),
                opened_at=datetime.fromisoformat(p["opened_at"]),
            )
            for did, p in self.s["positions"].items()
        ]

    def working_orders(self) -> list[WorkingOrder]:
        return [
            WorkingOrder(order_id=oid, symbol=w["symbol"], side=Side(w["side"]), qty=D(w["qty"]),
                         level=D(w["entry"]), order_type=OrderType(w["type"]))
            for oid, w in self.s["working"].items()
        ]

    def find_by_client_id(self, client_order_id: str) -> BrokerOrderIds | None:
        rec = self.s["by_client"].get(client_order_id)
        return BrokerOrderIds.model_validate(rec) if rec else None

    def place_bracket(self, group: BracketOrder) -> BrokerOrderIds:
        existing = self.find_by_client_id(group.client_order_id)
        if existing:  # idempotent (R-EXE-02)
            return existing
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            raise BrokerError(BrokerErrorKind.REJECTED_BY_BROKER, reason)
        if abs(group.entry_price - group.stop_price) < self.min_stop_distance:
            raise BrokerError(BrokerErrorKind.INVALID_LEVEL, "stop closer than broker minimum")
        bid, ask = self._quote(group.instrument.symbol)

        oid = uuid.uuid4().hex[:12]
        rec: dict[str, Any] = {
            "symbol": group.instrument.symbol, "side": group.side.value, "qty": str(group.qty),
            "entry": str(group.entry_price), "stop": str(group.stop_price),
            "limit": str(group.target_price), "type": group.order_type.value,
            "client_id": group.client_order_id, "vpp": str(group.instrument.value_per_point),
        }
        if group.order_type is OrderType.MARKET:
            px = (ask + self.slippage) if group.side is Side.BUY else (bid - self.slippage)
            self._open(oid, rec, group.qty, px)
        else:
            self.s["working"][oid] = rec
        ids = BrokerOrderIds(client_order_id=group.client_order_id, entry_id=oid,
                             stop_id=f"{oid}:stop", target_id=f"{oid}:limit", deal_id=oid)
        self.s["by_client"][group.client_order_id] = ids.model_dump()
        self._save()
        if self.fail_next_with_timeout:
            self.fail_next_with_timeout = False
            raise BrokerError(BrokerErrorKind.TIMEOUT, "simulated timeout after placement")
        return ids

    def modify(self, broker_order_id: str, stop: str | None, limit: str | None) -> None:
        p = self.s["positions"].get(broker_order_id) or self.s["working"].get(broker_order_id)
        if p is None:
            raise BrokerError(BrokerErrorKind.REJECTED_BY_BROKER, "unknown order")
        if stop is not None:
            p["stop"] = stop
        if limit is not None:
            p["limit"] = limit
        self._save()

    def cancel(self, broker_order_id: str) -> None:
        self.s["working"].pop(broker_order_id, None)
        self._save()

    def cancel_all(self) -> None:
        self.s["working"] = {}
        self._save()

    def flatten_all(self) -> None:
        for did in list(self.s["positions"]):
            p = self.s["positions"][did]
            bid, ask = self._quote(p["symbol"])
            self._close(did, bid if p["side"] == "BUY" else ask, "flatten")
        self._save()

    def subscribe(self, instruments: list[Instrument]) -> Iterator[PriceTick]:
        for i in instruments:
            bid, ask = self._quote(i.symbol)
            yield PriceTick(i.symbol, bid, ask)

    def day_trade_count_5d(self) -> int | None:
        return None  # not applicable to spread bets / futures

    def fills(self) -> list[Fill]:
        return [Fill.model_validate(f) for f in self.s["fills"]]

    # ---- internals ---------------------------------------------------------------
    def _record_fill(self, client_id: str, symbol: str, side: Side, qty: Decimal,
                     price: Decimal) -> Fill:
        f = Fill(client_order_id=client_id, broker_exec_id=uuid.uuid4().hex[:12],
                 ts_utc=datetime.now(UTC), symbol=symbol, side=side, qty=qty, price=price)
        self.s["fills"].append(f.model_dump(mode="json"))
        return f

    def _open(self, did: str, rec: dict[str, Any], qty: Decimal, px: Decimal) -> Fill:
        pos = dict(rec, qty=str(qty), entry=str(px), opened_at=datetime.now(UTC).isoformat())
        self.s["positions"][did] = pos
        return self._record_fill(rec["client_id"], rec["symbol"], Side(rec["side"]), qty, px)

    def _add_fill(self, did: str, rec: dict[str, Any], qty: Decimal, px: Decimal) -> Fill:
        """Partial fills accumulate into one position under the same deal id."""
        p = self.s["positions"].get(did)
        if p is None:
            return self._open(did, rec, qty, px)
        old_q = D(p["qty"])
        p["entry"] = str((D(p["entry"]) * old_q + px * qty) / (old_q + qty))
        p["qty"] = str(old_q + qty)
        return self._record_fill(rec["client_id"], rec["symbol"], Side(rec["side"]), qty, px)

    def _upl(self, p: dict[str, Any]) -> Decimal:
        bid, ask = self._quote(p["symbol"])
        sign = 1 if p["side"] == "BUY" else -1
        exit_px = bid if sign == 1 else ask
        return (exit_px - D(p["entry"])) * D(p["qty"]) * D(p["vpp"]) * sign

    def _close(self, did: str, px: Decimal, why: str) -> Fill:
        p = self.s["positions"].pop(did)
        sign = 1 if p["side"] == "BUY" else -1
        pnl = (px - D(p["entry"])) * D(p["qty"]) * D(p["vpp"]) * sign
        self.s["cash"] = str(D(self.s["cash"]) + pnl)
        exit_side = Side.SELL if sign == 1 else Side.BUY
        return self._record_fill(p["client_id"] + ":" + why, p["symbol"], exit_side,
                                 D(p["qty"]), px)

    def _process_working(self, symbol: str) -> list[Fill]:
        out: list[Fill] = []
        bid, ask = self._quote(symbol)
        for oid in list(self.s["working"]):
            w = self.s["working"][oid]
            if w["symbol"] != symbol:
                continue
            lvl, buy = D(w["entry"]), w["side"] == "BUY"
            if w["type"] == "LIMIT":
                # queue model: must trade THROUGH the level, touching is not enough
                hit = ask < lvl if buy else bid > lvl
                px = lvl
            else:  # STOP entry
                hit = ask >= lvl if buy else bid <= lvl
                px = (max(ask, lvl) if buy else min(bid, lvl))
            if not hit:
                continue
            remaining = D(w["qty"])
            qty = remaining
            if self.partial_fill_fraction < 1:
                qty = min(remaining, max((remaining * self.partial_fill_fraction)
                                         .quantize(D("0.01")), D("0.01")))
            out.append(self._add_fill(oid, w, qty, px))
            if qty >= remaining:
                del self.s["working"][oid]
            else:
                w["qty"] = str(remaining - qty)
        return out

    def _process_positions(self, symbol: str) -> list[Fill]:
        out: list[Fill] = []
        bid, ask = self._quote(symbol)
        for did in list(self.s["positions"]):
            p = self.s["positions"][did]
            if p["symbol"] != symbol:
                continue
            stop, limit = D(p["stop"]), D(p["limit"])
            if p["side"] == "BUY":
                if bid <= stop:
                    out.append(self._close(did, min(bid, stop), "stop"))  # gaps fill worse
                elif bid >= limit:
                    out.append(self._close(did, limit, "limit"))
            else:
                if ask >= stop:
                    out.append(self._close(did, max(ask, stop), "stop"))
                elif ask <= limit:
                    out.append(self._close(did, limit, "limit"))
        return out
