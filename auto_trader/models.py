from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Price(BaseModel):
    symbol: str
    name: str = ""
    price: Decimal = Field(gt=0)
    currency: str = "KRW"
    timestamp: datetime | None = None


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    client_order_id: str | None = Field(default=None, max_length=36, pattern=r"^[a-zA-Z0-9_-]+$")


class Order(BaseModel):
    id: int
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    status: Literal["SUBMITTED", "ACCEPTED", "PARTIALLY_FILLED", "PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED", "FILLED", "CANCELED", "REJECTED", "REPLACED"]
    mode: Literal["PAPER", "DRY_RUN", "LIVE"]
    created_at: str = ""
    external_order_id: str | None = None


class StrategyBacktestRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    count: int = Field(default=200, ge=60, le=200)
    initial_cash: Decimal = Field(default=Decimal("10000000"), gt=0)


class Portfolio(BaseModel):
    cash: Decimal
    positions: dict[str, Decimal]
    realized_pnl: Decimal


class Recommendation(BaseModel):
    rank: int
    score: int
    name: str
    symbol: str
    price: Decimal
    currency: str
    recommended_quantity: int
    recommended_amount: Decimal
    indicators: str = "시세 기반 후보"


class Candle(BaseModel):
    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
