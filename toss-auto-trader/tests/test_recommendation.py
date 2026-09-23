import asyncio
import unittest

import app.recommendation as module


def candles():
    return [
        {"timestamp": f"2026-07-{day:02d}", "closePrice": str(1000 + day * 10), "volume": "2000000"}
        for day in range(1, 31)
    ]


class FakeClient:
    async def list_stocks(self, market):
        return [
            {"symbol": "111111", "name": "안전종목"},
            {"symbol": "222222", "name": "경고종목"},
        ] if market == "KOSPI" else []

    async def rankings(self, count):
        return {"rankedAt": "2026-08-28T10:00:00+09:00", "rankings": [
            {"symbol": "111111", "price": {"lastPrice": "5000"}, "tradingAmount": "2000000000"},
            {"symbol": "222222", "price": {"lastPrice": "4000"}, "tradingAmount": "2000000000"},
        ]}

    async def stock_infos(self, symbols):
        return [{"symbol": symbol, "status": "ACTIVE", "securityType": "STOCK",
                 "isCommonShare": True, "koreanMarketDetail": {"liquidationTrading": False,
                 "krxTradingSuspended": False, "nxtTradingSuspended": False}} for symbol in symbols]

    async def warnings(self, symbol):
        return [{"warningType": "INVESTMENT_WARNING"}] if symbol == "222222" else []

    async def candles(self, symbol, count):
        return candles()


class UniverseFilterTests(unittest.TestCase):
    def test_warning_stock_is_removed(self):
        module._universe_cache = None
        stocks, warnings, _, stats = asyncio.run(module.load_live_stocks(FakeClient(), 100_000))
        self.assertEqual(["111111"], [stock.symbol for stock in stocks])
        self.assertEqual({"universe": 2, "affordable": 2, "safe": 1, "analyzed": 1}, stats)
        self.assertEqual([], warnings)

    def test_stock_name_map_uses_universe_cache(self):
        module._universe_cache = None
        names = asyncio.run(module.load_stock_name_map(FakeClient(), ["111111", "999999"]))
        self.assertEqual({"111111": "안전종목"}, names)


if __name__ == "__main__":
    unittest.main()
