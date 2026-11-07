"""IG adapter against a fake HTTP session. No network, no credentials."""

from __future__ import annotations

import json
from decimal import Decimal as D
from typing import Any

import pytest
import requests

from tradingdesk.adapters.base import BrokerError, BrokerErrorKind
from tradingdesk.adapters.ig import IGAdapter, IGCredentials, _Throttle, map_ig_error
from tradingdesk.domain import BracketOrder, Instrument, OrderType, Side

EPIC = "IX.D.FTSE.DAILY.IP"


class Resp:
    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeIG:
    """Minimal in-memory IG: enough to exercise the adapter's request flow."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []
        self.confirms: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = []
        self.working: list[dict[str, Any]] = []
        self.reject_reason: str | None = None
        self.timeout_next_post = False
        self.confirm_delay = 0

    def request(self, method: str, url: str, json: Any = None, headers: Any = None,
                timeout: Any = None) -> Resp:
        path = url.split("/gateway/deal")[1]
        h = headers or {}
        self.calls.append((method, path, json, h))
        if method == "POST" and path == "/session":
            if json["password"] == "bad":
                return Resp(401, {"errorCode": "error.security.invalid-details"})
            return Resp(200, {"currentAccountId": "ABC"},
                        {"CST": "c", "X-SECURITY-TOKEN": "x"})
        if path == "/session" and method == "PUT":
            return Resp(200, {})
        if path == "/accounts":
            return Resp(200, {"accounts": [{"accountId": "ABC", "currency": "GBP", "balance": {
                "balance": 10000.5, "profitLoss": -20.25, "deposit": 300, "available": 9000}}]})
        if path.startswith("/markets/"):
            return Resp(200, {"instrument": {"expiry": "DFB"},
                              "dealingRules": {"minDealSize": {"value": 0.5}},
                              "snapshot": {"bid": 7999.5, "offer": 8000.5,
                                           "marketStatus": "TRADEABLE"}})
        if path.startswith("/confirms/"):
            ref = path.rsplit("/", 1)[1]
            if self.confirm_delay > 0 and ref in self.confirms:
                self.confirm_delay -= 1
                return Resp(404, {"errorCode": "error.confirms.deal-not-found"})
            if ref in self.confirms:
                return Resp(200, self.confirms[ref])
            return Resp(404, {"errorCode": "error.confirms.deal-not-found"})
        if method == "POST" and path in ("/positions/otc", "/workingorders/otc") \
                and h.get("_method") != "DELETE":
            ref = json["dealReference"]
            if self.reject_reason:
                self.confirms[ref] = {"dealStatus": "REJECTED", "reason": self.reject_reason}
            else:
                did = f"DI{len(self.confirms)}"
                self.confirms[ref] = {"dealStatus": "ACCEPTED", "dealId": did}
                if path == "/positions/otc":
                    self.positions.append({"position": {
                        "dealId": did, "direction": json["direction"], "size": float(json["size"]),
                        "level": 8000.5, "stopLevel": float(json["stopLevel"]),
                        "limitLevel": float(json["limitLevel"]),
                        "createdDateUTC": "2026-11-09T11:00:00"}, "market": {"epic": json["epic"]}})
                else:
                    self.working.append({"workingOrderData": {
                        "dealId": did, "direction": json["direction"], "epic": json["epic"],
                        "orderSize": float(json["size"]), "orderLevel": float(json["level"]),
                        "orderType": json["type"]}})
            if self.timeout_next_post:
                self.timeout_next_post = False
                raise requests.Timeout("lost reply")
            return Resp(200, {"dealReference": ref})
        if method == "POST" and path == "/positions/otc" and h.get("_method") == "DELETE":
            self.positions = [p for p in self.positions
                              if p["position"]["dealId"] != json["dealId"]]
            return Resp(200, {"dealReference": "close"})
        if method == "POST" and path.startswith("/workingorders/otc/"):
            did = path.rsplit("/", 1)[1]
            self.working = [w for w in self.working if w["workingOrderData"]["dealId"] != did]
            return Resp(200, {})
        if path == "/positions":
            return Resp(200, {"positions": self.positions})
        if path == "/workingorders":
            return Resp(200, {"workingOrders": self.working})
        if method == "PUT" and path.startswith("/positions/otc/"):
            return Resp(200, {})
        return Resp(500, None)


def make(fake: FakeIG, **kw: Any) -> IGAdapter:
    a = IGAdapter(IGCredentials("key", "user", kw.pop("pw", "pw"), kw.pop("acct", None)),
                  session=fake, sleep=lambda _: None)
    a.trade_throttle = _Throttle(1000)
    a.data_throttle = _Throttle(1000)
    return a


def bracket(ref: str = "ref1", ot: OrderType = OrderType.MARKET) -> BracketOrder:
    return BracketOrder(client_order_id=ref, instrument=Instrument(symbol=EPIC), side=Side.BUY,
                        qty=D("2.5"), order_type=ot, entry_price=D("8000"),
                        stop_price=D("7980"), target_price=D("8040"))


def test_login_and_account() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    assert f.headers == {"CST": "c", "X-SECURITY-TOKEN": "x"} and a.account_id == "ABC"
    acct = a.account()
    assert acct.equity == D("9980.25") and acct.margin_used == D("300")
    assert f.calls[0][3]["X-IG-API-KEY"] == "key" and f.calls[0][3]["Version"] == "2"


def test_switch_account() -> None:
    f = FakeIG()
    a = make(f, acct="XYZ")
    a.connect()
    assert any(c[0] == "PUT" and c[1] == "/session" for c in f.calls)
    with pytest.raises(BrokerError):
        a.account()  # XYZ not in /accounts


def test_bad_login_is_auth_error() -> None:
    with pytest.raises(BrokerError) as e:
        make(FakeIG(), pw="bad").connect()
    assert e.value.kind is BrokerErrorKind.AUTH


def test_market_bracket_has_stop_attached() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    ids = a.place_bracket(bracket())
    post = next(c for c in f.calls if c[1] == "/positions/otc")
    body = post[2]
    assert body is not None
    assert body["stopLevel"] == "7980" and body["limitLevel"] == "8040"
    assert body["dealReference"] == "ref1" and body["expiry"] == "DFB"
    assert body["guaranteedStop"] is False and body["forceOpen"] is True
    [p] = a.positions()
    assert p.deal_id == ids.deal_id and p.stop_level == D("7980")


def test_working_order_and_cancel_all() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    a.place_bracket(bracket("w1", OrderType.LIMIT))
    body = next(c for c in f.calls if c[1] == "/workingorders/otc")[2]
    assert body is not None and body["stopDistance"] == "20" and body["limitDistance"] == "40"
    assert len(a.working_orders()) == 1
    a.cancel_all()
    assert a.working_orders() == []


def test_idempotent_on_resubmit() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    a.place_bracket(bracket())
    a.place_bracket(bracket())  # same dealReference
    assert len(f.positions) == 1


def test_timeout_then_found_by_reference() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    f.timeout_next_post = True
    with pytest.raises(BrokerError) as e:
        a.place_bracket(bracket())
    assert e.value.kind is BrokerErrorKind.TIMEOUT
    assert a.find_by_client_id("ref1") is not None
    a.place_bracket(bracket())
    assert len(f.positions) == 1


def test_confirm_polling_and_reject() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    f.confirm_delay = 2  # first two confirm polls (pre-check + 1) see nothing
    a.place_bracket(bracket("slow"))
    f.reject_reason = "INSUFFICIENT_FUNDS"
    with pytest.raises(BrokerError) as e:
        a.place_bracket(bracket("poor"))
    assert e.value.kind is BrokerErrorKind.INSUFFICIENT_FUNDS
    assert a.find_by_client_id("poor") is None


def test_no_confirm_is_timeout() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    f.confirm_delay = 100
    with pytest.raises(BrokerError) as e:
        a.place_bracket(bracket("never"))
    assert e.value.kind is BrokerErrorKind.TIMEOUT


def test_flatten_modify_quote_instrument() -> None:
    f = FakeIG()
    a = make(f)
    a.connect()
    a.place_bracket(bracket())
    a.modify("DI0", "7990", None)
    a.modify("DI0", None, "8050")
    a.flatten_all()
    assert a.positions() == []
    q = a.quote(EPIC)
    assert q.bid == D("7999.5") and q.status == "TRADEABLE"
    assert a.instrument(EPIC, "uk_index").min_size == D("0.5")
    assert list(a.subscribe([Instrument(symbol=EPIC)]))[0].ask == D("8000.5")
    assert a.day_trade_count_5d() is None


def test_long_reference_refused() -> None:
    with pytest.raises(ValueError):
        make(FakeIG()).place_bracket(bracket("x" * 31))


def test_live_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TDESK_ALLOW_LIVE", raising=False)
    with pytest.raises(BrokerError):
        IGAdapter(IGCredentials("k", "u", "p", environment="live"), session=FakeIG())
    with pytest.raises(ValueError):
        IGAdapter(IGCredentials("k", "u", "p", environment="prod"), session=FakeIG())
    monkeypatch.setenv("TDESK_ALLOW_LIVE", "yes")
    assert IGAdapter(IGCredentials("k", "u", "p", environment="live"),
                     session=FakeIG()).base.startswith("https://api.ig.com")


def test_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("IG_API_KEY", "IG_USERNAME", "IG_PASSWORD", "IG_ACCOUNT_ID", "IG_ENV"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(BrokerError):
        IGCredentials.from_env()
    monkeypatch.setenv("IG_API_KEY", "k")
    monkeypatch.setenv("IG_USERNAME", "u")
    monkeypatch.setenv("IG_PASSWORD", "p")
    c = IGCredentials.from_env()
    assert c.environment == "demo" and c.account_id is None


@pytest.mark.parametrize("code,kind", [
    ("INSUFFICIENT_FUNDS", BrokerErrorKind.INSUFFICIENT_FUNDS),
    ("MARKET_CLOSED_WITH_EDITS", BrokerErrorKind.MARKET_CLOSED),
    ("error.public-api.exceeded-account-trading-allowance", BrokerErrorKind.RATE_LIMITED),
    ("error.security.client-token-invalid", BrokerErrorKind.AUTH),
    ("ATTACHED_ORDER_LEVEL_ERROR", BrokerErrorKind.INVALID_LEVEL),
    ("MINIMUM_ORDER_SIZE_ERROR", BrokerErrorKind.REJECTED_BY_BROKER),
    ("SOMETHING_NEW", BrokerErrorKind.UNKNOWN),
    (None, BrokerErrorKind.UNKNOWN),
])
def test_error_mapping(code: str | None, kind: BrokerErrorKind) -> None:
    assert map_ig_error(code) is kind


def test_throttle_sleeps_when_full() -> None:
    t = [0.0]
    slept: list[float] = []
    th = _Throttle(2, clock=lambda: t[0], sleep=lambda s: slept.append(s))
    th.wait()
    th.wait()
    th.wait()
    assert slept and slept[0] > 59
    t[0] = 120
    th.wait()
    assert len(slept) == 1
