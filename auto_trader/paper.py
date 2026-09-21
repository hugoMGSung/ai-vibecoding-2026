from decimal import Decimal
from threading import Lock

from .models import Order, OrderRequest, Portfolio


class PaperBroker:
    def __init__(self, initial_cash: Decimal = Decimal("10000000")) -> None:
        self._cash = initial_cash
        self._positions: dict[str, Decimal] = {}
        self._realized_pnl = Decimal("0")
        self._orders: list[Order] = []
        self._next_id = 1
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
            position = self._positions.get(request.symbol, Decimal("0"))
            if request.side == "BUY":
                if mode == "PAPER" and amount > self._cash:
                    status = "REJECTED"
                else:
                    status = "FILLED"
                    if mode == "PAPER":
                        self._cash -= amount
                        self._positions[request.symbol] = position + request.quantity
            else:
                status = "FILLED" if mode == "DRY_RUN" or position >= request.quantity else "REJECTED"
                if status == "FILLED" and mode == "PAPER":
                    self._cash += amount
                    self._positions[request.symbol] = position - request.quantity
            order = Order(id=self._next_id, client_order_id=f"paper-{self._next_id}", **request.model_dump(), status=status, mode=mode)
            self._next_id += 1
            self._orders.append(order)
            return order
