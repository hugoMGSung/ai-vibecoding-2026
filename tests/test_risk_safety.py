from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from auto_trader.market_calendar import allows_regular_order
from auto_trader.risk import RiskGuard


SEOUL = ZoneInfo("Asia/Seoul")


def test_market_calendar_blocks_holidays_and_unknown_payloads():
    now = datetime(2026, 10, 1, 10, 0, tzinfo=SEOUL)
    assert not allows_regular_order({"date": "2026-10-01", "integrated": None}, "BUY", now)
    assert not allows_regular_order({"date": "2026-10-01"}, "SELL", now)


def test_market_calendar_enforces_order_sessions_and_auction_cutoff():
    calendar_day = {
        "date": "2026-10-01",
        "integrated": {
            "regularMarket": {
                "startTime": "2026-10-01T09:00:00+09:00",
                "singlePriceAuctionStartTime": "2026-10-01T15:20:00+09:00",
                "endTime": "2026-10-01T15:30:00+09:00",
            }
        },
    }
    assert allows_regular_order(calendar_day, "BUY", datetime(2026, 10, 1, 9, 0, tzinfo=SEOUL))
    assert not allows_regular_order(calendar_day, "BUY", datetime(2026, 10, 1, 15, 20, tzinfo=SEOUL))
    assert allows_regular_order(calendar_day, "SELL", datetime(2026, 10, 1, 15, 20, tzinfo=SEOUL))
    assert not allows_regular_order(calendar_day, "SELL", datetime(2026, 10, 1, 15, 30, tzinfo=SEOUL))
    assert not allows_regular_order(calendar_day, "BUY", datetime(2026, 10, 2, 10, 0, tzinfo=SEOUL))
    calendar_day["integrated"]["regularMarket"]["startTime"] = "2026-10-01T09:00:00"
    assert not allows_regular_order(calendar_day, "BUY", datetime(2026, 10, 1, 10, 0, tzinfo=SEOUL))


def test_daily_risk_baseline_and_halt_survive_restart():
    db_path = Path(__file__).parent / f".risk-{uuid4().hex}.sqlite3"
    try:
        first = RiskGuard(str(db_path))
        assert first.start_day_if_needed("LIVE:2026-10-01", "1000000")["opening_equity"] == "1000000"
        first.halt_day("LIVE:2026-10-01", "daily loss limit")
        first.close()

        reopened = RiskGuard(str(db_path))
        saved = reopened.daily_state("LIVE:2026-10-01")
        assert saved == {"opening_equity": "1000000", "halted": True, "reason": "daily loss limit"}
        reopened.close()
    finally:
        db_path.unlink(missing_ok=True)
