import asyncio
import time
from typing import Any

import httpx

from app.config import Settings, settings


class TossApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, request_id: str | None = None,
                 error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.error_code = error_code

    @property
    def is_ip_not_allowed(self) -> bool:
        normalized = str(self).lower().replace("_", " ").replace("-", " ")
        return self.error_code == "IP_NOT_ALLOWED" or "ip address not allowed" in normalized


class TossInvestClient:
    """조회 전용 토스증권 Open API 클라이언트. 주문 메서드는 의도적으로 없다."""

    def __init__(self, config: Settings = settings, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._kr_calendar: dict | None = None
        self._kr_calendar_expires_at = 0.0

    def clear_session(self) -> None:
        """네트워크 변경 후 기존 OAuth/시장 캐시를 폐기한다."""
        self._token = None
        self._token_expires_at = 0.0
        self._kr_calendar = None
        self._kr_calendar_expires_at = 0.0

    async def reconnect(self) -> list[dict]:
        """새 토큰으로 계좌 조회까지 성공해야 연결 복구로 간주한다."""
        self.clear_session()
        return await self.accounts()

    async def _access_token(self) -> str:
        if not self.config.toss_configured:
            raise TossApiError(".env에 TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 입력하세요.", 400)
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token
            async with httpx.AsyncClient(base_url=self.config.toss_api_base_url,
                                           transport=self._transport, timeout=10.0) as client:
                response = await client.post("/oauth2/token", data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.toss_client_id,
                    "client_secret": self.config.toss_client_secret,
                })
            data = self._decode(response)
            self._token = data["access_token"]
            self._token_expires_at = time.monotonic() + int(data.get("expires_in", 86400))
            return self._token

    async def _get(self, path: str, *, params: dict | None = None,
                   account_seq: str | int | None = None) -> Any:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account_seq is not None:
            headers["X-Tossinvest-Account"] = str(account_seq)
        async with httpx.AsyncClient(base_url=self.config.toss_api_base_url,
                                     transport=self._transport, timeout=10.0) as client:
            response = await client.get(path, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = min(float(response.headers.get("Retry-After", "1")), 5.0)
                await asyncio.sleep(retry_after)
                response = await client.get(path, params=params, headers=headers)
        if response.status_code in {401, 403}:
            self.clear_session()
        return self._decode(response).get("result")

    @staticmethod
    def _decode(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise TossApiError("토스 API가 올바르지 않은 응답을 반환했습니다.", response.status_code) from exc
        if response.is_success:
            if not isinstance(data, dict):
                raise TossApiError("토스 API 응답 형식을 해석할 수 없습니다.", 502)
            return data

        # OAuth 오류의 error 필드는 객체 또는 문자열로 반환될 수 있다.
        error = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(error, dict):
            message = (error.get("message") or error.get("error_description")
                       or error.get("error") or "토스 API 요청에 실패했습니다.")
            request_id = error.get("requestId") or error.get("request_id")
            error_code = error.get("code") or error.get("errorCode")
        elif isinstance(error, str):
            description = data.get("error_description") if isinstance(data, dict) else None
            message = (description or error).strip() or "토스 API 요청에 실패했습니다."
            request_id = None
            error_code = error
        else:
            message = "토스 API 요청에 실패했습니다."
            request_id = None
            error_code = None

        normalized = message.lower().replace("_", " ").replace("-", " ")
        if "ip address not allowed" in normalized:
            message = "IP address not allowed\nWTS 허용 IP를 확인하세요."
            error_code = "IP_NOT_ALLOWED"
        elif response.status_code == 403:
            message += "\nWTS에서 현재 서버의 허용 IP를 확인하세요."
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "잠시")
            message += f"\n{retry}초 후 다시 시도하세요."
        raise TossApiError(message, response.status_code, request_id, error_code)

    async def accounts(self) -> list[dict]:
        return await self._get("/api/v1/accounts")

    async def resolve_account_seq(self) -> str | int:
        if self.config.toss_account_seq:
            return self.config.toss_account_seq
        accounts = await self.accounts()
        if not accounts:
            raise TossApiError("사용 가능한 종합매매 계좌가 없습니다.", 404)
        return accounts[0]["accountSeq"]

    async def holdings(self, symbol: str | None = None) -> dict:
        account_seq = await self.resolve_account_seq()
        params = {"symbol": symbol} if symbol else None
        return await self._get("/api/v1/holdings", params=params, account_seq=account_seq)

    async def prices(self, symbols: list[str]) -> list[dict]:
        clean = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if not 1 <= len(clean) <= 200:
            raise TossApiError("종목코드는 1~200개까지 조회할 수 있습니다.", 400)
        return await self._get("/api/v1/prices", params={"symbols": ",".join(clean)})

    async def candles(self, symbol: str, count: int = 30) -> list[dict]:
        if not 20 <= count <= 200:
            raise TossApiError("일봉은 20~200개까지 조회할 수 있습니다.", 400)
        result = await self._get("/api/v1/candles", params={
            "symbol": symbol.strip().upper(),
            "interval": "1d",
            "count": count,
        })
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("candles", "records", "items"):
                if isinstance(result.get(key), list):
                    return result[key]
        raise TossApiError(f"{symbol} 일봉 응답 형식을 해석할 수 없습니다.", 502)

    async def list_stocks(self, market: str) -> list[dict]:
        market = market.upper()
        if market not in {"KOSPI", "KOSDAQ"}:
            raise TossApiError("국내 시장은 KOSPI 또는 KOSDAQ만 지원합니다.", 400)
        return await self._get("/api/v1/stocks/all", params={
            "market": market, "status": "ACTIVE", "securityType": "STOCK", "commonShare": "true"
        })

    async def stock_infos(self, symbols: list[str]) -> list[dict]:
        clean = list(dict.fromkeys(symbols))
        if not 1 <= len(clean) <= 200:
            raise TossApiError("종목 상세정보는 1~200개까지 조회할 수 있습니다.", 400)
        return await self._get("/api/v1/stocks", params={"symbols": ",".join(clean)})

    async def warnings(self, symbol: str) -> list[dict]:
        return await self._get(f"/api/v1/stocks/{symbol}/warnings")

    async def rankings(self, count: int = 100) -> dict:
        return await self._get("/api/v1/rankings", params={
            "type": "MARKET_TRADING_AMOUNT",
            "marketCountry": "KR",
            "duration": "realtime",
            "excludeInvestmentCaution": "true",
            "count": min(max(count, 1), 100),
        })

    async def market_calendar_kr(self, date: str | None = None) -> dict:
        if date is None and self._kr_calendar and time.monotonic() < self._kr_calendar_expires_at:
            return self._kr_calendar
        params = {"date": date} if date else None
        result = await self._get("/api/v1/market-calendar/KR", params=params)
        if not isinstance(result, dict):
            raise TossApiError("국내 장 운영정보 응답을 해석할 수 없습니다.", 502)
        if date is None:
            self._kr_calendar = result
            self._kr_calendar_expires_at = time.monotonic() + 300
        return result
