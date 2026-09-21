from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, HTTPException

from .models import Order, OrderRequest, Portfolio, Price
from .paper import PaperBroker

app = FastAPI(title="Toss Auto Trader", version="0.1.0")
broker = PaperBroker()
prices: dict[str, Price] = {}
mode: Literal["PAPER", "DRY_RUN"] = "PAPER"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": mode}


@app.get("/api/v1/market/prices", response_model=list[Price])
def get_prices() -> list[Price]:
    return list(prices.values())


@app.put("/api/v1/market/prices/{symbol}", response_model=Price)
def set_price(symbol: str, price: Decimal, currency: str = "KRW") -> Price:
    value = Price(symbol=symbol.upper(), price=price, currency=currency)
    prices[value.symbol] = value
    return value


@app.get("/api/v1/portfolio", response_model=Portfolio)
def get_portfolio() -> Portfolio:
    return broker.portfolio()


@app.get("/api/v1/orders", response_model=list[Order])
def get_orders() -> list[Order]:
    return broker.orders()


@app.post("/api/v1/orders", response_model=Order)
def create_order(request: OrderRequest) -> Order:
    if mode == "PAPER" and request.symbol not in prices:
        raise HTTPException(400, "paper mode requires a known price")
    return broker.place(request, mode)


@app.post("/api/v1/mode/{new_mode}")
def set_mode(new_mode: Literal["PAPER", "DRY_RUN"]) -> dict[str, str]:
    global mode
    mode = new_mode
    return {"mode": mode}
