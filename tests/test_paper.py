from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from auto_trader.paper import PaperBroker
from auto_trader.models import OrderRequest


def test_paper_buy_reduces_cash_and_adds_position() -> None:
    db_path = Path(__file__).parent / f".paper-{uuid4().hex}.sqlite3"
    try:
        broker = PaperBroker(Decimal("1000"), Decimal("0"), str(db_path))
        order = broker.place(OrderRequest(symbol="AAA", side="BUY", quantity=Decimal("2"), price=Decimal("100")))
        assert order.status == "FILLED"
        assert broker.portfolio().cash == Decimal("800")
        assert broker.portfolio().positions["AAA"] == Decimal("2")
        broker._db.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_insufficient_cash_rejects_order() -> None:
    db_path = Path(__file__).parent / f".paper-{uuid4().hex}.sqlite3"
    try:
        broker = PaperBroker(Decimal("100"), Decimal("0"), str(db_path))
        order = broker.place(OrderRequest(symbol="AAA", side="BUY", quantity=Decimal("2"), price=Decimal("100")))
        assert order.status == "REJECTED"
        broker._db.close()
    finally:
        db_path.unlink(missing_ok=True)
