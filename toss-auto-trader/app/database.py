import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings


def initialize_database() -> None:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price INTEGER NOT NULL CHECK(price > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS recommendation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                score INTEGER NOT NULL,
                ma5 REAL NOT NULL,
                ma20 REAL NOT NULL,
                rsi14 REAL NOT NULL,
                volume_ratio REAL NOT NULL,
                reasons TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(recommendation_date, symbol)
            );

            CREATE TABLE IF NOT EXISTS automation_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                auto_buy_paused INTEGER NOT NULL DEFAULT 1,
                pause_reason TEXT NOT NULL DEFAULT 'INITIAL_SAFETY_LOCK',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS auto_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                score INTEGER NOT NULL,
                price INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(signal_date, symbol, action)
            );

            CREATE TABLE IF NOT EXISTS daily_risk (
                risk_date TEXT PRIMARY KEY,
                start_equity INTEGER NOT NULL,
                current_equity INTEGER NOT NULL,
                loss_rate REAL NOT NULL DEFAULT 0,
                halted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                initial_cash INTEGER NOT NULL,
                final_equity INTEGER NOT NULL,
                total_pnl INTEGER NOT NULL,
                total_return_rate REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                trade_count INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                rules TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS automation_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                stop_loss_rate REAL NOT NULL,
                take_profit_rate REAL NOT NULL,
                trailing_stop_rate REAL NOT NULL,
                max_holding_days INTEGER NOT NULL,
                min_score INTEGER NOT NULL,
                max_positions INTEGER NOT NULL,
                max_order_amount INTEGER NOT NULL,
                daily_loss_limit_rate REAL NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS position_tracking (
                symbol TEXT PRIMARY KEY,
                highest_price INTEGER NOT NULL,
                first_bought_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS automation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                symbol TEXT,
                message TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_performance (
                performance_date TEXT PRIMARY KEY,
                cash INTEGER NOT NULL,
                market_value INTEGER NOT NULL,
                equity INTEGER NOT NULL,
                daily_pnl INTEGER NOT NULL,
                daily_return_rate REAL NOT NULL,
                total_pnl INTEGER NOT NULL,
                total_return_rate REAL NOT NULL,
                realized_pnl INTEGER NOT NULL,
                unrealized_pnl INTEGER NOT NULL,
                buy_count INTEGER NOT NULL,
                sell_count INTEGER NOT NULL,
                position_count INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                price_source TEXT NOT NULL,
                is_final INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute("INSERT OR IGNORE INTO paper_account(id, cash) VALUES (1, ?)",
                     (settings.paper_initial_cash,))
        conn.execute("""INSERT OR IGNORE INTO automation_state
                        (id, auto_buy_paused, pause_reason) VALUES (1, 1, 'INITIAL_SAFETY_LOCK')""")
        conn.execute(
            """INSERT OR IGNORE INTO automation_config
               (id,stop_loss_rate,take_profit_rate,trailing_stop_rate,max_holding_days,
                min_score,max_positions,max_order_amount,daily_loss_limit_rate)
               VALUES(1,?,?,?,?,?,?,?,?)""",
            (settings.paper_stop_loss_rate, settings.paper_take_profit_rate, 0.05, 20,
             settings.paper_auto_buy_min_score, settings.paper_max_positions,
             settings.paper_max_order_amount, settings.paper_daily_loss_limit_rate),
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
        if "fee" not in columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN fee INTEGER NOT NULL DEFAULT 0")
        if "tax" not in columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN tax INTEGER NOT NULL DEFAULT 0")
        if "realized_pnl" not in columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN realized_pnl INTEGER NOT NULL DEFAULT 0")
        if "reason" not in columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN reason TEXT NOT NULL DEFAULT 'MANUAL'")
        signal_columns = {row[1] for row in conn.execute("PRAGMA table_info(auto_signals)")}
        if "name" not in signal_columns:
            conn.execute("ALTER TABLE auto_signals ADD COLUMN name TEXT NOT NULL DEFAULT ''")


@contextmanager
def db():
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
