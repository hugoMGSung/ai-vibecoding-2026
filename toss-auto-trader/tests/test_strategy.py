import unittest

from app.strategy import calculate_rsi, recommend, stock_from_candles


class RecommendationTests(unittest.TestCase):
    def test_budget_and_warning_filters(self):
        items = recommend(300_000)
        self.assertEqual(5, len(items))
        self.assertTrue(all(item["price"] <= 90_000 for item in items))
        self.assertTrue(all(not item["warning"] for item in items))

    def test_small_budget_has_no_expensive_stocks(self):
        items = recommend(50_000)
        self.assertTrue(all(item["price"] <= 15_000 for item in items))

    def test_indicators_are_calculated_from_candles(self):
        candles = [
            {"timestamp": f"2026-08-{day:02d}", "close": str(1000 + day * 10), "volume": str(10000 + day * 100)}
            for day in range(1, 31)
        ]
        stock = stock_from_candles("123456", "테스트", 1300, candles)
        self.assertAlmostEqual(1280, stock.ma5)
        self.assertAlmostEqual(1205, stock.ma20)
        self.assertEqual(100, stock.rsi14)
        self.assertGreater(stock.volume_ratio, 1)

    def test_flat_prices_have_neutral_rsi(self):
        self.assertEqual(50, calculate_rsi([100] * 15))


if __name__ == "__main__":
    unittest.main()
