import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.auto_trading import (assess_market_filter, calculate_order_quantity, evaluate_exit, execute_paper_buy,
                              get_automation_config, market_is_regular_open,
                              is_ip_not_allowed_error, update_automation_config)
from app.config import Settings
from app.database import db, initialize_database
from app.strategy import Stock
from app.toss import TossApiError


class AutoTradingTests(unittest.TestCase):
    def test_ip_error_is_detected_immediately(self):
        self.assertTrue(is_ip_not_allowed_error(
            TossApiError("IP address not allowed\nWTS 허용 IP를 확인하세요.", 403, error_code="IP_NOT_ALLOWED")
        ))
        self.assertFalse(is_ip_not_allowed_error(TossApiError("일시 오류", 502)))

    def test_stop_loss_and_take_profit(self):
        self.assertEqual("AUTO_STOP_LOSS", evaluate_exit(10000, 9500, .05, .10))
        self.assertEqual("AUTO_TAKE_PROFIT", evaluate_exit(10000, 11000, .05, .10))
        self.assertIsNone(evaluate_exit(10000, 10300, .05, .10))

    def test_trailing_stop_and_max_holding(self):
        self.assertEqual("AUTO_TRAILING_STOP",
                         evaluate_exit(10000, 11300, .05, .50, 12000, .05, 3, 20))
        self.assertEqual("AUTO_MAX_HOLDING",
                         evaluate_exit(10000, 10100, .05, .50, 10200, .05, 20, 20))

    def test_holiday_is_closed(self):
        self.assertFalse(market_is_regular_open({"today": {"integrated": None}}))

    def test_regular_session_is_open(self):
        calendar = {"today": {"integrated": {"regularMarket": {
            "startTime": "2026-08-28T09:00:00+09:00",
            "endTime": "2026-08-28T15:30:00+09:00",
        }}}}
        now = datetime.fromisoformat("2026-08-28T10:00:00+09:00")
        self.assertTrue(market_is_regular_open(calendar, now))

    def test_order_quantity_respects_all_limits(self):
        self.assertEqual(3, calculate_order_quantity(30000, 200000, 100000, 5))
        self.assertEqual(0, calculate_order_quantity(120000, 200000, 100000, 5))

    def test_market_filter_raises_score_when_market_is_weak(self):
        stocks = [
            Stock("1", "상승", 1100, 1080, 1000, 1.5, 55, 2_000_000_000),
            Stock("2", "하락1", 900, 950, 1000, 1.5, 55, 2_000_000_000),
            Stock("3", "하락2", 880, 930, 1000, 1.5, 55, 2_000_000_000),
        ]
        result = assess_market_filter(stocks, 80)
        self.assertEqual("DEFENSIVE", result["mode"])
        self.assertFalse(result["allow_buy"])

    def test_market_filter_keeps_base_score_when_market_is_good(self):
        stocks = [
            Stock("1", "상승1", 1100, 1080, 1000, 1.5, 55, 2_000_000_000),
            Stock("2", "상승2", 1050, 1030, 1000, 1.5, 55, 2_000_000_000),
            Stock("3", "하락", 990, 995, 1000, 1.5, 55, 2_000_000_000),
        ]
        result = assess_market_filter(stocks, 80)
        self.assertEqual("FAVORABLE", result["mode"])
        self.assertEqual(80, result["min_score"])

    def test_auto_buy_is_saved_and_duplicate_signal_is_blocked(self):
        with TemporaryDirectory() as directory:
            config = Settings(database_path=f"{directory}/test.db", paper_initial_cash=200000,
                              paper_max_positions=3, paper_max_order_amount=100000)
            candidate = {"symbol": "019170", "name": "신풍제약", "score": 80}
            with patch("app.database.settings", config):
                initialize_database()
                result = execute_paper_buy(candidate, 2, 30000, config)
                self.assertEqual(60000, result["amount"])
                with db() as conn:
                    self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM auto_signals").fetchone()[0])
                    self.assertEqual("신풍제약",
                                     conn.execute("SELECT name FROM auto_signals").fetchone()[0])
                    self.assertEqual("AUTO_BUY_SCORE_80",
                                     conn.execute("SELECT reason FROM paper_trades").fetchone()[0])
                with self.assertRaises(ValueError):
                    execute_paper_buy(candidate, 1, 30000, config)

    def test_runtime_config_is_saved(self):
        with TemporaryDirectory() as directory:
            config = Settings(database_path=f"{directory}/test.db")
            values = {"stop_loss_rate": .04, "take_profit_rate": .12,
                      "trailing_stop_rate": .03, "max_holding_days": 15,
                      "min_score": 85, "max_positions": 2,
                      "max_order_amount": 70000, "daily_loss_limit_rate": .02}
            with patch("app.database.settings", config):
                initialize_database()
                saved = update_automation_config(values)
                self.assertEqual(85, saved["min_score"])
                self.assertEqual(70000, saved["max_order_amount"])


if __name__ == "__main__":
    unittest.main()
