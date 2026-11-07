"""IG Markets REST adapter (spread bet or CFD account).

Talks to the IG REST trading API directly with `requests`. Entries go in as a
single request carrying the stop and limit levels, so the stop is resting at IG
from the moment the position exists (R-EXE-01, R-EXE-04). Every order carries a
client-generated `dealReference`, which is how idempotency works (R-EXE-02):
before placing, and after any timeout, the adapter asks IG for the confirm of
that reference.

Defaults to the DEMO environment. Live requires `TDESK_ALLOW_LIVE=yes`.

Not implemented yet: Lightstreamer streaming. `subscribe` polls snapshots,
which is enough for the operator-driven flow but not for automated execution.

Verify endpoint versions and field names against labs.ig.com before going live.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import requests

from tradingdesk.adapters.base import BrokerError, BrokerErrorKind
from tradingdesk.domain import (
    AccountSnapshot,
    BracketOrder,
    BrokerOrderIds,
    Instrument,
    OrderType,
    Position,
    Side,
    WorkingOrder,
)

URLS = {
    "demo": "https://demo-api.ig.com/gateway/deal",
    "live": "https://api.ig.com/gateway/deal",
}
D = Decimal


class HttpSession(Protocol):
    headers: Any

    def request(self, method: str, url: str, **kw: Any) -> Any: ...


@dataclass(frozen=True)
class IGCredentials:
    api_key: str
    username: str
    password: str
    account_id: str | None = None
    environment: str = "demo"

    @classmethod
    def from_env(cls) -> IGCredentials:
        missing = [k for k in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD") if not os.getenv(k)]
        if missing:
            raise BrokerError(BrokerErrorKind.AUTH, f"missing env vars: {', '.join(missing)}")
        return cls(
            api_key=os.environ["IG_API_KEY"],
            username=os.environ["IG_USERNAME"],
            password=os.environ["IG_PASSWORD"],
            account_id=os.getenv("IG_ACCOUNT_ID") or None,
            environment=os.getenv("IG_ENV", "demo").lower(),
        )


@dataclass
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    status: str


class _Throttle:
    """Keep under IG's per-minute allowances (about 40 trade requests/min)."""

    def __init__(self, per_minute: int, clock: Any = time.monotonic, sleep: Any = time.sleep):
        self.per_minute, self.clock, self.sleep = per_minute, clock, sleep
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = self.clock()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.per_minute:
            self.sleep(60 - (now - self.calls[0]) + 0.05)
        self.calls.append(self.clock())


def map_ig_error(code: str | None) -> BrokerErrorKind:
    """IG error / reject reason -> internal taxonomy. Unknown means halt (R-ADP-02)."""
    c = (code or "").upper()
    if not c:
        return BrokerErrorKind.UNKNOWN
    if "INSUFFICIENT_FUNDS" in c:
        return BrokerErrorKind.INSUFFICIENT_FUNDS
    if "MARKET_CLOSED" in c or "MARKET_OFFLINE" in c or "MARKET_NOT_TRADEABLE" in c:
        return BrokerErrorKind.MARKET_CLOSED
    if "EXCEEDED" in c and "ALLOWANCE" in c:
        return BrokerErrorKind.RATE_LIMITED
    if "SECURITY" in c or "TOKEN" in c or "INVALID-DETAILS" in c or "CLIENT-TOKEN" in c:
        return BrokerErrorKind.AUTH
    if "LEVEL" in c or "DISTANCE" in c:
        return BrokerErrorKind.INVALID_LEVEL
    if "MINIMUM_ORDER_SIZE" in c or "SIZE" in c or "REJECT" in c:
        return BrokerErrorKind.REJECTED_BY_BROKER
    return BrokerErrorKind.UNKNOWN


