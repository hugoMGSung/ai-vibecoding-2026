from decimal import Decimal
import sqlite3
from pathlib import Path
from threading import Lock
from datetime import datetime, timezone

from .models import Order, OrderRequest, Portfolio


class PaperBroker:
    def __init__(self, initial_cash: Decimal = Decimal("10000000"), commission_rate: Decimal = Decimal("0.00015"), db_path: str = "auto_trader.db") -> None:
        self._db_path = Path(db_path)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id=1), cash TEXT NOT NULL, realized_pnl TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_positions (symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_orders (id INTEGER PRIMARY KEY, client_order_id TEXT, symbol TEXT, side TEXT, quantity TEXT, price TEXT, status TEXT, mode TEXT)")
        self._db.execute("CREATE TABLE IF NOT EXISTS paper_position_meta (symbol TEXT PRIMARY KEY, opened_at TEXT NOT NULL, high_water TEXT NOT NULL)")
        order_columns = {row[1] for row in self._db.execute("PRAGMA table_info(paper_orders)")}
        if "created_at" not in order_columns:
            self._db.execute("ALTER TABLE paper_orders ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        self._db.commit()
        account = self._db.execute("SELECT cash, realized_pnl FROM paper_account WHERE id=1").fetchone()
        self._cash = Decimal(account[0]) if account else initial_cash
        self._commission_rate = commission_rate
        self._realized_pnl = Decimal(account[1]) if account else Decimal("0")
        self._positions = {row[0]: Decimal(row[1]) for row in self._db.execute("SELECT symbol, quantity FROM paper_positions") if Decimal(row[1]) > 0}
        self._orders = [Order(id=row[0], client_order_id=row[1], symbol=row[2], side=row[3], quantity=row[4], price=row[5], status=row[6], mode=row[7], created_at=row[8]) for row in self._db.execute("SELECT id, client_order_id, symbol, side, quantity, price, status, mode, created_at FROM paper_orders ORDER BY id")]
        self._realized_pnl = self._recalculate_realized_pnl()
        self._db.execute("INSERT OR REPLACE INTO paper_account(id, cash, realized_pnl) VALUES(1, ?, ?)", (str(self._cash), str(self._realized_pnl)))
        self._db.commit()
        self._next_id = (self._orders[-1].id + 1) if self._orders else 1
        self._lock = Lock()

    def _recalculate_realized_pnl(self) -> Decimal:
        quantities: dict[str, Decimal] = {}
        bases: dict[str, Decimal] = {}
        realized = Decimal("0")
        for order in self._orders:
            if order.mode != "PAPER" or order.status != "FILLED":
                continue
            quantity = quantities.get(order.symbol, Decimal("0"))
            basis = bases.get(order.symbol, Decimal("0"))
            amount = order.quantity * order.price
            fee = (amount * self._commission_rate).quantize(Decimal("0.01"))
            if order.side == "BUY":
                quantities[order.symbol] = quantity + order.quantity
                bases[order.symbol] = basis + amount + fee
            elif quantity > 0:
                sold = min(quantity, order.quantity)
                average_cost = basis / quantity
                realized += (order.price - average_cost) * sold - fee
                quantities[order.symbol] = quantity - sold
                bases[order.symbol] = basis - average_cost * sold
        return realized

    def portfolio(self) -> Portfolio:
        with self._lock:
            return Portfolio(cash=self._cash, positions=self._positions.copy(), realized_pnl=self._realized_pnl)

    def orders(self) -> list[Order]:
        with self._lock:
            return self._orders.copy()

    def position_cost_basis(self, symbol: str) -> Decimal:
        """Average-cost basis of the remaining PAPER position, including buy fees."""
        with self._lock:
            return self._position_cost_basis_unlocked(symbol)

    def _position_cost_basis_unlocked(self, symbol: str) -> Decimal:
        quantity = Decimal("0")
        basis = Decimal("0")
        for order in self._orders:
            if order.symbol != symbol or order.mode != "PAPER" or order.status != "FILLED":
                continue
            notional = order.quantity * order.price
            fee = (notional * self._commission_rate).quantize(Decimal("0.01"))
            if order.side == "BUY":
                quantity += order.quantity
                basis += notional + fee
            elif quantity > 0:
                sold = min(order.quantity, quantity)
                basis -= (basis / quantity) * sold
                quantity -= sold
                if quantity <= 0:
                    quantity, basis = Decimal("0"), Decimal("0")
        return (basis / quantity).quantize(Decimal("0.01")) if quantity else Decimal("0")

    def position_metadata(self, symbol: str) -> dict[str, str] | None:
        with self._lock:
            row = self._db.execute("SELECT opened_at, high_water FROM paper_position_meta WHERE symbol=?", (symbol,)).fetchone()
            return {"opened_at": row[0], "high_water": row[1]} if row else None

    def update_position_high_water(self, symbol: str, price: Decimal) -> dict[str, str]:
        with self._lock:
            row = self._db.execute("SELECT opened_at, high_water FROM paper_position_meta WHERE symbol=?", (symbol,)).fetchone()
            if row:
                opened_at, high_water = row[0], max(Decimal(row[1]), price)
            else:
                prior_buys, position_quantity = [], Decimal("0")
                for order in self._orders:
                    if order.symbol != symbol or order.mode != "PAPER" or order.status != "FILLED":
                        continue
                    if order.side == "BUY":
                        if position_quantity == 0:
                            prior_buys = []
                        prior_buys.append(order)
                        position_quantity += order.quantity
                    else:
                        position_quantity -= order.quantity
                        if position_quantity <= 0:
                            position_quantity, prior_buys = Decimal("0"), []
                opened_at = (prior_buys[0].created_at if prior_buys and prior_buys[0].created_at else datetime.now(timezone.utc).isoformat())
                high_water = max([price, *(o.price for o in prior_buys)])
            self._db.execute("INSERT OR REPLACE INTO paper_position_meta(symbol, opened_at, high_water) VALUES(?, ?, ?)", (symbol, opened_at, str(high_water)))
            self._db.commit()
            return {"opened_at": opened_at, "high_water": str(high_water)}

    def has_prior_sell(self, symbol: str) -> bool:
        with self._lock:
            position_quantity, active_start = Decimal("0"), None
            partial_sell = False
            for order in self._orders:
                if order.symbol != symbol or order.mode != "PAPER" or order.status != "FILLED":
                    continue
                if order.side == "BUY":
                    if position_quantity == 0:
                        active_start, partial_sell = order.id, False
                    position_quantity += order.quantity
                elif position_quantity > 0:
                    position_quantity -= order.quantity
                    if position_quantity <= 0:
                        position_quantity, active_start, partial_sell = Decimal("0"), None, False
                    else:
                        partial_sell = True
            return bool(position_quantity > 0 and active_start is not None and partial_sell)

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
                    avg_cost = self._position_cost_basis_unlocked(request.symbol)
                    self._cash += amount - commission
                    self._positions[request.symbol] = position - request.quantity
                    if self._positions[request.symbol] <= 0:
                        self._positions.pop(request.symbol, None)
                        self._db.execute("DELETE FROM paper_position_meta WHERE symbol=?", (request.symbol,))
                    self._realized_pnl += (request.price - avg_cost) * request.quantity - commission
            order = Order(id=self._next_id, client_order_id=f"paper-{self._next_id}", **request.model_dump(exclude={"client_order_id"}), status=status, mode=mode, created_at=datetime.now(timezone.utc).isoformat())
            if status == "FILLED" and mode == "PAPER" and request.side == "BUY":
                previous_meta = self._db.execute("SELECT opened_at, high_water FROM paper_position_meta WHERE symbol=?", (request.symbol,)).fetchone()
                opened_at = previous_meta[0] if previous_meta else datetime.now(timezone.utc).isoformat()
                high_water = max(Decimal(previous_meta[1]), request.price) if previous_meta else request.price
                self._db.execute("INSERT OR REPLACE INTO paper_position_meta(symbol, opened_at, high_water) VALUES(?, ?, ?)", (request.symbol, opened_at, str(high_water)))
            self._next_id += 1
            self._orders.append(order)
            self._db.execute("INSERT OR REPLACE INTO paper_account(id, cash, realized_pnl) VALUES(1, ?, ?)", (str(self._cash), str(self._realized_pnl)))
            self._db.execute("DELETE FROM paper_positions")
            self._db.executemany("INSERT INTO paper_positions(symbol, quantity) VALUES(?, ?)", [(s, str(q)) for s, q in self._positions.items()])
            self._db.execute("INSERT INTO paper_orders(id, client_order_id, symbol, side, quantity, price, status, mode, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", (order.id, order.client_order_id, order.symbol, order.side, str(order.quantity), str(order.price), order.status, order.mode, order.created_at))
            self._db.commit()
            return order
