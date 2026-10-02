from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import random
import asyncio
from typing import Literal
from datetime import datetime, timedelta
from time import monotonic
import sqlite3
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import Candle, Order, OrderRequest, Portfolio, Price, Recommendation, StrategyBacktestRequest
from .market_calendar import allows_regular_order
from .market_rules import validate_live_market_data
from .live_journal import LiveJournal, OPEN_STATUSES
from .operations import OperationsStore, SingleInstanceLock
from .paper import PaperBroker
from .risk import RiskGuard
from .settings import settings
from .toss_client import TossApiError, TossClient
from .strategy import backtest as run_backtest, evaluate

app = FastAPI(title="Toss Auto Trader", version="0.2.0")
broker = PaperBroker(settings.paper_initial_cash, settings.paper_commission_rate)
risk_guard = RiskGuard()
toss = TossClient()
live_journal = LiveJournal()
operations = OperationsStore()
instance_lock = SingleInstanceLock()
live_reconciliation = {"ok": False, "message": "실계좌 주문 재동기화 전", "checked_at": ""}
live_order_lock = asyncio.Lock()
live_monitor_task: asyncio.Task | None = None
prices: dict[str, Price] = {}
_kr_calendar_cache: tuple[str, float, dict] | None = None


def cache_prices(items: list[Price]) -> None:
    prices.update({item.symbol: item for item in items})


def krx_tick_size(price: Decimal) -> Decimal:
    if price < 2000: return Decimal("1")
    if price < 5000: return Decimal("5")
    if price < 20000: return Decimal("10")
    if price < 50000: return Decimal("50")
    if price < 200000: return Decimal("100")
    if price < 500000: return Decimal("500")
    return Decimal("1000")


def validate_paper_price(price: Decimal, candles: list[Candle] | None = None, *, check_gap: bool = True) -> None:
    tick = krx_tick_size(price)
    if price % tick != 0:
        raise HTTPException(400, f"주문 가격 {price}원이 호가 단위({tick}원)에 맞지 않습니다.")
    if candles and check_gap:
        reference = Decimal(candles[0].close)
        if reference > 0 and abs(price - reference) / reference > settings.paper_max_slippage_rate:
            raise HTTPException(409, "현재가와 최근 일봉 종가 차이가 허용 범위를 넘어 주문을 보류했습니다. 시세를 새로 조회하세요.")


def simulated_fill_price(price: Decimal, side: str) -> Decimal:
    tick = krx_tick_size(price)
    adjusted = price * (Decimal("1") + settings.paper_simulated_slippage_rate if side == "BUY" else Decimal("1") - settings.paper_simulated_slippage_rate)
    ticks = adjusted / tick
    rounded_ticks = ticks.to_integral_value(rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR)
    return rounded_ticks * tick


def should_flatten_before_close(now: datetime | None = None) -> bool:
    if not settings.force_flatten_at_close:
        return False
    seoul = ZoneInfo("Asia/Seoul")
    local_now = (now or datetime.now(seoul)).astimezone(seoul)
    if local_now.weekday() >= 5:
        return False
    close_minutes = 15 * 60 + 30
    current_minutes = local_now.hour * 60 + local_now.minute
    return close_minutes - settings.flatten_before_close_minutes <= current_minutes < close_minutes


def completed_trading_days(opened_at: datetime, now: datetime) -> int:
    start, end = opened_at.date(), now.date()
    total = 0
    while start < end:
        start += timedelta(days=1)
        if start.weekday() < 5:
            total += 1
    return total


def is_regular_market_hours(now: datetime | None = None) -> bool:
    seoul = ZoneInfo("Asia/Seoul")
    local_now = (now or datetime.now(seoul)).astimezone(seoul)
    return local_now.weekday() < 5 and (local_now.hour, local_now.minute) >= (9, 0) and (local_now.hour, local_now.minute) < (15, 30)


async def kr_market_day(day: str) -> dict:
    global _kr_calendar_cache
    if _kr_calendar_cache and _kr_calendar_cache[0] == day and monotonic() - _kr_calendar_cache[1] < 60:
        return _kr_calendar_cache[2]
    calendar_day = await toss.market_calendar_kr(day)
    _kr_calendar_cache = (day, monotonic(), calendar_day)
    return calendar_day


async def market_session_allows(side: str, now: datetime | None = None) -> bool:
    local_now = (now or datetime.now(ZoneInfo("Asia/Seoul"))).astimezone(ZoneInfo("Asia/Seoul"))
    try:
        today = await kr_market_day(local_now.date().isoformat())
    except Exception:
        return False
    return allows_regular_order(today, side, local_now)


async def paper_equity_and_exposure() -> tuple[Decimal, Decimal]:
    portfolio = broker.portfolio()
    held_symbols = [symbol for symbol, quantity in portfolio.positions.items() if quantity > 0]
    if held_symbols:
        quotes = await toss.prices(held_symbols)
        cache_prices(quotes)
        quoted = {item.symbol: item.price for item in quotes}
        if any(symbol not in quoted for symbol in held_symbols):
            raise TossApiError("보유 종목의 최신 현재가가 모두 확인되지 않았습니다.")
    else:
        quoted = {}
    exposure = sum((quantity * quoted[symbol] for symbol, quantity in portfolio.positions.items() if symbol in quoted), Decimal("0"))
    return portfolio.cash + exposure, exposure


