import sqlite3
from pathlib import Path
from threading import Lock


class RiskGuard:
    """Persisted emergency stop and per-KST-day loss circuit breaker."""

    def __init__(self, db_path: str = "auto_trader.db") -> None:
        self._db = sqlite3.connect(Path(db_path), check_same_thread=False)
        self._lock = Lock()
        self._db.execute("CREATE TABLE IF NOT EXISTS risk_control (id INTEGER PRIMARY KEY CHECK(id=1), emergency_stop INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')")
        self._db.execute("INSERT OR IGNORE INTO risk_control(id, emergency_stop, reason) VALUES(1, 0, '')")
        self._db.execute("CREATE TABLE IF NOT EXISTS risk_daily (day TEXT PRIMARY KEY, opening_equity TEXT NOT NULL, halted INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')")
        self._db.commit()

    def emergency_state(self) -> dict[str, bool | str]:
        with self._lock:
            row = self._db.execute("SELECT emergency_stop, reason FROM risk_control WHERE id=1").fetchone()
            return {"active": bool(row[0]), "reason": row[1]}

    def activate_emergency_stop(self, reason: str) -> None:
        with self._lock:
            self._db.execute("UPDATE risk_control SET emergency_stop=1, reason=? WHERE id=1", (reason[:250],))
            self._db.commit()

    def clear_emergency_stop(self) -> None:
        with self._lock:
            self._db.execute("UPDATE risk_control SET emergency_stop=0, reason='' WHERE id=1")
            self._db.commit()

    def start_day_if_needed(self, day: str, equity: str) -> dict[str, str | bool]:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO risk_daily(day, opening_equity, halted, reason) VALUES(?, ?, 0, '')", (day, equity))
            self._db.commit()
            row = self._db.execute("SELECT opening_equity, halted, reason FROM risk_daily WHERE day=?", (day,)).fetchone()
            return {"opening_equity": row[0], "halted": bool(row[1]), "reason": row[2]}

    def daily_state(self, day: str) -> dict[str, str | bool] | None:
        """Return the persisted daily baseline without creating one."""
        with self._lock:
            row = self._db.execute("SELECT opening_equity, halted, reason FROM risk_daily WHERE day=?", (day,)).fetchone()
            if row is None:
                return None
            return {"opening_equity": row[0], "halted": bool(row[1]), "reason": row[2]}

    def halt_day(self, day: str, reason: str) -> None:
        with self._lock:
            self._db.execute("UPDATE risk_daily SET halted=1, reason=? WHERE day=?", (reason[:250], day))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
