from typing import Any
import asyncio
import hashlib
from datetime import datetime, timezone

import httpx

from .models import Candle, Order, OrderRequest, Price
from .settings import settings


class TossApiError(RuntimeError):
    pass


class TossClient:
    async def _token(self) -> str:
        if not settings.toss_client_id or not settings.toss_client_secret:
            raise TossApiError("TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET이 설정되지 않았습니다.")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.toss_api_base}/oauth2/token",
                data={"grant_type": "client_credentials", "client_id": settings.toss_client_id, "client_secret": settings.toss_client_secret},
            )
        if response.is_error:
            detail = response.text[:300].replace("\n", " ")
            if response.status_code == 403:
                detail = "토스증권 Open API 허용 IP에 현재 서버 공인 IP가 등록되지 않았을 수 있습니다. " + detail
            raise TossApiError(f"토큰 발급 실패: HTTP {response.status_code} - {detail}")
        return response.json()["access_token"]

    async def accounts(self) -> list[dict[str, Any]]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{settings.toss_api_base}/api/v1/accounts", headers={"Authorization": f"Bearer {token}"})
        if response.is_error:
            raise TossApiError(f"계좌 조회 실패: HTTP {response.status_code} - {response.text[:300]}")
        body = response.json()
        return body.get("result", body if isinstance(body, list) else [])

    async def place_order(self, request: OrderRequest) -> Order:
        if not settings.toss_account_seq:
            raise TossApiError("TOSS_ACCOUNT_SEQ가 설정되지 않았습니다.")
        token = await self._token()
        # Retries must reuse exactly the same id: Toss treats clientOrderId as an
        # idempotency key for 10 minutes and returns the original result.
        if request.client_order_id:
            client_order_id = request.client_order_id
        else:
            # Keep an implicit key stable for the Toss idempotency window so a
            # repeated HTTP request cannot create a second identical order.
            bucket = int(datetime.now(timezone.utc).timestamp() // 600)
            fingerprint = f"{request.symbol}|{request.side}|{request.quantity}|{request.price}|{bucket}"
            client_order_id = f"live-{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"
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
            raise TossApiError(f"실거래 주문 실패: HTTP {response.status_code} - {response.text[:500].replace(chr(10), ' ')}")
        body = response.json()
        data = body.get("result", body)
        order_id = data.get("orderId", data.get("id", client_order_id))
        status = str(data.get("status", data.get("orderStatus", "SUBMITTED"))).upper()
        if status not in {"SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "REJECTED"}:
            status = "SUBMITTED"
        return Order(id=0, client_order_id=str(order_id), symbol=request.symbol, side=request.side, quantity=request.quantity, price=request.price, status=status, mode="LIVE")

    async def prices(self, symbols: list[str]) -> list[Price]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{settings.toss_api_base}/api/v1/prices",
                params={"symbols": ",".join(symbols)},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.is_error:
            raise TossApiError(f"현재가 조회 실패: HTTP {response.status_code}")
        result: list[dict[str, Any]] = response.json().get("result", [])
        return [Price(symbol=item["symbol"], price=item["lastPrice"], currency=item.get("currency", "KRW")) for item in result]

    async def stock_names(self, symbols: list[str]) -> dict[str, str]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{settings.toss_api_base}/api/v1/stocks",
                params={"symbols": ",".join(symbols)},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.is_error:
            raise TossApiError(f"종목 정보 조회 실패: HTTP {response.status_code}")
        result: list[dict[str, Any]] = response.json().get("result", [])
        return {item["symbol"]: item.get("name", item.get("stockName", "")) for item in result}

    async def candles(self, symbol: str, count: int = 60) -> list[Candle]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{settings.toss_api_base}/api/v1/candles",
                params={"symbol": symbol, "interval": "1d", "count": count},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.is_error:
            raise TossApiError(f"캔들 조회 실패: HTTP {response.status_code}")
        result: list[dict[str, Any]] = response.json().get("result", {}).get("candles", [])
        return [Candle(timestamp=item["timestamp"], open=item.get("open", item.get("openPrice")), high=item.get("high", item.get("highPrice")), low=item.get("low", item.get("lowPrice")), close=item.get("close", item.get("closePrice")), volume=item.get("volume", "0")) for item in result]
