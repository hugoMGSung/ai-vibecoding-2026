from decimal import Decimal
import sqlite3
from pathlib import Path
from threading import Lock

from .models import Order, OrderRequest, Portfolio


class PaperBroker:
    def __init__(self, initial_cash: Decimal = Decimal("10000000"), commission_rate: Decimal = Decimal("0.00015"), db_path: str = "auto_trader.db") -> None:
        self._db_path = Path(db_path)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id=1), cash TEXT NOT NULL, realized_pnl TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_positions (symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_orders (id INTEGER PRIMARY KEY, client_order_id TEXT, symbol TEXT, side TEXT, quantity TEXT, price TEXT, status TEXT, mode TEXT)")
        self._db.commit()
        account = self._db.execute("SELECT cash, realized_pnl FROM paper_account WHERE id=1").fetchone()
        self._cash = Decimal(account[0]) if account else initial_cash
        self._commission_rate = commission_rate
        self._realized_pnl = Decimal(account[1]) if account else Decimal("0")
        self._positions = {row[0]: Decimal(row[1]) for row in self._db.execute("SELECT symbol, quantity FROM paper_positions") if Decimal(row[1]) > 0}
        self._orders = [Order(id=row[0], client_order_id=row[1], symbol=row[2], side=row[3], quantity=row[4], price=row[5], status=row[6], mode=row[7]) for row in self._db.execute("SELECT id, client_order_id, symbol, side, quantity, price, status, mode FROM paper_orders ORDER BY id")]
        self._next_id = (self._orders[-1].id + 1) if self._orders else 1
        self._lock = Lock()

    def portfolio(self) -> Portfolio:
        with self._lock:
            return Portfolio(cash=self._cash, positions=self._positions.copy(), realized_pnl=self._realized_pnl)

    def orders(self) -> list[Order]:
        with self._lock:
            return self._orders.copy()

    def place(self, request: OrderRequest, mode: str = "PAPER") -> Order:
        with self._lock:
            amount = request.quantity * request.price
            commission = (amount * self._commission_rate).quantize(Decimal("0.01"))
            position = self._positions.get(request.symbol, Decimal("0"))
            if request.side == "BUY":
                # PAPER와 DRY_RUN 모두 가상 매수 가능 금액을 초과하면 체결하지 않는다.
                # DRY_RUN은 현금을 차감하지 않지만 잔고 검사는 수행한다.
                if amount + commission > self._cash:
                    status = "REJECTED"
                else:
                    status = "FILLED"
                    if mode == "PAPER":
                        self._cash -= amount + commission
                        self._positions[request.symbol] = position + request.quantity
            else:
                status = "FILLED" if mode == "DRY_RUN" or position >= request.quantity else "REJECTED"
                if status == "FILLED" and mode == "PAPER":
                    self._cash += amount - commission
                    self._positions[request.symbol] = position - request.quantity
                    if self._positions[request.symbol] <= 0:
                        self._positions.pop(request.symbol, None)
            order = Order(id=self._next_id, client_order_id=f"paper-{self._next_id}", **request.model_dump(), status=status, mode=mode)
            self._next_id += 1
            self._orders.append(order)
            self._db.execute("INSERT OR REPLACE INTO paper_account(id, cash, realized_pnl) VALUES(1, ?, ?)", (str(self._cash), str(self._realized_pnl)))
            self._db.execute("DELETE FROM paper_positions")
            self._db.executemany("INSERT INTO paper_positions(symbol, quantity) VALUES(?, ?)", [(s, str(q)) for s, q in self._positions.items()])
            self._db.execute("INSERT INTO paper_orders VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (order.id, order.client_order_id, order.symbol, order.side, str(order.quantity), str(order.price), order.status, order.mode))
            self._db.commit()
            return order
