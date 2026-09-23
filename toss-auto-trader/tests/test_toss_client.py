import asyncio
import json
import unittest

try:
    import httpx
except ImportError:  # 설치 전 소스 검사 환경에서는 건너뛴다.
    httpx = None

if httpx:
    from app.config import Settings
    from app.toss.client import TossApiError, TossInvestClient


@unittest.skipUnless(httpx, "httpx dependency is not installed")
class TossClientTests(unittest.TestCase):
    def test_token_is_reused_and_headers_are_sent(self):
        calls = {"token": 0}

        def handler(request):
            if request.url.path == "/oauth2/token":
                calls["token"] += 1
                return httpx.Response(200, json={"access_token": "test-token", "expires_in": 86400})
            self.assertEqual("Bearer test-token", request.headers["Authorization"])
            if request.url.path == "/api/v1/accounts":
                return httpx.Response(200, json={"result": [{"accountSeq": 1}]})
            self.assertEqual("1", request.headers["X-Tossinvest-Account"])
            return httpx.Response(200, json={"result": {"items": []}})

        config = Settings(toss_client_id="id", toss_client_secret="secret")
        client = TossInvestClient(config, httpx.MockTransport(handler))
        asyncio.run(client.accounts())
        asyncio.run(client.holdings())
        self.assertEqual(1, calls["token"])

    def test_string_ip_error_has_wts_guidance(self):
        response = httpx.Response(
            403,
            json={"error": "IP address not allowed"},
            request=httpx.Request("POST", "https://openapi.tossinvest.com/oauth2/token"),
        )

        with self.assertRaises(TossApiError) as caught:
            TossInvestClient._decode(response)

        self.assertEqual(403, caught.exception.status_code)
        self.assertTrue(caught.exception.is_ip_not_allowed)
        self.assertEqual(
            "IP address not allowed\nWTS 허용 IP를 확인하세요.",
            str(caught.exception),
        )

    def test_oauth_ip_error_description_has_wts_guidance(self):
        response = httpx.Response(
            403,
            json={"error": "access_denied", "error_description": "IP address not allowed"},
            request=httpx.Request("POST", "https://openapi.tossinvest.com/oauth2/token"),
        )

        with self.assertRaises(TossApiError) as caught:
            TossInvestClient._decode(response)

        self.assertEqual("IP address not allowed\nWTS 허용 IP를 확인하세요.", str(caught.exception))
        self.assertTrue(caught.exception.is_ip_not_allowed)

    def test_forbidden_response_discards_cached_token(self):
        def handler(request):
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={"access_token": "old-token", "expires_in": 86400})
            return httpx.Response(403, json={"error": "IP address not allowed"})

        client = TossInvestClient(Settings(toss_client_id="id", toss_client_secret="secret"),
                                  httpx.MockTransport(handler))
        with self.assertRaises(TossApiError):
            asyncio.run(client.accounts())
        self.assertIsNone(client._token)

    def test_reconnect_forces_a_new_token(self):
        calls = {"token": 0}

        def handler(request):
            if request.url.path == "/oauth2/token":
                calls["token"] += 1
                return httpx.Response(200, json={"access_token": f"token-{calls['token']}",
                                                 "expires_in": 86400})
            return httpx.Response(200, json={"result": [{"accountSeq": 1}]})

        client = TossInvestClient(Settings(toss_client_id="id", toss_client_secret="secret"),
                                  httpx.MockTransport(handler))
        asyncio.run(client.accounts())
        asyncio.run(client.reconnect())
        self.assertEqual(2, calls["token"])

    def test_object_error_preserves_message_and_request_id(self):
        response = httpx.Response(
            400,
            json={"error": {"message": "invalid request", "requestId": "req-123"}},
            request=httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/prices"),
        )

        with self.assertRaises(TossApiError) as caught:
            TossInvestClient._decode(response)

        self.assertEqual("invalid request", str(caught.exception))
        self.assertEqual("req-123", caught.exception.request_id)


if __name__ == "__main__":
    unittest.main()
