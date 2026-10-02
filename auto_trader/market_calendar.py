from datetime import datetime
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def allows_regular_order(calendar_day: dict, side: str, now: datetime) -> bool:
    """Fail closed unless the official calendar provides today's regular session."""
    if now.tzinfo is None or not isinstance(calendar_day, dict):
        return False
    local_now = now.astimezone(SEOUL)
    if calendar_day.get("date") != local_now.date().isoformat():
        return False
    integrated = calendar_day.get("integrated")
    session = integrated.get("regularMarket") if isinstance(integrated, dict) else None
    if not isinstance(session, dict):
        return False
    try:
        start = datetime.fromisoformat(session["startTime"])
        end = datetime.fromisoformat(session["endTime"])
        auction_start = session.get("singlePriceAuctionStartTime")
        buy_end = datetime.fromisoformat(auction_start) if auction_start else end
    except (KeyError, TypeError, ValueError):
        return False
    if any(value.tzinfo is None for value in (start, end, buy_end)):
        return False
    start, end, buy_end = (value.astimezone(SEOUL) for value in (start, end, buy_end))
    if not (start < end and start <= buy_end <= end):
        return False
    if side == "BUY":
        return start <= local_now < min(end, buy_end)
    if side == "SELL":
        return start <= local_now < end
    return False
