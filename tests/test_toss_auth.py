import asyncio

import httpx

from auto_trader.toss_client import TossClient, safe_api_error


def test_read_refreshes_revoked_token_once(monkeypatch):
    toss = TossClient()
    toss._cached_token = "stale"
    seen = []

    async def token():
        if not toss._cached_token:
            toss._cached_token = "fresh"
        return toss._cached_token

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, *, params, headers):
            seen.append(headers["Authorization"])
            if seen[-1] == "Bearer stale":
                return httpx.Response(401, json={"error": {"code": "token-revoked"}})
            return httpx.Response(200, json={"result": [{"symbol": "000660", "lastPrice": "1000", "currency": "KRW"}]})

    monkeypatch.setattr(toss, "_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeHttp())

    prices = asyncio.run(toss.prices(["000660"]))
    assert seen == ["Bearer stale", "Bearer fresh"]
    assert prices[0].symbol == "000660"


def test_read_does_not_retry_unrelated_unauthorized(monkeypatch):
    toss = TossClient()
    toss._cached_token = "stale"
    seen = []

    async def token():
        return toss._cached_token

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, *, params, headers):
            seen.append(headers["Authorization"])
            return httpx.Response(401, json={"error": {"code": "login-user-not-found"}})

    monkeypatch.setattr(toss, "_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeHttp())

    response = asyncio.run(toss._get("/api/v1/prices", params={"symbols": "000660"}))
    assert response.status_code == 401
    assert seen == ["Bearer stale"]


def test_forbidden_does_not_refresh_and_error_omits_response_message(monkeypatch):
    toss = TossClient()
    toss._cached_token = "stale"
    seen = []

    async def token():
        return toss._cached_token

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, *, params, headers):
            seen.append(headers["Authorization"])
            return httpx.Response(403, json={"error": {"code": "forbidden", "message": "sensitive-value"}})

    monkeypatch.setattr(toss, "_token", token)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeHttp())

    response = asyncio.run(toss._get("/api/v1/prices", params={"symbols": "000660"}))
    assert response.status_code == 403
    assert seen == ["Bearer stale"]
    assert safe_api_error(response) == "HTTP 403 (forbidden)"
