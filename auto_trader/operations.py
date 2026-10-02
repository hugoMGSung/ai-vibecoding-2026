"""Single-process guard and durable operational event log."""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys
from threading import Lock


class SingleInstanceLock:
    def __init__(self, path: str = "auto_trader.lock") -> None:
        self.path = Path(path)
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        file = self.path.open("a+b")
        try:
            file.seek(0)
            if file.read(1) == b"":
                file.seek(0)
                file.write(b"0")
                file.flush()
            file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            file.close()
            raise RuntimeError("같은 데이터베이스를 사용하는 서버가 이미 실행 중입니다.") from exc
        self._file = file

    def release(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                           "event": getattr(record, "event", "runtime"), "message": record.getMessage()}, ensure_ascii=False)


def audit_logger() -> logging.Logger:
    logger = logging.getLogger("auto_trader.audit")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class OperationsStore:
    def __init__(self, db_path: str = "auto_trader.db") -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("CREATE TABLE IF NOT EXISTS auto_runtime (id INTEGER PRIMARY KEY CHECK(id=1), running INTEGER NOT NULL, message TEXT NOT NULL, last_action TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS operational_events (id INTEGER PRIMARY KEY, time TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL, severity TEXT NOT NULL)")
        self._db.commit()
        self._lock = Lock()

    def restore_stopped(self) -> dict:
        """A task cannot survive process restart; remember interruption explicitly."""
        with self._lock:
            row = self._db.execute("SELECT running,message,last_action FROM auto_runtime WHERE id=1").fetchone()
            interrupted = bool(row and row[0])
            message = "서버 재시작으로 자동매매가 중지되었습니다." if interrupted else (row[1] if row else "대기 중")
            last_action = row[2] if row else ""
            self._db.execute("INSERT INTO auto_runtime VALUES (1,0,?,?,?) ON CONFLICT(id) DO UPDATE SET running=0,message=excluded.message,updated_at=excluded.updated_at", (message, last_action, datetime.now(timezone.utc).isoformat()))
            self._db.commit()
        if interrupted:
            self.add_event("자동매매 중단", message, "warning")
        return {"running": False, "message": message, "last_action": last_action}

    def save_auto_state(self, state: dict) -> None:
        with self._lock:
            self._db.execute("INSERT INTO auto_runtime VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET running=excluded.running,message=excluded.message,last_action=excluded.last_action,updated_at=excluded.updated_at",
                             (int(bool(state["running"])), str(state["message"])[:500], str(state["last_action"])[:500], datetime.now(timezone.utc).isoformat()))
            self._db.commit()

    def add_event(self, event: str, detail: str, severity: str = "info") -> None:
        if severity not in {"info", "warning", "error"}:
            raise ValueError("잘못된 이벤트 심각도")
        at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute("INSERT INTO operational_events(time,event,detail,severity) VALUES(?,?,?,?)", (at, event[:100], detail[:500], severity))
            self._db.commit()
        audit_logger().log(logging.ERROR if severity == "error" else logging.WARNING if severity == "warning" else logging.INFO,
                           detail[:500], extra={"event": event[:100]})

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT time,event,detail,severity FROM operational_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("time", "event", "detail", "severity"), row)) for row in rows]

    def alert_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM operational_events WHERE severity IN ('warning','error') AND time>=?", ((datetime.now(timezone.utc).date().isoformat()),)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
