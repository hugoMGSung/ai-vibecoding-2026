from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Price(BaseModel):
    symbol: str
    price: Decimal = Field(gt=0)
    currency: str = "KRW"


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)


class Order(BaseModel):
    id: int
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    status: Literal["FILLED", "REJECTED"]
    mode: Literal["PAPER", "DRY_RUN"]


class Portfolio(BaseModel):
    cash: Decimal
    positions: dict[str, Decimal]
    realized_pnl: Decimal