async def live_account_snapshot() -> dict:
    """Build a fresh KRW-valued risk snapshot from Toss account endpoints."""
    open_orders = await toss.open_orders()
    if open_orders:
        raise TossApiError("실계좌에 미체결 주문이 있어 가용 잔고·노출을 확정할 수 없습니다.")
    holdings = await toss.holdings()
    cash_krw = await toss.buying_power("KRW")
    items = holdings["items"]
    market_value = holdings.get("marketValue", {}).get("amount", {})
    if not isinstance(market_value, dict) or "krw" not in market_value:
        raise TossApiError("실계좌 요약 평가금액에 필수 KRW 값이 없습니다.")
    holdings_krw = Decimal(str(market_value["krw"]))
    holdings_usd = Decimal(str(market_value.get("usd") or "0"))
    cash_usd = await toss.buying_power("USD")
    if cash_krw < 0 or cash_usd < 0 or holdings_krw < 0 or holdings_usd < 0:
        raise TossApiError("실계좌 잔고 또는 평가금액이 음수라 안전 한도를 계산할 수 없습니다.")
    fx = await toss.exchange_rate_usd_krw() if holdings_usd or cash_usd else Decimal("0")
    equity = cash_krw + holdings_krw + (cash_usd + holdings_usd) * fx
    positions: dict[str, Decimal] = {}
    symbol_values: dict[str, Decimal] = {}
    open_positions = 0
    for item in items:
        symbol = str(item.get("symbol", "")).upper()
        quantity = Decimal(str(item.get("quantity", "0")))
        currency = item.get("currency")
        if quantity <= 0:
            continue
        open_positions += 1
        item_value = item.get("marketValue", {}).get("amount")
        if item_value is None:
            raise TossApiError(f"{symbol} 보유 평가금액이 누락되어 안전 한도를 계산할 수 없습니다.")
        value = Decimal(str(item_value))
        if value < 0:
            raise TossApiError(f"{symbol} 보유 평가금액이 음수라 안전 한도를 계산할 수 없습니다.")
        if currency == "KRW":
            positions[symbol] = quantity
            symbol_values[symbol] = value
        elif currency == "USD":
            symbol_values[symbol] = value * fx
        else:
            raise TossApiError(f"위험 계산에서 지원하지 않는 보유 통화입니다: {currency}")
    exposure = holdings_krw + holdings_usd * fx
    return {
        "equity": equity,
        "exposure": exposure,
        "cash_krw": cash_krw,
        "positions": positions,
        "symbol_values": symbol_values,
        "open_positions": open_positions,
    }


async def reconcile_live_orders() -> dict:
    """Read remote order detail and persist cumulative fills before any LIVE use."""
    remote_open = await toss.open_orders()
    unresolved = live_journal.unresolved_intents()
    if any(not item["order_id"] for item in unresolved):
        raise TossApiError("주문 ID를 모르는 미확정 실계좌 주문 의도가 있습니다. 수동 대조가 필요합니다.")
    order_ids = {item["order_id"] for item in unresolved} | live_journal.unfinished_order_ids()
    for item in remote_open:
        order_id = item.get("orderId")
        if not isinstance(order_id, str) or not order_id:
            raise TossApiError("미체결 주문 식별자를 확인할 수 없습니다.")
        if item.get("currency") != "KRW":
            raise TossApiError("미체결 해외 주문이 있어 실계좌 상태를 확정할 수 없습니다.")
        order_ids.add(order_id)
    for order_id in sorted(order_ids):
        live_journal.apply_detail(await toss.order_detail(order_id))
    result = {"ok": True, "open_orders": len(remote_open), "checked_orders": len(order_ids),
              "message": "원격 주문 상세와 누적 체결을 동기화했습니다.",
              "checked_at": datetime.now(ZoneInfo("UTC")).isoformat()}
    live_reconciliation.update(result)
    return result


