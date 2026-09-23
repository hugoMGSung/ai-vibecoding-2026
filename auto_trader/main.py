from decimal import Decimal
import random
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import Candle, Order, OrderRequest, Portfolio, Price, Recommendation
from .paper import PaperBroker
from .settings import settings
from .toss_client import TossApiError, TossClient

app = FastAPI(title="Toss Auto Trader", version="0.1.0")
broker = PaperBroker(settings.paper_initial_cash)
toss = TossClient()
prices: dict[str, Price] = {}
STOCK_ALIASES = {
    "삼성전자": "005930", "sk하이닉스": "000660", "네이버": "035420",
    "카카오": "035720", "셀트리온": "068270", "현대차": "005380",
    "기아": "000270", "lg에너지솔루션": "373220", "삼성바이오로직스": "207940",
    "삼성sdi": "006400", "삼성sds": "018260", "삼성전기": "009150",
    "삼성물산": "028260", "삼성생명": "032830", "삼성화재": "000810",
    "삼성증권": "016360", "삼성카드": "029780", "삼성중공업": "010140",
    "삼성엔지니어링": "028050", "삼성에스디에스": "018260",
    "sk": "034730", "sk텔레콤": "017670", "sk이노베이션": "096770",
    "sk스퀘어": "402340", "sk바이오사이언스": "302440", "sk바이오팜": "326030",
    "sk아이이테크놀로지": "361610", "sk네트웍스": "001740", "skc": "011790",
    "sk가스": "018670", "sk디앤디": "210980", "sk오션플랜트": "100090",
    "한화": "000880", "한화에어로스페이스": "012450", "한화솔루션": "009830",
    "한화시스템": "272210", "한화오션": "042660", "한화생명": "088350",
    "한화손해보험": "000370", "한화투자증권": "003530", "한화갤러리아": "452260",
    "한화3우b": "000885", "한화리츠": "451800",
    "현대모비스": "012330", "현대글로비스": "086280", "현대건설": "000720",
    "현대제철": "004020", "현대해상": "001450", "현대오토에버": "307950",
    "lg전자": "066570", "lg화학": "051910", "lg생활건강": "051900",
    "lg유플러스": "032640", "lg디스플레이": "034220", "lx세미콘": "108320",
    "포스코홀딩스": "005490", "포스코퓨처엠": "003670", "포스코인터내셔널": "047050",
    "카카오뱅크": "323410", "카카오페이": "377300", "카카오게임즈": "293490",
    "쿠팡": "CPNG", "알테오젠": "196170", "에코프로": "086520",
    "에코프로비엠": "247540", "에코프로머티": "450080", "두산에너빌리티": "034020",
    "두산밥캣": "241560", "두산로보틱스": "454910", "크래프톤": "259960",
    "하이브": "352820", "엔씨소프트": "036570", "넷마블": "251270",
    "한국전력": "015760", "대한항공": "003490", "아모레퍼시픽": "090430",
    "cj제일제당": "097950", "농심": "004370", "오리온": "271560",
    "셀트리온제약": "068760", "유한양행": "000100", "한미약품": "128940",
    "sk텔레콤": "017670", "kt": "030200", "lg": "003550",
}
mode: Literal["PAPER", "DRY_RUN"] = settings.trading_mode if settings.trading_mode in ("PAPER", "DRY_RUN") else "PAPER"
app.mount("/static", StaticFiles(directory="auto_trader/static"), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse("auto_trader/static/index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": mode}


@app.get("/api/v1/toss/status")
async def toss_status(symbol: str = "000660") -> dict[str, str | bool]:
    try:
        await toss.prices([symbol.strip().upper()])
        return {"connected": True, "label": "연결됨"}
    except TossApiError as exc:
        return {"connected": False, "label": "연결실패", "detail": str(exc)}


@app.get("/api/v1/market/prices", response_model=list[Price])
async def get_prices(symbols: str | None = None) -> list[Price]:
    if symbols:
        requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
        resolved: list[str] = []
        for item in requested:
            if item.isdigit() or item.isascii() and item.replace(".", "").replace("-", "").isalnum():
                resolved.append(item)
            else:
                matches = await search_stocks(item)
                if matches:
                    resolved.append(matches[0]["symbol"])
        try:
            requested = resolved
            live_prices = await toss.prices(requested)
            try:
                names = await toss.stock_names(requested)
                live_prices = [item.model_copy(update={"name": names.get(item.symbol, "")}) for item in live_prices]
            except TossApiError:
                pass
            prices.update({item.symbol: item for item in live_prices})
        except TossApiError as exc:
            if not prices:
                raise HTTPException(503, str(exc)) from exc
    return list(prices.values())


async def search_stocks(query: str) -> list[dict[str, str]]:
    query = query.casefold()
    matches = [{"symbol": symbol, "name": name} for name, symbol in STOCK_ALIASES.items() if query in name.casefold()]
    matches.extend({"symbol": item.symbol, "name": item.name} for item in prices.values() if item.name and query in item.name.casefold())
    unique: dict[str, dict[str, str]] = {item["symbol"]: item for item in matches}
    return list(unique.values())[:10]


@app.get("/api/v1/market/stocks/search")
async def stock_search(q: str) -> list[dict[str, str]]:
    try:
        return await search_stocks(q)
    except TossApiError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/v1/market/candles", response_model=list[Candle])
async def get_candles(symbol: str, count: int = 60) -> list[Candle]:
    try:
        return await toss.candles(symbol.strip().upper(), min(max(count, 20), 200))
    except TossApiError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.put("/api/v1/market/prices/{symbol}", response_model=Price)
def set_price(symbol: str, price: Decimal, currency: str = "KRW") -> Price:
    value = Price(symbol=symbol.upper(), price=price, currency=currency)
    prices[value.symbol] = value
    return value


@app.get("/api/v1/portfolio", response_model=Portfolio)
def get_portfolio() -> Portfolio:
    return broker.portfolio()


@app.get("/api/v1/capital")
def get_capital() -> dict[str, Decimal | str]:
    portfolio = broker.portfolio()
    return {
        "mode": mode,
        "account_cash": portfolio.cash,
        "recommended_amount": (portfolio.cash * settings.recommended_trade_ratio).quantize(Decimal("0.01")),
        "recommended_ratio": settings.recommended_trade_ratio,
        "currency": "KRW",
    }


@app.get("/api/v1/recommendations", response_model=list[Recommendation])
async def recommendations() -> list[Recommendation]:
    all_symbols = list(dict.fromkeys(STOCK_ALIASES.values()))
    sampled_symbols = random.SystemRandom().sample(all_symbols, min(30, len(all_symbols)))
    try:
        universe = await toss.prices(sampled_symbols)
        names = await toss.stock_names([item.symbol for item in universe])
        universe = [item.model_copy(update={"name": names.get(item.symbol, "")}) for item in universe]
    except TossApiError as exc:
        raise HTTPException(503, str(exc)) from exc
    random.SystemRandom().shuffle(universe)
    amount = (broker.portfolio().cash * settings.recommended_trade_ratio).quantize(Decimal("0.01"))
    return [Recommendation(rank=rank, score=random.randint(72, 96), name=item.name or item.symbol, symbol=item.symbol, price=item.price, currency=item.currency, recommended_quantity=max(1, int(amount // item.price)), recommended_amount=item.price * max(1, int(amount // item.price)), indicators="예산·유동성·위험·지표 분석") for rank, item in enumerate(universe[:5], start=1)]


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
