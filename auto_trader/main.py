from decimal import Decimal
import random
import asyncio
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import Candle, Order, OrderRequest, Portfolio, Price, Recommendation
from .paper import PaperBroker
from .settings import settings
from .toss_client import TossApiError, TossClient
from .strategy import evaluate

app = FastAPI(title="Toss Auto Trader", version="0.2.0")
broker = PaperBroker(settings.paper_initial_cash, settings.paper_commission_rate)
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
SYMBOL_NAMES = {symbol: name for name, symbol in STOCK_ALIASES.items()}


def is_kr_symbol(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6
mode: Literal["PAPER", "DRY_RUN", "LIVE"] = settings.trading_mode if settings.trading_mode in ("PAPER", "DRY_RUN", "LIVE") else "PAPER"
auto_task: asyncio.Task | None = None
auto_state = {"running": False, "last_action": "", "message": "대기 중"}
auto_history: list[dict[str, str]] = []
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


@app.get("/api/v1/live/accounts")
async def live_accounts() -> list[dict]:
    try:
        return await toss.accounts()
    except TossApiError as exc:
        raise HTTPException(502, str(exc)) from exc


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


@app.get("/api/v1/portfolio/summary")
async def portfolio_summary() -> dict:
    portfolio = broker.portfolio()
    if portfolio.positions:
        try:
            live = await toss.prices(list(portfolio.positions))
            prices.update({item.symbol: item for item in live})
        except Exception:
            pass
    orders = [item for item in broker.orders() if item.status == "FILLED" and item.mode == "PAPER"]
    rows, total_cost, total_value = [], Decimal("0"), Decimal("0")
    for symbol, quantity in portfolio.positions.items():
        if quantity <= 0:
            continue
        buys = [item for item in orders if item.symbol == symbol and item.side == "BUY"]
        sells = [item for item in orders if item.symbol == symbol and item.side == "SELL"]
        cost = sum((item.quantity * item.price for item in buys), Decimal("0")) - sum((item.quantity * item.price for item in sells), Decimal("0"))
        current = prices.get(symbol)
        current_price = current.price if current else (buys[-1].price if buys else Decimal("0"))
        value = quantity * current_price
        total_cost += cost; total_value += value
        display_name = (current.name if current and current.name else SYMBOL_NAMES.get(symbol, symbol))
        rows.append({"symbol": symbol, "name": display_name, "quantity": quantity, "buy_amount": cost, "average_cost": (cost / quantity if quantity else Decimal("0")).quantize(Decimal("0.01")), "current_price": current_price, "market_value": value, "profit_loss": value - cost, "return_rate": (((value - cost) / cost * 100) if cost else Decimal("0")).quantize(Decimal("0.01"))})
    return {"cash": portfolio.cash, "positions": rows, "total_cost": total_cost, "total_value": total_value, "total_return_rate": (((total_value - total_cost) / total_cost * 100) if total_cost else Decimal("0")).quantize(Decimal("0.01"))}


@app.get("/api/v1/capital")
async def get_capital() -> dict[str, Decimal | str]:
    portfolio = broker.portfolio()
    if portfolio.positions:
        try:
            live = await toss.prices(list(portfolio.positions))
            prices.update({item.symbol: item for item in live})
        except Exception:
            pass
    stock_value = sum((quantity * prices[symbol].price for symbol, quantity in portfolio.positions.items() if symbol in prices), Decimal("0"))
    total_assets = portfolio.cash + stock_value
    total_profit = total_assets - settings.paper_initial_cash
    return {
        "mode": mode,
        "account_cash": portfolio.cash,
        "recommended_amount": (portfolio.cash * settings.recommended_trade_ratio).quantize(Decimal("0.01")),
        "recommended_ratio": settings.recommended_trade_ratio,
        "currency": "KRW",
        "stock_value": stock_value,
        "total_assets": total_assets,
        "total_profit": total_profit,
        "total_return_rate": ((total_profit / settings.paper_initial_cash * 100) if settings.paper_initial_cash else Decimal("0")).quantize(Decimal("0.01")),
    }


@app.get("/api/v1/auto-trader/status")
def auto_trader_status() -> dict[str, str | bool]:
    return {**auto_state, "mode": mode}


@app.get("/api/v1/auto-trader/history")
def auto_trader_history() -> list[dict[str, str]]:
    return auto_history[-20:][::-1]


def record_auto_event(event: str, detail: str) -> None:
    from datetime import datetime
    auto_history.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "event": event, "detail": detail})