async def live_reconciliation_loop() -> None:
    while True:
        await asyncio.sleep(30)
        was_ok = bool(live_reconciliation["ok"])
        try:
            async with live_order_lock:
                await reconcile_live_orders()
            if not was_ok:
                operations.add_event("실계좌 주문 재동기화 복구", "원격 주문 상세와 체결 상태를 다시 확인했습니다.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc)
            changed = was_ok or live_reconciliation["message"] != message
            live_reconciliation.update(ok=False, message=message, checked_at=datetime.now(ZoneInfo("UTC")).isoformat())
            if changed:
                operations.add_event("실계좌 주문 재동기화 실패", message, "warning")


async def daily_risk_snapshot(account_mode: str | None = None) -> dict[str, Decimal | str | bool]:
    account_mode = account_mode or mode
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    calendar_day = now.date().isoformat()
    day = f"LIVE:{calendar_day}" if account_mode == "LIVE" else calendar_day
    snapshot = risk_guard.daily_state(day)
    if snapshot is None:
        # A baseline first captured after the session opens could hide losses
        # earlier that day. Establish it only before the KRX session; otherwise
        # fail closed until the next eligible pre-market snapshot.
        try:
            market_day = await kr_market_day(calendar_day)
        except TossApiError as exc:
            raise RuntimeError(f"장 시작 전 캘린더 확인 실패: {exc}") from exc
        integrated = market_day.get("integrated")
        if not isinstance(integrated, dict) or not integrated.get("regularMarket") or (now.hour, now.minute) >= (9, 0):
            raise RuntimeError("오늘 장 시작 전 위험 기준금액이 저장되지 않았습니다. 오늘 신규 매수는 차단되며, 다음 영업일 09:00 전에 기준금액을 확보해야 합니다.")
        if account_mode == "LIVE":
            equity = Decimal(str((await live_account_snapshot())["equity"]))
        else:
            equity, _ = await paper_equity_and_exposure()
        snapshot = risk_guard.start_day_if_needed(day, str(equity))
    else:
        if account_mode == "LIVE":
            equity = Decimal(str((await live_account_snapshot())["equity"]))
        else:
            equity, _ = await paper_equity_and_exposure()
    opening = Decimal(str(snapshot["opening_equity"]))
    pnl = equity - opening
    loss_limit = opening * settings.daily_loss_limit_rate
    halted = bool(snapshot["halted"])
    reason = str(snapshot["reason"])
    if pnl <= -loss_limit and not halted:
        reason = f"일일 손실 한도 도달: {pnl} KRW"
        risk_guard.halt_day(day, reason)
        halted = True
    return {"day": day, "equity": equity, "opening_equity": opening, "pnl": pnl, "loss_limit": loss_limit, "halted": halted, "reason": reason}


async def enforce_order_safety(request: OrderRequest, account_mode: str | None = None) -> None:
    account_mode = account_mode or mode
    current = datetime.now(ZoneInfo("Asia/Seoul"))
    if not await market_session_allows(request.side, current):
        raise HTTPException(423, "토스 국내 장 캘린더 기준 주문 가능 시간이 아니거나 휴장일입니다. 캘린더를 확인할 수 없는 경우에도 주문을 차단합니다.")
    if request.side == "BUY":
        emergency = risk_guard.emergency_state()
        if emergency["active"]:
            raise HTTPException(423, f"비상 정지 상태라 신규 매수를 차단했습니다. {emergency['reason']}")
        try:
            daily = await daily_risk_snapshot(account_mode)
        except Exception as exc:
            raise HTTPException(503, f"위험 한도 계산에 필요한 현재가를 확인할 수 없어 매수를 보류했습니다: {exc}") from exc
        if daily["halted"]:
            raise HTTPException(423, f"일일 손실 한도 초과로 신규 매수가 중단되었습니다. {daily['reason']}")
        amount = request.quantity * request.price
        if amount > settings.max_order_amount:
            raise HTTPException(409, f"주문 금액이 주문 한도 {settings.max_order_amount}원을 초과합니다.")
        try:
            if account_mode == "LIVE":
                account = await live_account_snapshot()
                current_quantity = account["positions"].get(request.symbol, Decimal("0"))
                symbol_exposure = account["symbol_values"].get(request.symbol, Decimal("0")) + amount * (Decimal("1") + settings.live_fee_reserve_rate)
                exposure = account["exposure"]
                open_positions = account["open_positions"]
                required_cash = amount * (Decimal("1") + settings.live_fee_reserve_rate)
                if required_cash > account["cash_krw"]:
                    raise HTTPException(409, "주문 금액과 수수료 예비분이 실계좌 매수 가능 원화를 초과합니다.")
            else:
                portfolio = broker.portfolio()
                _, exposure = await paper_equity_and_exposure()
                current_quantity = portfolio.positions.get(request.symbol, Decimal("0"))
                current_price = prices[request.symbol].price if request.symbol in prices else request.price
                symbol_exposure = current_quantity * current_price + amount
                open_positions = len([q for q in portfolio.positions.values() if q > 0])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"최신 계좌·보유 한도를 확인할 수 없어 매수를 보류했습니다: {exc}") from exc
        if current_quantity == 0 and open_positions >= settings.max_open_positions:
            raise HTTPException(409, f"최대 보유 종목 수({settings.max_open_positions})에 도달했습니다.")
        if symbol_exposure > settings.max_symbol_exposure_amount:
            raise HTTPException(409, f"종목별 보유 한도 {settings.max_symbol_exposure_amount}원을 초과합니다.")
        if current_quantity + request.quantity > settings.max_symbol_quantity:
            raise HTTPException(409, f"종목별 최대 보유 수량 {settings.max_symbol_quantity}주를 초과합니다.")
        exposure_order_amount = amount * (Decimal("1") + settings.live_fee_reserve_rate) if account_mode == "LIVE" else amount
        if exposure + exposure_order_amount > settings.max_portfolio_exposure_amount:
            raise HTTPException(409, f"전체 보유 한도 {settings.max_portfolio_exposure_amount}원을 초과합니다.")
    elif account_mode == "LIVE":
        try:
            account = await live_account_snapshot()
            held_quantity = account["positions"].get(request.symbol, Decimal("0"))
            if request.quantity > held_quantity:
                raise HTTPException(409, "실계좌 보유 수량을 초과하는 매도는 허용하지 않습니다.")
            sellable = await toss.sellable_quantity(request.symbol)
            if request.quantity > sellable:
                raise HTTPException(409, "실계좌 매도 가능 수량을 초과합니다.")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, f"실계좌 매도 가능 수량을 확인할 수 없어 매도를 보류했습니다: {exc}") from exc
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
mode: Literal["PAPER", "DRY_RUN", "LIVE"] = settings.trading_mode if settings.trading_mode in ("PAPER", "DRY_RUN") else "PAPER"
auto_task: asyncio.Task | None = None
auto_state = {"running": False, "last_action": "", "message": "대기 중"}
auto_history: list[dict[str, str]] = []
app.mount("/static", StaticFiles(directory="auto_trader/static"), name="static")


