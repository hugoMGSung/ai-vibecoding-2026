from decimal import Decimal

from auto_trader.paper import PaperBroker
from auto_trader.models import OrderRequest


def test_paper_buy_reduces_cash_and_adds_position() -> None:
    broker = PaperBroker(Decimal("1000"))
    order = broker.place(OrderRequest(symbol="AAA", side="BUY", quantity=Decimal("2"), price=Decimal("100")))
    assert order.status == "FILLED"
    assert broker.portfolio().cash == Decimal("800")
    assert broker.portfolio().positions["AAA"] == Decimal("2")


def test_insufficient_cash_rejects_order() -> None:
    broker = PaperBroker(Decimal("100"))
    order = broker.place(OrderRequest(symbol="AAA", side="BUY", quantity=Decimal("2"), price=Decimal("100")))
    assert order.status == "REJECTED"