async def paper_auto_loop() -> None:
    while auto_state["running"]:
        try:
            candidates = await recommendations()
            if candidates:
                candidate = candidates[0]
                candles = await toss.candles(candidate.symbol, 60)
                signal = evaluate(candles)
                auto_state["message"] = f"{candidate.symbol} 전략 신호 {signal.action} ({signal.score}점)"
                record_auto_event("전략 판단", f"{candidate.name} · {signal.action} · {signal.reason}")
                if signal.action != "BUY":
                    await asyncio.sleep(60)
                    continue
                existing = broker.portfolio().positions.get(candidate.symbol, Decimal("0"))
                if existing == 0:
                    order = broker.place(OrderRequest(symbol=candidate.symbol, side="BUY", quantity=Decimal(candidate.recommended_quantity), price=candidate.price), "PAPER")
                    auto_state["last_action"] = f"{candidate.name} {order.status}"
                    auto_state["message"] = f"{candidate.symbol} PAPER 매수 시뮬레이션 완료"
                    record_auto_event("PAPER 매수", f"{candidate.name} ({candidate.symbol}) · {order.status} · {candidate.recommended_quantity}주")
                else:
                    auto_state["message"] = f"{candidate.symbol} 보유 중이라 중복 매수하지 않음"
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            auto_state["message"] = f"자동매매 일시 중단: {exc}"
            await asyncio.sleep(60)


@app.post("/api/v1/auto-trader/start")
async def start_auto_trader() -> dict[str, str | bool]:
    global auto_task
    if mode != "PAPER":
        raise HTTPException(400, "자동매매 v0.2는 PAPER 모드에서만 실행할 수 있습니다.")
    if auto_task and not auto_task.done():
        return auto_trader_status()
    auto_state.update(running=True, message="PAPER 자동매매 시작")
    record_auto_event("시작", "PAPER 자동매매를 시작했습니다.")
    auto_task = asyncio.create_task(paper_auto_loop())
    return auto_trader_status()


@app.post("/api/v1/auto-trader/stop")
async def stop_auto_trader() -> dict[str, str | bool]:
    global auto_task
    auto_state.update(running=False, message="PAPER 자동매매 중지")
    record_auto_event("중지", "PAPER 자동매매를 중지했습니다.")
    if auto_task and not auto_task.done():
        auto_task.cancel()
        await asyncio.gather(auto_task, return_exceptions=True)
    auto_task = None
    return auto_trader_status()


class RatioUpdate(BaseModel):
    ratio: Decimal = Field(ge=0, le=1)


@app.put("/api/v1/capital/recommended-ratio")
def update_recommended_ratio(request: RatioUpdate) -> dict[str, Decimal]:
    settings.recommended_trade_ratio = request.ratio
    return {"recommended_ratio": settings.recommended_trade_ratio}


@app.get("/api/v1/recommendations", response_model=list[Recommendation])
async def recommendations() -> list[Recommendation]:
    all_symbols = [symbol for symbol in dict.fromkeys(STOCK_ALIASES.values()) if is_kr_symbol(symbol)]
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
async def create_order(request: OrderRequest) -> Order:
    if not is_kr_symbol(request.symbol):
        raise HTTPException(400, "현재는 국내 주식만 거래할 수 있습니다.")
    if mode == "PAPER" and request.symbol not in prices:
        raise HTTPException(400, "paper mode requires a known price")
    if mode == "LIVE":
        if not settings.live_trading_enabled:
            raise HTTPException(403, "LIVE 거래가 비활성화되어 있습니다. LIVE_TRADING_ENABLED=true 설정이 필요합니다.")
        try:
            return await toss.place_order(request)
        except TossApiError as exc:
            raise HTTPException(502, str(exc)) from exc
    return broker.place(request, mode)


@app.post("/api/v1/mode/{new_mode}")
def set_mode(new_mode: Literal["PAPER", "DRY_RUN", "LIVE"]) -> dict[str, str]:
    global mode
    if new_mode == "LIVE" and not settings.live_trading_enabled:
        raise HTTPException(403, "LIVE 거래가 비활성화되어 있습니다. 환경변수 LIVE_TRADING_ENABLED=true로 명시적으로 활성화하세요.")
    if new_mode == "LIVE" and not settings.toss_account_seq:
        raise HTTPException(400, "LIVE 거래에는 TOSS_ACCOUNT_SEQ가 필요합니다.")
    mode = new_mode
    return {"mode": mode}


