import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.database import db, initialize_database
from app.performance_history import cash_from_trades, ensure_daily_performance, save_daily_performance


KST = timezone(timedelta(hours=9))


class FakeClient:
    async def prices(self, symbols):
        return [{"symbol": symbol, "lastPrice": "12000"} for symbol in symbols]

    async def candles(self, symbol, count):
        yesterday = (datetime.now(KST).date() - timedelta(days=1)).isoformat()
        return [{"timestamp": f"{yesterday}T00:00:00+09:00", "closePrice": "11000"}]


class PerformanceHistoryTests(unittest.TestCase):
    def test_cash_is_reconstructed_from_trade_ledger(self):
        trades = [
            {"side": "BUY", "price": 10000, "quantity": 2, "fee": 100},
            {"side": "SELL", "price": 12000, "quantity": 1, "fee": 50, "tax": 100},
        ]
        self.assertEqual(991750, cash_from_trades(trades, 1_000_000))

    def test_missing_yesterday_is_created_and_today_is_upserted(self):
        with TemporaryDirectory() as directory:
            config = Settings(database_path=f"{directory}/test.db", paper_initial_cash=1_000_000)
            yesterday = (datetime.now(KST).date() - timedelta(days=1)).isoformat()
            with patch("app.database.settings", config):
                initialize_database()
                with db() as conn:
                    conn.execute(
                        """INSERT INTO paper_trades
                           (symbol,name,side,quantity,price,fee,created_at)
                           VALUES('005930','삼성전자','BUY',2,10000,0,?)""",
                        (f"{yesterday} 01:00:00",),
                    )
                asyncio.run(ensure_daily_performance(FakeClient(), config))
                asyncio.run(ensure_daily_performance(FakeClient(), config))
                with db() as conn:
                    rows = [dict(row) for row in conn.execute(
                        "SELECT * FROM daily_performance ORDER BY performance_date")]
                self.assertEqual(2, len(rows))
                self.assertEqual(yesterday, rows[0]["performance_date"])
                self.assertEqual(1, rows[0]["is_final"])
                self.assertEqual(1_002_000, rows[0]["equity"])
                self.assertEqual(1, rows[1]["position_count"])


if __name__ == "__main__":
    unittest.main()
