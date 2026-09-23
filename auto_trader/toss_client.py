from typing import Any

import httpx

from .models import Candle, Price
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
