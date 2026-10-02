import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from auto_trader.live_journal import LiveJournal
from auto_trader.market_rules import validate_live_market_data
from auto_trader.models import OrderRequest, Price
from auto_trader.operations import OperationsStore, SingleInstanceLock
from auto_trader.toss_client import TossClient
from auto_trader.toss_client import TossApiError


def remote_order(status="PARTIAL_FILLED", filled="2", amount="140000", commission="210", tax="0"):
    return {"orderId": "remote-1", "symbol": "005930", "side": "BUY", "currency": "KRW",
            "quantity": "5", "status": status,
            "execution": {"filledQuantity": filled, "averageFilledPrice": "70000" if Decimal(filled) else None,
                          "filledAmount": amount if Decimal(filled) else None,
                          "commission": commission if Decimal(filled) else None,
                          "tax": tax if Decimal(filled) else None, "filledAt": None}}


def test_live_journal_partial_fill_restart_and_regression_block():
    path = Path(__file__).parent / f".live-{uuid4().hex}.db"
    try:
        journal = LiveJournal(str(path))
        journal.record_intent("client-1", "005930", "BUY", Decimal("5"), Decimal("70000"))
        assert journal.unresolved_intents()[0]["order_id"] is None
        assert journal.intent("client-1")["symbol"] == "005930"
        journal.link_order("client-1", "remote-1")
        assert journal.apply_detail(remote_order())["filled_quantity"] == "2"
        journal.close()
        journal = LiveJournal(str(path))
        assert journal.unresolved_intents()[0]["order_id"] == "remote-1"
        with pytest.raises(ValueError, match="감소"):
            journal.apply_detail(remote_order(filled="1", amount="70000", commission="100"))
        assert journal.apply_detail(remote_order("FILLED", "5", "350000", "525"))["commission"] == "525"
        assert journal.unresolved_intents() == []
        assert journal.recent_orders()[0]["filled_quantity"] == "5"
        journal.close()
    finally:
        path.unlink(missing_ok=True)


def test_canceled_order_keeps_partial_fill_and_actual_costs():
    path = Path(__file__).parent / f".cancel-{uuid4().hex}.db"
    try:
        journal = LiveJournal(str(path))
        journal.record_intent("client-2", "005930", "BUY", Decimal("5"), Decimal("70000"))
        journal.link_order("client-2", "remote-1")
        journal.apply_detail(remote_order())
        canceled = journal.apply_detail(remote_order("CANCELED", "2", "140000", "210", "0"))
        assert canceled["filled_quantity"] == "2"
        assert canceled["commission"] == "210"
        assert journal.unresolved_intents() == []
        assert journal.recent_orders()[0]["status"] == "CANCELED"
        journal.close()
    finally:
        path.unlink(missing_ok=True)


def test_market_rules_reject_stale_quote_and_accept_fresh_bounded_order():
    now = datetime(2026, 10, 2, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    request = OrderRequest(symbol="005930", side="BUY", quantity="2", price="70000")
    stock = {"symbol": "005930", "currency": "KRW", "market": "KOSPI", "status": "ACTIVE",
             "securityType": "STOCK", "isCommonShare": True,
             "koreanMarketDetail": {"liquidationTrading": False, "krxTradingSuspended": False}}
    limits = {"currency": "KRW", "lowerLimitPrice": "50000", "upperLimitPrice": "90000"}
    commissions = [{"marketCountry": "KR", "commissionRate": "0.00015", "startDate": "2026-01-01", "endDate": None}]
    fresh = Price(symbol="005930", price="70000", currency="KRW", timestamp=now)
    result = validate_live_market_data(request, fresh, stock, limits, [], commissions, now=now)
    assert result["estimated_commission"] == Decimal("21")
    with pytest.raises(ValueError, match="오래"):
        validate_live_market_data(request, fresh.model_copy(update={"timestamp": now - timedelta(minutes=1)}), stock, limits, [], commissions, now=now)
    with pytest.raises(ValueError, match="유의사항"):
        validate_live_market_data(request, fresh, stock, limits, [{"warningType": "INVESTMENT_RISK"}], commissions, now=now)
    with pytest.raises(ValueError, match="상·하한가"):
        validate_live_market_data(request, fresh, stock, {**limits, "upperLimitPrice": "60000"}, [], commissions, now=now)


def test_operations_restore_and_single_instance_lock():
    path = Path(__file__).parent / f".ops-{uuid4().hex}.db"
    lock_path = Path(__file__).parent / f".ops-{uuid4().hex}.lock"
    try:
        first_lock, second_lock = SingleInstanceLock(str(lock_path)), SingleInstanceLock(str(lock_path))
        first_lock.acquire()
        with pytest.raises(RuntimeError, match="이미 실행"):
            second_lock.acquire()
        first_lock.release()
        second_lock.acquire()
        second_lock.release()

        store = OperationsStore(str(path))
        store.save_auto_state({"running": True, "message": "매매 중", "last_action": "005930"})
        store.close()
        store = OperationsStore(str(path))
        restored = store.restore_stopped()
        assert not restored["running"]
        assert "재시작" in restored["message"]
        assert store.alert_count() >= 1
        store.close()
    finally:
        path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def test_order_detail_and_commission_reader_use_account_header(monkeypatch):
    toss = TossClient()
    seen = []

    async def fake_get(path, *, params=None, account=False):
        seen.append((path, account))
        import httpx
        if path.endswith("/commissions"):
            return httpx.Response(200, json={"result": [{"marketCountry": "KR", "commissionRate": "0.00015"}]})
        return httpx.Response(200, json={"result": remote_order()})

    from auto_trader import toss_client
    monkeypatch.setattr(toss_client.settings, "toss_account_seq", "1")
    monkeypatch.setattr(toss, "_get", fake_get)
    assert asyncio.run(toss.order_detail("remote-1"))["orderId"] == "remote-1"
    assert asyncio.run(toss.commissions())[0]["marketCountry"] == "KR"
    assert seen == [("/api/v1/orders/remote-1", True), ("/api/v1/commissions", True)]


def test_live_submission_preserves_client_id_and_remote_id(monkeypatch):
    import httpx
    from auto_trader import toss_client

    toss = TossClient()
    seen = []

    async def token():
        return "test-token"

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, json, headers):
            seen.append((json["clientOrderId"], headers["X-Tossinvest-Account"]))
            return httpx.Response(200, json={"result": {"orderId": "remote-1", "clientOrderId": "client-1"}})

    monkeypatch.setattr(toss_client.settings, "toss_account_seq", "1")
    monkeypatch.setattr(toss, "_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeHttp())
    order = asyncio.run(toss.place_order(OrderRequest(symbol="005930", side="BUY", quantity="1", price="70000", client_order_id="client-1")))
    assert seen == [("client-1", "1")]
    assert order.client_order_id == "client-1" and order.external_order_id == "remote-1" and order.status == "PENDING"
    with pytest.raises(TossApiError, match="client_order_id"):
        asyncio.run(toss.place_order(OrderRequest(symbol="005930", side="BUY", quantity="1", price="70000")))
