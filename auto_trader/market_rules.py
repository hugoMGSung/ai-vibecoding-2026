"""Conservative KRW order preflight using fresh Toss reference data."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .models import OrderRequest, Price


SEOUL = ZoneInfo("Asia/Seoul")


def tick_size(price: Decimal) -> Decimal:
    if price < 2000:
        return Decimal("1")
    if price < 5000:
        return Decimal("5")
    if price < 20000:
        return Decimal("10")
    if price < 50000:
        return Decimal("50")
    if price < 200000:
        return Decimal("100")
    if price < 500000:
        return Decimal("500")
    return Decimal("1000")


def validate_live_market_data(request: OrderRequest, quote: Price, stock: dict, limits: dict,
                              warnings: list[dict], commissions: list[dict], *, now: datetime | None = None,
                              max_quote_age_seconds: int = 30) -> dict:
    now = (now or datetime.now(SEOUL)).astimezone(SEOUL)
    if stock.get("symbol") != request.symbol or stock.get("currency") != "KRW" or stock.get("market") not in {"KOSPI", "KOSDAQ"}:
        raise ValueError("국내 원화 종목 정보가 일치하지 않습니다.")
    if stock.get("status") != "ACTIVE" or stock.get("securityType") != "STOCK" or stock.get("isCommonShare") is not True:
        raise ValueError("거래 가능한 국내 보통주가 아닙니다.")
    detail = stock.get("koreanMarketDetail")
    if not isinstance(detail, dict) or detail.get("liquidationTrading") is not False or detail.get("krxTradingSuspended") is not False:
        raise ValueError("정리매매 또는 거래정지 여부를 안전하게 확인할 수 없습니다.")
    if request.side == "BUY" and warnings:
        raise ValueError("활성 매수 유의사항이 있어 신규 매수를 보류합니다.")
    if quote.symbol != request.symbol or quote.currency != "KRW" or quote.timestamp is None or quote.timestamp.tzinfo is None:
        raise ValueError("현재가·통화·시각이 확인되지 않았습니다.")
    age = now - quote.timestamp.astimezone(SEOUL)
    if age < timedelta(seconds=-5) or age > timedelta(seconds=max_quote_age_seconds):
        raise ValueError("현재가가 오래됐거나 시각이 잘못되었습니다.")
    if request.price % tick_size(request.price):
        raise ValueError("주문가가 국내 호가 단위에 맞지 않습니다.")
    if abs(request.price - quote.price) / quote.price > Decimal("0.02"):
        raise ValueError("주문가와 최신 현재가 차이가 2%를 넘습니다.")
    if limits.get("currency") != "KRW":
        raise ValueError("가격 제한폭 통화가 올바르지 않습니다.")
    try:
        lower, upper = Decimal(str(limits["lowerLimitPrice"])), Decimal(str(limits["upperLimitPrice"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("상·하한가가 확인되지 않았습니다.") from exc
    if not lower.is_finite() or not upper.is_finite() or lower <= 0 or upper < lower or not lower <= request.price <= upper:
        raise ValueError("주문가가 당일 상·하한가 범위 밖입니다.")
    active = [entry for entry in commissions if entry.get("marketCountry") == "KR"
              and (not entry.get("startDate") or entry["startDate"] <= now.date().isoformat())
              and (not entry.get("endDate") or entry["endDate"] >= now.date().isoformat())]
    if len(active) != 1:
        raise ValueError("현재 적용 수수료율을 하나로 확정할 수 없습니다.")
    try:
        rate = Decimal(str(active[0]["commissionRate"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("현재 수수료율이 잘못되었습니다.") from exc
    if not rate.is_finite() or rate < 0 or rate > Decimal("0.05"):
        raise ValueError("현재 수수료율이 잘못되었습니다.")
    amount = request.quantity * request.price
    return {"quote_price": quote.price, "quote_timestamp": quote.timestamp, "lower_limit": lower,
            "upper_limit": upper, "commission_rate": rate, "estimated_commission": amount * rate,
            "tax": "실제 체결 조회값으로만 확정", "amount": amount}