class IGAdapter:
    name = "ig"

    def __init__(self, creds: IGCredentials, session: HttpSession | None = None,
                 timeout_s: float = 10.0, confirm_retries: int = 5,
                 sleep: Any = time.sleep) -> None:
        if creds.environment not in URLS:
            raise ValueError("environment must be 'demo' or 'live'")
        if creds.environment == "live" and os.getenv("TDESK_ALLOW_LIVE") != "yes":
            raise BrokerError(BrokerErrorKind.AUTH,
                              "live IG environment refused: set TDESK_ALLOW_LIVE=yes deliberately")
        self.creds = creds
        self.base = URLS[creds.environment]
        self.http: HttpSession = session or requests.Session()
        self.timeout_s = timeout_s
        self.confirm_retries = confirm_retries
        self.sleep = sleep
        self.trade_throttle = _Throttle(35)
        self.data_throttle = _Throttle(55)
        self._markets: dict[str, dict[str, Any]] = {}
        self.account_id: str | None = None

    # ---- HTTP --------------------------------------------------------------------
    def _req(self, method: str, path: str, version: int = 1, body: dict[str, Any] | None = None,
             trade: bool = False, extra_headers: dict[str, str] | None = None) -> Any:
        (self.trade_throttle if trade else self.data_throttle).wait()
        headers = {"Version": str(version), "Accept": "application/json; charset=UTF-8",
                   "Content-Type": "application/json; charset=UTF-8",
                   "X-IG-API-KEY": self.creds.api_key}
        headers.update(extra_headers or {})
        try:
            r = self.http.request(method, self.base + path, json=body, headers=headers,
                                  timeout=self.timeout_s)
        except requests.Timeout as e:
            raise BrokerError(BrokerErrorKind.TIMEOUT, f"{method} {path} timed out") from e
        except requests.ConnectionError as e:
            raise BrokerError(BrokerErrorKind.TIMEOUT, f"{method} {path} connection: {e}") from e
        if r.status_code >= 400:
            try:
                code = r.json().get("errorCode")
            except ValueError:
                code = None
            raise BrokerError(map_ig_error(code), f"{method} {path} -> {r.status_code} {code}",
                              raw=getattr(r, "text", None))
        return r

    @staticmethod
    def _json(r: Any) -> dict[str, Any]:
        try:
            out: dict[str, Any] = r.json()
            return out
        except ValueError:
            return {}

    # ---- BrokerAdapter -----------------------------------------------------------
    def connect(self) -> None:
        r = self._req("POST", "/session", version=2,
                      body={"identifier": self.creds.username, "password": self.creds.password})
        cst, xst = r.headers.get("CST"), r.headers.get("X-SECURITY-TOKEN")
        if not cst or not xst:
            raise BrokerError(BrokerErrorKind.AUTH, "login returned no session tokens")
        self.http.headers.update({"CST": cst, "X-SECURITY-TOKEN": xst})
        current = self._json(r).get("currentAccountId")
        want = self.creds.account_id
        if want and want != current:
            self._req("PUT", "/session", body={"accountId": want, "defaultAccount": False})
        self.account_id = want or current

    def account(self) -> AccountSnapshot:
        accts = self._json(self._req("GET", "/accounts")).get("accounts", [])
        acct = next((a for a in accts if a.get("accountId") == self.account_id), None)
        if acct is None:
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"account {self.account_id} not found")
        bal = acct["balance"]
        cash = D(str(bal["balance"]))
        pl = D(str(bal.get("profitLoss", 0)))
        margin = D(str(bal.get("deposit", 0)))
        return AccountSnapshot(ts_utc=datetime.now(UTC), equity=cash + pl, cash=cash,
                               margin_used=margin, currency=acct.get("currency", "GBP"))

    def positions(self) -> list[Position]:
        out = []
        for row in self._json(self._req("GET", "/positions", version=2)).get("positions", []):
            p, m = row["position"], row["market"]
            out.append(Position(
                deal_id=p["dealId"], symbol=m["epic"], side=Side(p["direction"]),
                qty=D(str(p["size"])), avg_entry=D(str(p["level"])),
                stop_level=_dec(p.get("stopLevel")), limit_level=_dec(p.get("limitLevel")),
                opened_at=_ts(p.get("createdDateUTC")),
            ))
        return out

    def working_orders(self) -> list[WorkingOrder]:
        out = []
        data = self._json(self._req("GET", "/workingorders", version=2))
        for row in data.get("workingOrders", []):
            w = row["workingOrderData"]
            out.append(WorkingOrder(
                order_id=w["dealId"], symbol=w["epic"], side=Side(w["direction"]),
                qty=D(str(w["orderSize"])), level=D(str(w["orderLevel"])),
                order_type=OrderType(w.get("orderType", "LIMIT")),
            ))
        return out

    def market(self, epic: str) -> dict[str, Any]:
        if epic not in self._markets:
            self._markets[epic] = self._json(self._req("GET", f"/markets/{epic}", version=3))
        return self._markets[epic]

    def instrument(self, epic: str, correlation_group: str | None = None) -> Instrument:
        """Build an Instrument from IG's market details (min size, min stop distance)."""
        m = self.market(epic)
        rules = m.get("dealingRules", {})
        min_size = D(str(rules.get("minDealSize", {}).get("value", "0.5")))
        return Instrument(symbol=epic, venue="IG", currency="GBP",
                          value_per_point=D("1"), min_size=min_size, size_step=D("0.01"),
                          correlation_group=correlation_group)

    def quote(self, epic: str) -> Quote:
        snap = self._json(self._req("GET", f"/markets/{epic}", version=3)).get("snapshot", {})
        return Quote(epic, D(str(snap["bid"])), D(str(snap["offer"])),
                     snap.get("marketStatus", "UNKNOWN"))

    def _confirm(self, deal_ref: str) -> dict[str, Any] | None:
        try:
            return self._json(self._req("GET", f"/confirms/{deal_ref}"))
        except BrokerError as e:
            if e.raw is not None and "error.confirms.deal-not-found" in e.raw:
                return None
            if "404" in e.detail:
                return None
            raise

    def find_by_client_id(self, client_order_id: str) -> BrokerOrderIds | None:
        c = self._confirm(client_order_id)
        if not c or c.get("dealStatus") != "ACCEPTED":
            return None
        did = c.get("dealId", "")
        return BrokerOrderIds(client_order_id=client_order_id, entry_id=did, deal_id=did,
                              stop_id=f"{did}:stop", target_id=f"{did}:limit")

    def place_bracket(self, group: BracketOrder) -> BrokerOrderIds:
        ref = group.client_order_id
        if len(ref) > 30:
            raise ValueError("IG dealReference must be at most 30 characters")
        existing = self.find_by_client_id(ref)
        if existing:
            return existing
        epic = group.instrument.symbol
        expiry = self.market(epic).get("instrument", {}).get("expiry", "DFB")
        if group.order_type is OrderType.MARKET:
            body: dict[str, Any] = {
                "epic": epic, "expiry": expiry, "direction": group.side.value,
                "size": str(group.qty), "orderType": "MARKET",
                "timeInForce": "FILL_OR_KILL", "guaranteedStop": False, "trailingStop": False,
                "forceOpen": True, "currencyCode": group.instrument.currency,
                "stopLevel": str(group.stop_price), "limitLevel": str(group.target_price),
                "dealReference": ref,
            }
            path = "/positions/otc"
        else:
            body = {
                "epic": epic, "expiry": expiry, "direction": group.side.value,
                "size": str(group.qty), "level": str(group.entry_price),
                "type": group.order_type.value, "timeInForce": "GOOD_TILL_CANCELLED",
                "guaranteedStop": False, "forceOpen": True,
                "currencyCode": group.instrument.currency,
                "stopDistance": str(abs(group.entry_price - group.stop_price)),
                "limitDistance": str(abs(group.target_price - group.entry_price)),
                "dealReference": ref,
            }
            path = "/workingorders/otc"
        self._req("POST", path, version=2, body=body, trade=True)
        # IG accepts the request asynchronously; the confirm says what happened.
        for attempt in range(self.confirm_retries):
            c = self._confirm(ref)
            if c is not None:
                if c.get("dealStatus") == "ACCEPTED":
                    did = c["dealId"]
                    return BrokerOrderIds(client_order_id=ref, entry_id=did, deal_id=did,
                                          stop_id=f"{did}:stop", target_id=f"{did}:limit")
                reason = c.get("reason")
                raise BrokerError(map_ig_error(reason) if reason else BrokerErrorKind.UNKNOWN,
                                  f"IG rejected deal: {reason}")
            self.sleep(0.5 * (attempt + 1))
        raise BrokerError(BrokerErrorKind.TIMEOUT, f"no confirm for {ref}; reconcile required")

    def modify(self, broker_order_id: str, stop: str | None, limit: str | None) -> None:
        body: dict[str, Any] = {"trailingStop": False}
        if stop is not None:
            body["stopLevel"] = stop
        if limit is not None:
            body["limitLevel"] = limit
        self._req("PUT", f"/positions/otc/{broker_order_id}", version=2, body=body, trade=True)

    def cancel(self, broker_order_id: str) -> None:
        self._req("POST", f"/workingorders/otc/{broker_order_id}", version=2, trade=True,
                  extra_headers={"_method": "DELETE"})

    def cancel_all(self) -> None:
        for w in self.working_orders():
            self.cancel(w.order_id)

    def flatten_all(self) -> None:
        for p in self.positions():
            body = {"dealId": p.deal_id, "direction": "SELL" if p.side is Side.BUY else "BUY",
                    "size": str(p.qty), "orderType": "MARKET", "timeInForce": "FILL_OR_KILL"}
            self._req("POST", "/positions/otc", version=1, body=body, trade=True,
                      extra_headers={"_method": "DELETE"})

    def subscribe(self, instruments: list[Instrument]) -> Iterator[Quote]:
        for i in instruments:
            yield self.quote(i.symbol)

    def day_trade_count_5d(self) -> int | None:
        return None  # PDT does not apply to IG spread bets / CFDs


def _dec(v: Any) -> Decimal | None:
    return None if v is None else D(str(v))


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "")).replace(tzinfo=UTC)