@app.on_event("startup")
async def initialize_daily_risk_baseline() -> None:
    global live_monitor_task
    instance_lock.acquire()
    auto_state.update(operations.restore_stopped())
    auto_history[:] = operations.recent_events(20)[::-1]
    if settings.toss_account_seq:
        try:
            await reconcile_live_orders()
        except Exception as exc:
            live_reconciliation.update(ok=False, message=str(exc), checked_at=datetime.now(ZoneInfo("UTC")).isoformat())
            operations.add_event("실계좌 주문 재동기화 실패", str(exc), "warning")
        live_monitor_task = asyncio.create_task(live_reconciliation_loop())
    try:
        await daily_risk_snapshot()
    except Exception:
        # A new buy remains blocked until the equity snapshot becomes available.
        pass
    if settings.live_trading_enabled and settings.toss_account_seq:
        try:
            await daily_risk_snapshot("LIVE")
        except Exception:
            # Never replace a missing real-account baseline with PAPER equity.
            # LIVE remains locked when the account snapshot cannot be verified.
            pass


@app.on_event("shutdown")
async def shutdown_runtime() -> None:
    global live_monitor_task
    if live_monitor_task and not live_monitor_task.done():
        live_monitor_task.cancel()
        await asyncio.gather(live_monitor_task, return_exceptions=True)
    live_monitor_task = None
    if auto_task and not auto_task.done():
        auto_task.cancel()
        await asyncio.gather(auto_task, return_exceptions=True)
    operations.save_auto_state(auto_state)
    instance_lock.release()


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse("auto_trader/static/index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": mode}


@app.get("/api/v1/risk/status")
async def risk_status() -> dict:
    emergency = risk_guard.emergency_state()
    market_open = await market_session_allows("SELL")
    live_ready = bool(mode == "LIVE" and settings.live_trading_enabled and live_reconciliation["ok"]
                      and not live_journal.unresolved_intents() and not emergency["active"])
    base = {
        "emergency_stop": emergency["active"],
        "emergency_reason": emergency["reason"],
        "mode": mode,
        "market_open": market_open,
        "live_order_ready": live_ready,
        "live_block_reason": "PAPER 모드이거나 LIVE 설정·주문 재동기화가 완료되지 않았습니다." if not live_ready else "",
        "limits": {
            "daily_loss_limit_rate": settings.daily_loss_limit_rate,
            "max_order_amount": settings.max_order_amount,
            "max_symbol_exposure_amount": settings.max_symbol_exposure_amount,
            "max_symbol_quantity": settings.max_symbol_quantity,
            "max_portfolio_exposure_amount": settings.max_portfolio_exposure_amount,
            "max_open_positions": settings.max_open_positions,
            "live_fee_reserve_rate": settings.live_fee_reserve_rate,
        },
    }
    try:
        daily = await daily_risk_snapshot()
        return {**base, **daily, "live_order_ready": live_ready and not daily["halted"]}
    except Exception as exc:
        return {**base, "live_order_ready": False, "daily_risk_available": False, "risk_error": str(exc)}


@app.post("/api/v1/risk/emergency-stop")
async def activate_emergency_stop() -> dict[str, bool | str]:
    global auto_task
    reason = "사용자 비상 정지"
    risk_guard.activate_emergency_stop(reason)
    auto_state.update(running=False, message="비상 정지 활성화: 자동매매가 중지되었습니다.")
    record_auto_event("비상 정지", reason)
    if auto_task and not auto_task.done():
        auto_task.cancel()
        await asyncio.gather(auto_task, return_exceptions=True)
    auto_task = None
    return {"active": True, "reason": reason}


@app.post("/api/v1/risk/emergency-stop/clear")
def clear_emergency_stop(confirm: bool = False) -> dict[str, bool]:
    if not confirm:
        raise HTTPException(400, "비상 정지를 해제하려면 confirm=true를 명시해야 합니다.")
    risk_guard.clear_emergency_stop()
    record_auto_event("비상 정지 해제", "사용자가 비상 정지를 해제했습니다.")
    return {"active": False}


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


@app.get("/api/v1/live/snapshot")
async def live_snapshot() -> dict:
    """Display actual account data separately from the PAPER dashboard."""
    try:
        holdings = await toss.holdings()
        cash_krw = await toss.buying_power("KRW")
        cash_usd = await toss.buying_power("USD")
        remote_open = await toss.open_orders()
    except TossApiError as exc:
        raise HTTPException(502, str(exc)) from exc
    rows = []
    for item in holdings["items"]:
        rows.append({"symbol": item.get("symbol"), "name": item.get("name"),
                     "currency": item.get("currency"), "quantity": item.get("quantity"),
                     "average_purchase_price": item.get("averagePurchasePrice"),
                     "market_value": item.get("marketValue", {}).get("amount"),
                     "profit_loss": item.get("profitLoss", {}).get("amountAfterCost")})
    return {"source": "TOSS_LIVE_READ_ONLY", "cash_krw": cash_krw, "cash_usd": cash_usd,
            "holdings_value": holdings.get("marketValue", {}).get("amount", {}),
            "positions": rows, "open_order_count": len(remote_open),
            "paper_cash_for_comparison": broker.portfolio().cash,
            "observed_at": datetime.now(ZoneInfo("UTC")).isoformat()}


@app.post("/api/v1/live/orders/reconcile")
async def reconcile_live_orders_api() -> dict:
    try:
        async with live_order_lock:
            return await reconcile_live_orders()
    except (TossApiError, ValueError) as exc:
        live_reconciliation.update(ok=False, message=str(exc), checked_at=datetime.now(ZoneInfo("UTC")).isoformat())
        operations.add_event("실계좌 주문 재동기화 실패", str(exc), "warning")
        raise HTTPException(503, str(exc)) from exc


class UnknownOrderResolution(BaseModel):
    order_id: str = Field(min_length=1, max_length=200)


