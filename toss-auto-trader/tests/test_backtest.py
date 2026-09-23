import unittest

from app.backtest import BacktestRules, run_backtest


def candles(prices: list[float]) -> list[dict]:
    return [{
        "timestamp": f"2026-01-{index + 1:02d}T00:00:00+09:00",
        "openPrice": str(price), "highPrice": str(price * 1.01),
        "lowPrice": str(price * .99), "closePrice": str(price), "volume": "1000000",
    } for index, price in enumerate(prices)]


class BacktestTests(unittest.TestCase):
    def test_requires_enough_candles(self):
        with self.assertRaises(ValueError):
            run_backtest(candles([100] * 20), BacktestRules())

    def test_result_contains_performance_metrics(self):
        prices = [100, 101] * 10 + [102, 102, 103, 104, 105, 106, 107, 108, 109, 110,
                                         111, 112, 113, 114, 115]
        result = run_backtest(candles(prices), BacktestRules(initial_cash=100_000))
        self.assertIn("total_return_rate", result)
        self.assertIn("max_drawdown", result)
        self.assertEqual(1, result["trade_count"])
        self.assertEqual("TAKE_PROFIT", result["trades"][0]["reason"])
        self.assertEqual(result["final_equity"] - result["initial_cash"], result["total_pnl"])
        self.assertAlmostEqual(result["total_pnl"] / result["initial_cash"],
                               result["total_return_rate"])


if __name__ == "__main__":
    unittest.main()
