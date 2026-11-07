from __future__ import annotations

from decimal import Decimal as D

import pytest

from tradingdesk.adapters.base import BrokerError, BrokerErrorKind
from tradingdesk.adapters.paper import PaperAdapter
from tradingdesk.domain import BracketOrder, Instrument, OrderType, Side

INST = Instrument(symbol="X")


def br(cid: str, side: Side = Side.BUY, ot: OrderType = OrderType.MARKET, entry: str = "100",
       stop: str = "90", target: str = "120", qty: str = "2") -> BracketOrder:
    return BracketOrder(client_order_id=cid, instrument=INST, side=side, qty=D(qty), order_type=ot,
                        entry_price=D(entry), stop_price=D(stop), target_price=D(target))


@pytest.fixture
def pa() -> PaperAdapter:
    a = PaperAdapter(spread_points=D("1"), slippage_points=D("0.5"))
    a.set_price("X", D("100"))
    return a


def test_market_fill_pays_spread_and_slippage(pa: PaperAdapter) -> None:
    pa.place_bracket(br("a"))
    assert pa.positions()[0].avg_entry == D("101.0")  # 100 + 0.5 half-spread + 0.5 slip
    pa.place_bracket(br("b", Side.SELL, stop="110", target="80"))
    assert pa.positions()[1].avg_entry == D("99.0")


def test_limit_needs_trade_through(pa: PaperAdapter) -> None:
    pa.place_bracket(br("a", ot=OrderType.LIMIT, entry="95", stop="85", target="110"))
    pa.set_price("X", D("95.5"))  # ask = 96: not through 95
    assert pa.positions() == []
    pa.set_price("X", D("95"))  # ask = 95.5: still not through
    assert pa.positions() == []
    pa.set_price("X", D("94"))  # ask = 94.5 < 95: fills at 95
    assert pa.positions()[0].avg_entry == D("95")


def test_sell_limit_and_stop_entries(pa: PaperAdapter) -> None:
    pa.place_bracket(br("s", Side.SELL, OrderType.LIMIT, "105", "115", "90"))
    pa.place_bracket(br("b", Side.BUY, OrderType.STOP, "103", "95", "120"))
    pa.place_bracket(br("c", Side.SELL, OrderType.STOP, "97", "105", "80"))
    pa.set_price("Y", D("1"))  # other symbol: no effect
    pa.set_price("X", D("104"))  # buy stop triggers (ask 104.5 >= 103)
    assert {p.side for p in pa.positions()} == {Side.BUY}
    pa.set_price("X", D("106"))  # sell limit fills (bid 105.5 > 105)
    assert len(pa.positions()) == 2
    pa.set_price("X", D("96"))  # sell stop entry triggers; buy stop-loss not yet (95)
    assert len(pa.working_orders()) == 0


def test_partial_fills_accumulate() -> None:
    pa = PaperAdapter(partial_fill_fraction=D("0.5"))
    pa.set_price("X", D("100"))
    pa.place_bracket(br("a", ot=OrderType.LIMIT, entry="95", stop="85", target="110", qty="4"))
    pa.set_price("X", D("94"))
    assert pa.positions()[0].qty == D("2") and pa.working_orders()[0].qty == D("2")
    pa.set_price("X", D("93"))
    assert pa.positions()[0].qty == D("3")
    assert len(pa.positions()) == 1


def test_sell_stop_and_target(pa: PaperAdapter) -> None:
    pa.place_bracket(br("a", Side.SELL, stop="110", target="80"))
    pa.set_price("X", D("79"))
    assert pa.positions() == []
    pa.place_bracket(br("b", Side.SELL, stop="110", target="70"))
    fills = pa.set_price("X", D("112"))
    assert fills[0].price == D("112.5")  # gapped through the stop


def test_rejects_and_errors(pa: PaperAdapter) -> None:
    with pytest.raises(BrokerError) as e:
        pa.place_bracket(br("a", stop="99.5"))
    assert e.value.kind is BrokerErrorKind.INVALID_LEVEL
    with pytest.raises(BrokerError) as e2:
        pa.place_bracket(br("b").model_copy(update={"instrument": Instrument(symbol="NOPE")}))
    assert e2.value.kind is BrokerErrorKind.MARKET_CLOSED
    with pytest.raises(BrokerError):
        pa.modify("nope", "1", None)


def test_modify_cancel_flatten_account(pa: PaperAdapter) -> None:
    ids = pa.place_bracket(br("a"))
    pa.modify(ids.entry_id, "95", "130")
    assert pa.positions()[0].stop_level == D("95")
    w = pa.place_bracket(br("w", ot=OrderType.LIMIT, entry="90", stop="80", target="100"))
    pa.cancel(w.entry_id)
    assert pa.working_orders() == []
    pa.place_bracket(br("w2", ot=OrderType.LIMIT, entry="90", stop="80", target="100"))
    pa.cancel_all()
    assert pa.working_orders() == []
    assert pa.account().equity < D("10000")  # paid the spread
    pa.flatten_all()
    assert pa.positions() == [] and pa.account().equity == pa.account().cash
    assert list(pa.subscribe([INST]))[0].bid == D("99.5")
    assert pa.day_trade_count_5d() is None
    assert len(pa.fills()) == 2
    pa.connect()