@app.post("/api/v1/live/orders/intents/{client_order_id}/attach")
async def attach_unknown_live_order(client_order_id: str, request: UnknownOrderResolution, confirm: bool = False) -> dict:
    """Manually attach a Toss order ID to an unknown submission after review."""
    if not confirm:
        raise HTTPException(400, "실계좌 주문 기록을 대조한 뒤 confirm=true를 명시하세요.")
    async with live_order_lock:
        intent = live_journal.intent(client_order_id)
        if not intent or intent["order_id"] is not None:
            raise HTTPException(404, "주문 ID가 없는 미확정 의도를 찾을 수 없습니다.")
        try:
            remote = await toss.order_detail(request.order_id)
            ordered_at = datetime.fromisoformat(remote["orderedAt"])
            created_at = datetime.fromisoformat(intent["created_at"])
            if ordered_at.tzinfo is None or not (-5 <= (ordered_at - created_at).total_seconds() <= 900):
                raise ValueError("원격 주문 시각이 주문 의도와 일치하지 않습니다.")
            if (remote.get("symbol") != intent["symbol"] or remote.get("side") != intent["side"]
                    or remote.get("currency") != "KRW" or remote.get("orderType") != "LIMIT"
                    or Decimal(str(remote.get("quantity"))) != Decimal(intent["quantity"])
                    or Decimal(str(remote.get("price"))) != Decimal(intent["price"])):
                raise ValueError("원격 주문의 종목·방향·수량·가격이 주문 의도와 다릅니다.")
            live_journal.link_order(client_order_id, request.order_id)
            observed = live_journal.apply_detail(remote)
            await reconcile_live_orders()
        except (TossApiError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(409, f"주문 대조 실패: {exc}") from exc
        operations.add_event("미확정 LIVE 주문 대조", f"{client_order_id} · {request.order_id} · {observed['status']}", "warning")
        return observed


@app.get("/api/v1/live/orders/journal")
def live_orders_journal() -> dict:
    return {"reconciliation": live_reconciliation, "unresolved_intents": live_journal.unresolved_intents(),
            "orders": live_journal.recent_orders()}


@app.post("/api/v1/live/orders/{order_id}/cancel")
async def cancel_live_order(order_id: str, confirm_live: bool = False) -> dict:
    if mode != "LIVE" or not settings.live_trading_enabled or not confirm_live:
        raise HTTPException(403, "LIVE 모드와 명시적 취소 확인이 필요합니다.")
    async with live_order_lock:
        tracked = live_journal.order(order_id)
        if tracked is None or tracked["status"] not in OPEN_STATUSES:
            raise HTTPException(409, "추적 중인 미완료 실계좌 주문만 취소할 수 있습니다.")
        live_reconciliation.update(ok=False, message="실계좌 주문 취소 결과 확인 중")
        try:
            cancel_id = await toss.cancel_order(order_id)
            observed = live_journal.apply_detail(await toss.order_detail(order_id))
            await reconcile_live_orders()
        except Exception as exc:
            operations.add_event("LIVE 취소 확인 실패", f"{order_id} · 결과 미확정: {exc}", "error")
            raise HTTPException(503, "취소 결과가 미확정입니다. 재요청 전에 실계좌 주문 상태를 확인하세요.") from exc
        operations.add_event("LIVE 주문 취소 확인", f"{order_id} · {observed['status']} · 체결 {observed['filled_quantity']}주")
        return {"cancel_order_id": cancel_id, "original_order": observed}


@app.post("/api/v1/live/orders/preflight")
async def live_order_preflight(request: OrderRequest) -> dict:
    """Validate a proposed LIVE order without submitting it."""
    if not is_kr_symbol(request.symbol) or request.quantity != request.quantity.to_integral_value():
        raise HTTPException(400, "국내 주식 코드와 정수 수량만 허용합니다.")
    if not live_reconciliation["ok"] or live_journal.unresolved_intents():
        raise HTTPException(423, "실계좌 주문 재동기화가 완료되지 않았습니다.")
    try:
        stock = await toss.stock_details(request.symbol)
        limits = await toss.price_limits(request.symbol)
        warnings = await toss.stock_warnings(request.symbol) if request.side == "BUY" else []
        commissions = await toss.commissions()
        await enforce_order_safety(request, "LIVE")
        quotes = await toss.prices([request.symbol])
        if len(quotes) != 1:
            raise ValueError("최신 현재가가 한 건으로 확인되지 않았습니다.")
        checked = validate_live_market_data(request, quotes[0], stock, limits, warnings, commissions)
        if checked["commission_rate"] > settings.live_fee_reserve_rate:
            raise ValueError("계좌 수수료율이 LIVE 수수료 예비율보다 높습니다.")
    except (TossApiError, ValueError) as exc:
        raise HTTPException(503, f"실계좌 주문 사전검증 실패: {exc}") from exc
    return {"order_submission": False, "source": "TOSS_LIVE", **checked}


@app.get("/api/v1/operations/status")
def operations_status() -> dict:
    return {"mode": mode, "auto_running": auto_state["running"], "emergency_stop": risk_guard.emergency_state(),
            "live_reconciliation": live_reconciliation, "alerts_today": operations.alert_count(),
            "recent_events": operations.recent_events(20)}


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
            cache_prices(live_prices)
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


@app.post("/api/v1/strategy/backtest")
async def strategy_backtest(request: StrategyBacktestRequest) -> dict:
    try:
        candles = await toss.candles(request.symbol, request.count)
    except TossApiError as exc:
        raise HTTPException(503, str(exc)) from exc
    result = run_backtest(candles, request.initial_cash, settings.paper_commission_rate, settings.paper_simulated_slippage_rate)
    if "error" in result:
        raise HTTPException(422, result["error"])
    result["symbol"] = request.symbol
    result["commission_rate"] = settings.paper_commission_rate
    return result


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
    live_by_symbol: dict[str, Price] = {}
    if portfolio.positions:
        try:
            live = await toss.prices(list(portfolio.positions))
            live_by_symbol = {item.symbol: item for item in live}
            if any(symbol not in live_by_symbol for symbol in portfolio.positions):
                raise TossApiError("보유 종목의 최신 현재가가 모두 확인되지 않았습니다.")
            cache_prices(live)
        except Exception as exc:
            raise HTTPException(503, f"보유 주식 평가에 필요한 시세를 확인할 수 없습니다: {exc}") from exc
    rows, total_cost, total_value = [], Decimal("0"), Decimal("0")
    for symbol, quantity in portfolio.positions.items():
        if quantity <= 0:
            continue
        average_cost = broker.position_cost_basis(symbol)
        cost = quantity * average_cost
        current = live_by_symbol[symbol]
        current_price = current.price
        value = quantity * current_price
        total_cost += cost; total_value += value
        display_name = (current.name if current and current.name else SYMBOL_NAMES.get(symbol, symbol))
        rows.append({"symbol": symbol, "name": display_name, "quantity": quantity, "buy_amount": cost, "average_cost": average_cost, "current_price": current_price, "market_value": value, "profit_loss": value - cost, "return_rate": (((value - cost) / cost * 100) if cost else Decimal("0")).quantize(Decimal("0.01"))})
    return {"cash": portfolio.cash, "positions": rows, "total_cost": total_cost, "total_value": total_value, "total_return_rate": (((total_value - total_cost) / total_cost * 100) if total_cost else Decimal("0")).quantize(Decimal("0.01"))}


@app.get("/api/v1/capital")
async def get_capital() -> dict[str, Decimal | str]:
    portfolio = broker.portfolio()
    live_by_symbol: dict[str, Price] = {}
    if portfolio.positions:
        try:
            live = await toss.prices(list(portfolio.positions))
            live_by_symbol = {item.symbol: item for item in live}
            if any(symbol not in live_by_symbol for symbol in portfolio.positions):
                raise TossApiError("보유 종목의 최신 현재가가 모두 확인되지 않았습니다.")
            cache_prices(live)
        except Exception as exc:
            raise HTTPException(503, f"전체 자산 평가에 필요한 시세를 확인할 수 없습니다: {exc}") from exc
    stock_value = sum((quantity * live_by_symbol[symbol].price for symbol, quantity in portfolio.positions.items()), Decimal("0"))
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
    return operations.recent_events(20)


def record_auto_event(event: str, detail: str) -> None:
    severity = "error" if "실패" in event or "오류" in event else "warning" if event in {"비상 정지", "매수 보류", "자동매매 중단"} else "info"
    operations.add_event(event, detail, severity)
    operations.save_auto_state(auto_state)
    auto_history.append({"time": datetime.now(ZoneInfo("UTC")).isoformat(), "event": event, "detail": detail, "severity": severity})


async def paper_auto_loop() -> None:
    while auto_state["running"]:
        try:
            if risk_guard.emergency_state()["active"]:
                auto_state.update(running=False, message="비상 정지로 자동매매를 멈췄습니다.")
                break
            daily_allows_entries = True
            try:
                daily_allows_entries = not bool((await daily_risk_snapshot())["halted"])
            except Exception:
                daily_allows_entries = False
            portfolio = broker.portfolio()
            sold_symbols: set[str] = set()
            exit_checks_ok = True
            flatten_now = should_flatten_before_close()
            for held_symbol, held_quantity in list(portfolio.positions.items()):
                if held_quantity <= 0:
                    continue
                try:
                    held_candles = await toss.candles(held_symbol, 60)
                    quotes = await toss.prices([held_symbol])
                    if not quotes:
                        exit_checks_ok = False
                        continue
                    if not held_candles and not flatten_now:
                        exit_checks_ok = False
                        continue
                    if len(held_candles) < 21 and not flatten_now:
                        exit_checks_ok = False
                        continue
                    quote = quotes[0]
                    cache_prices(quotes)
                    current_price = quote.price
                    validate_paper_price(current_price)
                    meta = broker.update_position_high_water(held_symbol, current_price)
                    opened_at = datetime.fromisoformat(meta["opened_at"])
                    if opened_at.tzinfo is None:
                        opened_at = opened_at.replace(tzinfo=ZoneInfo("UTC"))
                    holding_days = completed_trading_days(opened_at, datetime.now(ZoneInfo("UTC")))
                    entry_price = broker.position_cost_basis(held_symbol)
                    exit_signal = evaluate(
                        held_candles,
                        entry_price,
                        Decimal(meta["high_water"]),
                        holding_days,
                        broker.has_prior_sell(held_symbol),
                        current_price,
                    )
                    if flatten_now:
                        exit_signal.action, exit_signal.reason, exit_signal.sell_fraction = "SELL", "장 마감 전 청산", Decimal("1")
                except Exception:
                    exit_checks_ok = False
                    continue
                if exit_signal.action == "SELL":
                    sell_quantity = held_quantity
                    if exit_signal.sell_fraction < 1:
                        sell_quantity = max(Decimal("1"), (held_quantity * exit_signal.sell_fraction).to_integral_value(rounding=ROUND_FLOOR))
                        sell_quantity = min(sell_quantity, held_quantity)
                    fill_price = simulated_fill_price(current_price, "SELL")
                    if risk_guard.emergency_state()["active"]:
                        auto_state.update(running=False, message="비상 정지로 자동매매를 멈췄습니다.")
                        break
                    await enforce_order_safety(OrderRequest(symbol=held_symbol, side="SELL", quantity=sell_quantity, price=fill_price))
                    order = broker.place(OrderRequest(symbol=held_symbol, side="SELL", quantity=sell_quantity, price=fill_price), "PAPER")
                    auto_state["last_action"] = f"{held_symbol} {order.status}"
                    record_auto_event("PAPER 매도", f"{held_symbol} · {exit_signal.reason} · {order.status} · {sell_quantity}주")
                    if order.status == "FILLED":
                        sold_symbols.add(held_symbol)
            if not exit_checks_ok:
                auto_state["message"] = "보유 종목 시세 확인 실패로 이번 회차 신규 매수를 건너뜁니다."
                record_auto_event("매수 보류", "보유 종목의 매도 신호 점검을 완료하지 못함")
                await asyncio.sleep(60)
                continue
            if risk_guard.emergency_state()["active"]:
                auto_state.update(running=False, message="비상 정지로 자동매매를 멈췄습니다.")
                break
            if not daily_allows_entries:
                auto_state["message"] = "일일 손실 한도 또는 위험 데이터 확인 실패로 신규 매수를 건너뜁니다."
                record_auto_event("매수 보류", "일일 손실 한도 또는 위험 데이터 미확인")
                await asyncio.sleep(60)
                continue
            candidates = await recommendations()
            if candidates:
                candidate = candidates[0]
                if candidate.symbol in sold_symbols:
                    await asyncio.sleep(60)
                    continue
                candles = await toss.candles(candidate.symbol, 60)
                existing = broker.portfolio().positions.get(candidate.symbol, Decimal("0"))
                entry_price = broker.position_cost_basis(candidate.symbol) if existing > 0 else None
                signal = evaluate(candles, entry_price)
                auto_state["message"] = f"{candidate.symbol} 전략 신호 {signal.action} ({signal.score}점)"
                record_auto_event("전략 판단", f"{candidate.name} · {signal.action} · {signal.reason}")
                if signal.action != "BUY":
                    await asyncio.sleep(60)
                    continue
                if existing == 0:
                    quotes = await toss.prices([candidate.symbol])
                    if not quotes:
                        raise TossApiError("현재가를 확인할 수 없어 매수를 보류했습니다.")
                    quote = quotes[0]
                    cache_prices(quotes)
                    validate_paper_price(quote.price, candles)
                    average_volume = sum((Decimal(c.volume) for c in candles[1:21]), Decimal("0")) / Decimal("20")
                    if average_volume < settings.paper_min_average_daily_volume:
                        record_auto_event("매수 보류", f"{candidate.name} · 평균 거래량 부족")
                        await asyncio.sleep(60)
                        continue
                    average_turnover = sum((Decimal(c.volume) * Decimal(c.close) for c in candles[1:21]), Decimal("0")) / Decimal("20")
                    if average_turnover < settings.paper_min_average_daily_turnover:
                        record_auto_event("매수 보류", f"{candidate.name} · 평균 거래대금 부족")
                        await asyncio.sleep(60)
                        continue
                    fill_price = simulated_fill_price(quote.price, "BUY")
                    budget = broker.portfolio().cash * settings.recommended_trade_ratio
                    quantity = (budget / fill_price).to_integral_value(rounding=ROUND_FLOOR)
                    if quantity < 1:
                        record_auto_event("매수 보류", f"{candidate.name} · 추천예산으로 1주 매수 불가")
                        await asyncio.sleep(60)
                        continue
                    buy_request = OrderRequest(symbol=candidate.symbol, side="BUY", quantity=quantity, price=fill_price)
                    try:
                        await enforce_order_safety(buy_request)
                    except HTTPException as exc:
                        record_auto_event("매수 보류", str(exc.detail))
                        auto_state["message"] = str(exc.detail)
                        await asyncio.sleep(60)
                        continue
                    order = broker.place(buy_request, "PAPER")
                    auto_state["last_action"] = f"{candidate.name} {order.status}"
                    auto_state["message"] = f"{candidate.symbol} PAPER 매수 시뮬레이션 {order.status}"
                    record_auto_event("PAPER 매수", f"{candidate.name} ({candidate.symbol}) · {order.status} · {quantity}주")
                else:
                    auto_state["message"] = f"{candidate.symbol} 보유 중이라 중복 매수하지 않음"
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            auto_state["message"] = f"자동매매 일시 중단: {exc}"
            record_auto_event("자동매매 오류", str(exc))
            await asyncio.sleep(60)


@app.post("/api/v1/auto-trader/start")
async def start_auto_trader() -> dict[str, str | bool]:
    global auto_task
    if mode != "PAPER":
        raise HTTPException(400, "자동매매 v0.2는 PAPER 모드에서만 실행할 수 있습니다.")
    if risk_guard.emergency_state()["active"]:
        raise HTTPException(423, "비상 정지가 활성화되어 자동매매를 시작할 수 없습니다.")
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
async def create_order(request: OrderRequest, confirm_live: bool = False) -> Order:
    if not is_kr_symbol(request.symbol):
        raise HTTPException(400, "현재는 국내 주식만 거래할 수 있습니다.")
    if mode == "PAPER" and request.symbol not in prices:
        raise HTTPException(400, "paper mode requires a known price")
    if mode == "LIVE":
        if not settings.live_trading_enabled:
            raise HTTPException(403, "LIVE 거래가 비활성화되어 있습니다. LIVE_TRADING_ENABLED=true 설정이 필요합니다.")
        if not confirm_live:
            raise HTTPException(400, "실거래 주문을 실행하려면 confirm_live=true를 명시해야 합니다.")
        if not request.client_order_id:
            raise HTTPException(400, "실거래 주문에는 고유한 client_order_id가 필요합니다.")
    if request.quantity != request.quantity.to_integral_value():
        raise HTTPException(400, "국내 주식 수량은 정수(주 단위)여야 합니다.")
    if mode == "PAPER":
        try:
            quotes = await toss.prices([request.symbol])
            candles = await toss.candles(request.symbol, 30)
        except TossApiError as exc:
            raise HTTPException(503, f"최신 시세 확인에 실패해 PAPER 주문을 보류했습니다: {exc}") from exc
        if not quotes:
            raise HTTPException(503, "현재가를 확인할 수 없어 주문을 보류했습니다.")
        quote = quotes[0]
        cache_prices(quotes)
        if abs(request.price - quote.price) / quote.price > settings.paper_max_slippage_rate:
            raise HTTPException(409, "주문 입력 가격과 최신 현재가 차이가 허용 범위를 넘어 주문을 보류했습니다.")
        validate_paper_price(quote.price, candles if request.side == "BUY" else None)
        if request.side == "BUY":
            average_volume = sum((Decimal(c.volume) for c in candles[1:21]), Decimal("0")) / Decimal("20")
            if average_volume < settings.paper_min_average_daily_volume:
                raise HTTPException(409, "최근 평균 거래량이 낮아 PAPER 매수를 보류했습니다.")
            average_turnover = sum((Decimal(c.volume) * Decimal(c.close) for c in candles[1:21]), Decimal("0")) / Decimal("20")
            if average_turnover < settings.paper_min_average_daily_turnover:
                raise HTTPException(409, "최근 평균 거래대금이 낮아 PAPER 매수를 보류했습니다.")
        request = request.model_copy(update={"price": simulated_fill_price(quote.price, request.side)})
    if mode == "LIVE":
        async with live_order_lock:
            await live_order_preflight(request)
            try:
                live_journal.record_intent(request.client_order_id, request.symbol, request.side, request.quantity, request.price)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "이미 사용한 client_order_id입니다. 기존 주문 상태를 확인하세요.") from exc
            live_reconciliation.update(ok=False, message="실거래 주문 결과 확인 중")
            operations.add_event("LIVE 주문 의도", f"{request.client_order_id} · {request.symbol} · {request.side} · {request.quantity}주")
            try:
                submitted = await toss.place_order(request)
                live_journal.link_order(request.client_order_id, submitted.external_order_id)
                observed = live_journal.apply_detail(await toss.order_detail(submitted.external_order_id))
                # Never substitute PAPER cash/positions for the actual account.
                await toss.holdings()
                await toss.buying_power("KRW")
                await reconcile_live_orders()
            except Exception as exc:
                operations.add_event("LIVE 주문 확인 실패", f"{request.client_order_id} · 결과 미확정: {exc}", "error")
                raise HTTPException(503, "실거래 주문 결과가 미확정입니다. 재주문하지 말고 실계좌 주문 기록을 확인하세요.") from exc
            operations.add_event("LIVE 주문 확인", f"{request.client_order_id} · {observed['status']} · 체결 {observed['filled_quantity']}주")
            return submitted.model_copy(update={"status": observed["status"]})
    await enforce_order_safety(request)
    return broker.place(request, mode)


