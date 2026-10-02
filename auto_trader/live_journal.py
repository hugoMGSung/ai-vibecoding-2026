"""Durable, read-only reconciliation of Toss order executions.

The remote account is the source of truth. This journal never creates a local
cash balance or applies simulated fills to a LIVE position.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from threading import Lock


OPEN_STATUSES = {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED"}
FINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "REPLACED"}


def parse_execution(order: dict) -> dict:
    execution = order.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("체결 내역이 없습니다.")
    status = order.get("status")
    if status not in OPEN_STATUSES | FINAL_STATUSES:
        raise ValueError("알 수 없는 원격 주문 상태입니다.")
    try:
        quantity = Decimal(str(order["quantity"]))
        filled = Decimal(str(execution["filledQuantity"]))
        amount = Decimal(str(execution["filledAmount"] or "0"))
        commission = Decimal(str(execution["commission"] or "0"))
        tax = Decimal(str(execution["tax"] or "0"))
        average = Decimal(str(execution["averageFilledPrice"] or "0"))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError("체결 수량·금액 형식이 잘못되었습니다.") from exc
    if not all(x.is_finite() and x >= 0 for x in (quantity, filled, amount, commission, tax, average)) or quantity <= 0 or filled > quantity:
        raise ValueError("원격 체결 수량·금액이 유효하지 않습니다.")
    if filled and (average <= 0 or amount <= 0):
        raise ValueError("체결 가격 또는 체결 금액이 누락되었습니다.")
    if status == "FILLED" and filled != quantity:
        raise ValueError("전량 체결 상태와 체결 수량이 일치하지 않습니다.")
    if order.get("currency") != "KRW" or order.get("side") not in {"BUY", "SELL"}:
        raise ValueError("국내 원화 주문만 동기화할 수 있습니다.")
    order_id, symbol = order.get("orderId"), order.get("symbol")
    if not isinstance(order_id, str) or not order_id or not isinstance(symbol, str) or not symbol:
        raise ValueError("주문 식별자 또는 종목이 없습니다.")
    return {"order_id": order_id, "symbol": symbol, "side": order["side"], "status": status,
            "quantity": str(quantity), "filled_quantity": str(filled), "filled_amount": str(amount),
            "average_filled_price": str(average), "commission": str(commission), "tax": str(tax),
            "filled_at": execution.get("filledAt")}


class LiveJournal:
    def __init__(self, db_path: str = "auto_trader.db") -> None:
        self._db = sqlite3.connect(Path(db_path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("CREATE TABLE IF NOT EXISTS live_orders (order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL, filled_amount TEXT NOT NULL, average_filled_price TEXT NOT NULL, commission TEXT NOT NULL, tax TEXT NOT NULL, filled_at TEXT, observed_at TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS live_order_events (id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, status TEXT NOT NULL, filled_quantity TEXT NOT NULL, filled_amount TEXT NOT NULL, commission TEXT NOT NULL, tax TEXT NOT NULL, observed_at TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS live_order_intents (client_order_id TEXT PRIMARY KEY, order_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_live_intent_order_id ON live_order_intents(order_id) WHERE order_id IS NOT NULL")
        self._db.commit()
        self._lock = Lock()

    def record_intent(self, client_order_id: str, symbol: str, side: str, quantity: Decimal, price: Decimal) -> None:
        """Persist before any future LIVE submission; an unknown result blocks reuse."""
        if not client_order_id:
            raise ValueError("client_order_id가 필요합니다.")
        with self._lock:
            self._db.execute("INSERT INTO live_order_intents VALUES (?, NULL, ?, ?, ?, ?, 'UNKNOWN', ?)",
                             (client_order_id, symbol, side, str(quantity), str(price), datetime.now(timezone.utc).isoformat()))
            self._db.commit()

    def link_order(self, client_order_id: str, order_id: str) -> None:
        with self._lock:
            if self._db.execute("SELECT 1 FROM live_order_intents WHERE order_id=?", (order_id,)).fetchone():
                raise ValueError("원격 주문 ID가 이미 다른 주문 의도에 연결되어 있습니다.")
            cursor = self._db.execute("UPDATE live_order_intents SET order_id=?, status='PENDING' WHERE client_order_id=? AND order_id IS NULL", (order_id, client_order_id))
            if cursor.rowcount != 1:
                raise ValueError("기록된 주문 의도를 찾을 수 없습니다.")
            self._db.commit()

    def unresolved_intents(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT client_order_id, order_id, status FROM live_order_intents WHERE status NOT IN ('FILLED','CANCELED','REJECTED','REPLACED')").fetchall()
        return [{"client_order_id": row[0], "order_id": row[1], "status": row[2]} for row in rows]

    def intent(self, client_order_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT client_order_id,order_id,symbol,side,quantity,price,status,created_at FROM live_order_intents WHERE client_order_id=?", (client_order_id,)).fetchone()
        return dict(zip(("client_order_id", "order_id", "symbol", "side", "quantity", "price", "status", "created_at"), row)) if row else None

    def unfinished_order_ids(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT order_id FROM live_orders WHERE status NOT IN ('FILLED','CANCELED','REJECTED','REPLACED')").fetchall()
        return {row[0] for row in rows}

    def apply_detail(self, order: dict) -> dict:
        parsed = parse_execution(order)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            old = self._db.execute("SELECT symbol, side, quantity, filled_quantity, filled_amount, commission, tax FROM live_orders WHERE order_id=?", (parsed["order_id"],)).fetchone()
            if old:
                if old[0] != parsed["symbol"] or old[1] != parsed["side"] or Decimal(old[2]) != Decimal(parsed["quantity"]):
                    raise ValueError("기존 주문과 원격 주문의 기본 정보가 다릅니다.")
                if any(Decimal(new) < Decimal(prior) for new, prior in zip(
                    (parsed["filled_quantity"], parsed["filled_amount"], parsed["commission"], parsed["tax"]), old[3:])):
                    raise ValueError("원격 누적 체결 정보가 이전 관측보다 감소했습니다.")
            with self._db:
                prior_event = self._db.execute("SELECT status FROM live_order_events WHERE order_id=? ORDER BY id DESC LIMIT 1", (parsed["order_id"],)).fetchone()
                self._db.execute("INSERT INTO live_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, filled_quantity=excluded.filled_quantity, filled_amount=excluded.filled_amount, average_filled_price=excluded.average_filled_price, commission=excluded.commission, tax=excluded.tax, filled_at=excluded.filled_at, observed_at=excluded.observed_at",
                                 (parsed["order_id"], parsed["symbol"], parsed["side"], parsed["status"], parsed["quantity"], parsed["filled_quantity"], parsed["filled_amount"], parsed["average_filled_price"], parsed["commission"], parsed["tax"], parsed["filled_at"], now))
                if not old or any(Decimal(new) != Decimal(prior) for new, prior in zip(
                    (parsed["filled_quantity"], parsed["filled_amount"], parsed["commission"], parsed["tax"]), old[3:])) or not prior_event or parsed["status"] != prior_event[0]:
                    self._db.execute("INSERT INTO live_order_events(order_id,status,filled_quantity,filled_amount,commission,tax,observed_at) VALUES (?,?,?,?,?,?,?)",
                                     (parsed["order_id"], parsed["status"], parsed["filled_quantity"], parsed["filled_amount"], parsed["commission"], parsed["tax"], now))
                self._db.execute("UPDATE live_order_intents SET status=? WHERE order_id=?", (parsed["status"], parsed["order_id"]))
        return parsed

    def recent_orders(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT order_id,symbol,side,status,quantity,filled_quantity,average_filled_price,filled_amount,commission,tax,observed_at FROM live_orders ORDER BY observed_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ("order_id", "symbol", "side", "status", "quantity", "filled_quantity", "average_filled_price", "filled_amount", "commission", "tax", "observed_at")
        return [dict(zip(keys, row)) for row in rows]

    def order(self, order_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT order_id,symbol,side,status,quantity,filled_quantity FROM live_orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(zip(("order_id", "symbol", "side", "status", "quantity", "filled_quantity"), row)) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
