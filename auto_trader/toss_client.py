from typing import Any
import asyncio
from datetime import datetime
from decimal import Decimal
from time import monotonic
from urllib.parse import quote

import httpx

from .models import Candle, Order, OrderRequest, Price
from .settings import settings


class TossApiError(RuntimeError):
    pass


def safe_api_error(response: httpx.Response) -> str:
    """Report the provider error code without echoing response bodies or credentials."""
    try:
        error = response.json().get("error")
        code = error.get("code") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        code = None
    safe_code = code if isinstance(code, str) and len(code) <= 64 and all(c.isascii() and (c.isalnum() or c in "_-") for c in code) else None
    return f"HTTP {response.status_code}" + (f" ({safe_code})" if safe_code else "")


class TossClient:
    def __init__(self) -> None:
        self._cached_token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _token(self) -> str:
        if not settings.toss_client_id or not settings.toss_client_secret:
            raise TossApiError("TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET이 설정되지 않았습니다.")
        async with self._token_lock:
            if self._cached_token and monotonic() < self._token_expires_at:
                return self._cached_token
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{settings.toss_api_base}/oauth2/token",
                    data={"grant_type": "client_credentials", "client_id": settings.toss_client_id, "client_secret": settings.toss_client_secret},
                )
            if response.is_error:
                detail = safe_api_error(response)
                if response.status_code == 403:
                    detail += " · 토스증권에 등록한 허용 IP를 확인하세요."
                raise TossApiError(f"토큰 발급 실패: {detail}")
            body = response.json()
            self._cached_token = body["access_token"]
            # Toss issues a single valid token per client; cache it and refresh
            # shortly before expiry so concurrent calls do not revoke each other.
            self._token_expires_at = monotonic() + max(0, int(body.get("expires_in", 86400)) - 60)
            return self._cached_token

    async def _get(self, path: str, *, params: dict[str, str | int] | None = None, account: bool = False) -> httpx.Response:
        """Retry a read once when another client has replaced our cached token."""
        token = await self._token()
        async with httpx.AsyncClient(timeout=10) as client:
            for attempt in range(2):
                headers = {"Authorization": f"Bearer {token}"}
                if account:
                    headers["X-Tossinvest-Account"] = settings.toss_account_seq
                response = await client.get(f"{settings.toss_api_base}{path}", params=params, headers=headers)
                if response.status_code != 401 or attempt:
                    return response
                try:
                    code = response.json().get("error", {}).get("code")
                except ValueError:
                    code = None
                if code not in {"expired-token", "token-revoked"}:
                    return response
                async with self._token_lock:
                    if self._cached_token == token:
                        self._cached_token = ""
                        self._token_expires_at = 0.0
                token = await self._token()
        return response

    async def accounts(self) -> list[dict[str, Any]]:
        response = await self._get("/api/v1/accounts")
        if response.is_error:
            raise TossApiError(f"계좌 조회 실패: {safe_api_error(response)}")
        body = response.json()
        return body.get("result", body if isinstance(body, list) else [])

    async def holdings(self) -> dict[str, Any]:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        response = await self._get("/api/v1/holdings", account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 보유자산 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise TossApiError("실계좌 보유자산 응답 형식이 예상과 다릅니다.")
        return result

    async def buying_power(self, currency: str = "KRW") -> Decimal:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        response = await self._get("/api/v1/buying-power", params={"currency": currency}, account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 매수 가능 금액 조회 실패: {safe_api_error(response)}")
        try:
            return Decimal(response.json()["result"]["cashBuyingPower"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError(f"실계좌 {currency} 매수 가능 금액 응답이 올바르지 않습니다.") from exc

    async def open_orders(self) -> list[dict[str, Any]]:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        response = await self._get("/api/v1/orders", params={"status": "OPEN"}, account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 미체결 주문 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result", {})
        orders = result.get("orders") if isinstance(result, dict) else result
        if not isinstance(orders, list):
            raise TossApiError("실계좌 미체결 주문 응답 형식이 예상과 다릅니다.")
        if isinstance(result, dict) and result.get("hasNext"):
            raise TossApiError("실계좌 미체결 주문이 여러 페이지라 전체 주문을 확인할 수 없습니다.")
        return orders

    async def order_detail(self, order_id: str) -> dict[str, Any]:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        if not order_id or len(order_id) > 200:
            raise TossApiError("주문 식별자가 올바르지 않습니다.")
        response = await self._get(f"/api/v1/orders/{quote(order_id, safe='')}", account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 주문 상세 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, dict) or result.get("orderId") != order_id or not isinstance(result.get("execution"), dict):
            raise TossApiError("실계좌 주문 상세 응답이 예상과 다릅니다.")
        return result

    async def cancel_order(self, order_id: str) -> str:
        if not settings.toss_account_seq or not order_id or len(order_id) > 200:
            raise TossApiError("취소할 실계좌 주문 ID와 계좌 설정이 필요합니다.")
        token = await self._token()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{settings.toss_api_base}/api/v1/orders/{quote(order_id, safe='')}/cancel",
                                         headers={"Authorization": f"Bearer {token}", "X-Tossinvest-Account": settings.toss_account_seq})
        if response.is_error:
            raise TossApiError(f"실계좌 주문 취소 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, dict) or not isinstance(result.get("orderId"), str):
            raise TossApiError("실계좌 주문 취소 응답을 확인할 수 없습니다.")
        return result["orderId"]

    async def commissions(self) -> list[dict[str, Any]]:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        response = await self._get("/api/v1/commissions", account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 수수료 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, list):
            raise TossApiError("실계좌 수수료 응답이 예상과 다릅니다.")
        return result

    async def price_limits(self, symbol: str) -> dict[str, Any]:
        response = await self._get("/api/v1/price-limits", params={"symbol": symbol})
        if response.is_error:
            raise TossApiError(f"가격 제한폭 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, dict):
            raise TossApiError("가격 제한폭 응답이 예상과 다릅니다.")
        return result

    async def stock_details(self, symbol: str) -> dict[str, Any]:
        response = await self._get("/api/v1/stocks", params={"symbols": symbol})
        if response.is_error:
            raise TossApiError(f"종목 정보 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, list) or len(result) != 1 or result[0].get("symbol") != symbol:
            raise TossApiError("종목 정보 응답이 예상과 다릅니다.")
        return result[0]

    async def stock_warnings(self, symbol: str) -> list[dict[str, Any]]:
        response = await self._get(f"/api/v1/stocks/{quote(symbol, safe='')}/warnings")
        if response.is_error:
            raise TossApiError(f"종목 유의사항 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result")
        if not isinstance(result, list):
            raise TossApiError("종목 유의사항 응답이 예상과 다릅니다.")
        return result

    async def exchange_rate_usd_krw(self) -> Decimal:
        response = await self._get("/api/v1/exchange-rate", params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        if response.is_error:
            raise TossApiError(f"USD/KRW 환율 조회 실패: {safe_api_error(response)}")
        try:
            result = response.json()["result"]
            valid_until = datetime.fromisoformat(result["validUntil"])
            if valid_until <= datetime.now(valid_until.tzinfo):
                raise ValueError("expired exchange rate")
            rate = Decimal(result["rate"])
            if not rate.is_finite() or rate <= 0:
                raise ValueError("invalid exchange rate")
            return rate
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError("USD/KRW 환율이 없거나 유효 시간이 지났습니다.") from exc

    async def market_calendar_kr(self, day: str) -> dict[str, Any]:
        response = await self._get("/api/v1/market-calendar/KR", params={"date": day})
        if response.is_error:
            raise TossApiError(f"국내 장 운영정보 조회 실패: {safe_api_error(response)}")
        result = response.json().get("result", {})
        today = result.get("today") if isinstance(result, dict) else None
        if not isinstance(today, dict) or today.get("date") != day:
            raise TossApiError("국내 장 운영정보 응답 날짜가 요청 날짜와 일치하지 않습니다.")
        return today

    async def sellable_quantity(self, symbol: str) -> Decimal:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        response = await self._get("/api/v1/sellable-quantity", params={"symbol": symbol}, account=True)
        if response.is_error:
            raise TossApiError(f"실계좌 매도 가능 수량 조회 실패: {safe_api_error(response)}")
        try:
            return Decimal(response.json()["result"]["sellableQuantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TossApiError("실계좌 매도 가능 수량 응답이 올바르지 않습니다.") from exc

    async def place_order(self, request: OrderRequest) -> Order:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        if not request.client_order_id:
            raise TossApiError("실거래 주문에는 고유한 client_order_id가 필요합니다.")
        token = await self._token()
        # Retries must reuse exactly the same id: Toss treats clientOrderId as an
        # idempotency key for 10 minutes and returns the original result.
        client_order_id = request.client_order_id
        payload = {"clientOrderId": client_order_id, "symbol": request.symbol, "side": request.side, "orderType": "LIMIT", "quantity": str(request.quantity), "price": str(request.price), "confirmHighValueOrder": False}
        headers = {"Authorization": f"Bearer {token}", "X-Tossinvest-Account": settings.toss_account_seq, "Content-Type": "application/json"}
        max_retries = max(0, min(settings.live_order_max_retries, 5))
        response = None
        async with httpx.AsyncClient(timeout=15) as client:
            for attempt in range(max_retries + 1):
                try:
                    response = await client.post(f"{settings.toss_api_base}/api/v1/orders", json=payload, headers=headers)
                    break
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    if attempt >= max_retries:
                        raise TossApiError(f"실거래 주문 전송 실패(재시도 {max_retries}회 소진): {exc}") from exc
                    delay = float(settings.live_order_retry_backoff_seconds) * (2 ** attempt)
                    await asyncio.sleep(min(delay, 30.0))
        assert response is not None
        if response.is_error:
            raise TossApiError(f"실거래 주문 실패: {safe_api_error(response)}")
        body = response.json()
        data = body.get("result", body)
        order_id = data.get("orderId")
        if not isinstance(order_id, str) or not order_id or data.get("clientOrderId") != client_order_id:
            raise TossApiError("실거래 주문 응답의 주문 ID 또는 멱등키를 확인할 수 없습니다.")
        return Order(id=0, client_order_id=client_order_id, external_order_id=order_id, symbol=request.symbol,
                     side=request.side, quantity=request.quantity, price=request.price, status="PENDING", mode="LIVE")

    async def prices(self, symbols: list[str]) -> list[Price]:
        response = await self._get("/api/v1/prices", params={"symbols": ",".join(symbols)})
        if response.is_error:
            raise TossApiError(f"현재가 조회 실패: {safe_api_error(response)}")
        result: list[dict[str, Any]] = response.json().get("result", [])
        return [Price(symbol=item["symbol"], price=item["lastPrice"], currency=item.get("currency", "KRW"), timestamp=item.get("timestamp")) for item in result]

    async def stock_names(self, symbols: list[str]) -> dict[str, str]:
        response = await self._get("/api/v1/stocks", params={"symbols": ",".join(symbols)})
        if response.is_error:
            raise TossApiError(f"종목 정보 조회 실패: {safe_api_error(response)}")
        result: list[dict[str, Any]] = response.json().get("result", [])
        return {item["symbol"]: item.get("name", item.get("stockName", "")) for item in result}

    async def candles(self, symbol: str, count: int = 60) -> list[Candle]:
        response = await self._get("/api/v1/candles", params={"symbol": symbol, "interval": "1d", "count": count})
        if response.is_error:
            raise TossApiError(f"캔들 조회 실패: {safe_api_error(response)}")
        result: list[dict[str, Any]] = response.json().get("result", {}).get("candles", [])
        return [Candle(timestamp=item["timestamp"], open=item.get("open", item.get("openPrice")), high=item.get("high", item.get("highPrice")), low=item.get("low", item.get("lowPrice")), close=item.get("close", item.get("closePrice")), volume=item.get("volume", "0")) for item in result]