@app.post("/api/v1/mode/{new_mode}")
async def set_mode(new_mode: Literal["PAPER", "DRY_RUN", "LIVE"], confirm_live: bool = False) -> dict[str, str]:
    global mode
    if auto_task and not auto_task.done() and new_mode != mode:
        raise HTTPException(409, "자동매매가 실행 중입니다. 먼저 중지한 뒤 모드를 바꾸세요.")
    if new_mode == "LIVE" and not settings.live_trading_enabled:
        raise HTTPException(403, "LIVE 거래가 비활성화되어 있습니다. 환경변수 LIVE_TRADING_ENABLED=true로 명시적으로 활성화하세요.")
    if new_mode == "LIVE" and not confirm_live:
        raise HTTPException(400, "실거래 모드 전환을 명시적으로 확인하려면 confirm_live=true를 전달하세요.")
    if new_mode == "LIVE" and not settings.toss_account_seq:
        raise HTTPException(400, "LIVE 거래에는 TOSS_ACCOUNT_SEQ가 필요합니다.")
    if new_mode == "LIVE":
        if not live_reconciliation["ok"] or live_journal.unresolved_intents():
            raise HTTPException(409, "실계좌 주문 복구가 완료되지 않아 LIVE 모드로 전환할 수 없습니다.")
        if risk_guard.emergency_state()["active"]:
            raise HTTPException(423, "비상 정지가 활성화되어 LIVE 모드로 전환할 수 없습니다.")
        try:
            live_risk = await daily_risk_snapshot("LIVE")
        except Exception as exc:
            raise HTTPException(409, f"LIVE 위험 기준을 검증할 수 없어 모드 전환을 차단했습니다: {exc}") from exc
        if live_risk["halted"]:
            raise HTTPException(423, f"실계좌 일일 손실 한도에 도달해 LIVE 모드 전환을 차단했습니다: {live_risk['reason']}")
    mode = new_mode
    return {"mode": mode}





