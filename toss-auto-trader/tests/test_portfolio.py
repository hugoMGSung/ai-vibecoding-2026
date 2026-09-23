import unittest

from app.portfolio import build_positions


class PortfolioTests(unittest.TestCase):
    def test_average_cost_survives_partial_sell(self):
        trades = [
            {"id": 1, "symbol": "111111", "name": "테스트", "side": "BUY", "quantity": 10, "price": 1000, "fee": 0},
            {"id": 2, "symbol": "111111", "name": "테스트", "side": "BUY", "quantity": 10, "price": 2000, "fee": 0},
            {"id": 3, "symbol": "111111", "name": "테스트", "side": "SELL", "quantity": 5, "price": 2500, "fee": 0},
        ]
        position = build_positions(trades)["111111"]
        self.assertEqual(15, position["quantity"])
        self.assertEqual(1500, position["avg_price"])

    def test_full_sell_removes_position(self):
        trades = [
            {"id": 1, "symbol": "111111", "name": "테스트", "side": "BUY", "quantity": 2, "price": 1000, "fee": 0},
            {"id": 2, "symbol": "111111", "name": "테스트", "side": "SELL", "quantity": 2, "price": 1100, "fee": 0},
        ]
        self.assertEqual({}, build_positions(trades))


if __name__ == "__main__":
    unittest.main()
