import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.auto_trading import (auto_buy_loop, auto_exit_loop, block_for_connection_error,
                              check_auto_buys, execute_paper_sell,
                              get_automation_config, get_automation_state,
                              market_is_regular_open, set_auto_buy_paused,
                              update_automation_config)
from app.backtest import BacktestRules, run_backtest
from app.config import settings
from app.database import db, initialize_database
from app.portfolio import build_positions
from app.performance_history import daily_performance_loop, ensure_daily_performance
from app.recommendation import BLOCKED_WARNINGS, load_live_stocks, load_stock_name_map
from app.strategy import SAMPLE_STOCKS, recommend, recommend_stocks
from app.toss import TossApiError, TossInvestClient


toss = TossInvestClient()
auto_exit_status = {"enabled": settings.paper_auto_exit_enabled, "last_check": None,
                    "next_check": None, "last_actions": [], "last_error": None,
                    "consecutive_errors": 0}
auto_buy_status = {"enabled": settings.paper_auto_buy_enabled, "last_check": None,
                   "next_check": None, "last_actions": [], "warnings": [], "risk": None,
                   "market_filter": None, "last_error": None, "consecutive_errors": 0}
connection_status = {"state": "UNKNOWN", "connected": False, "ip_not_allowed": False,
                     "message": "연결 확인 필요", "last_checked": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    monitors = []
    try:
        await ensure_daily_performance(toss, settings)
    except Exception as exc:
        auto_buy_status["last_error"] = f"일별 성과 초기 저장 실패: {exc}"
    monitors.append(asyncio.create_task(daily_performance_loop(toss, settings)))
    if settings.paper_auto_exit_enabled and settings.toss_configured:
        monitors.append(asyncio.create_task(auto_exit_loop(toss, settings, auto_exit_status,
                                                           connection_status)))
    if settings.paper_auto_buy_enabled and settings.toss_configured:
        monitors.append(asyncio.create_task(auto_buy_loop(toss, settings, auto_buy_status,
                                                          connection_status)))
    yield
    for monitor in monitors:
        monitor.cancel()
    for monitor in monitors:
        try:
            await monitor
        except asyncio.CancelledError:
            pass


app = FastAPI(title="토스증권 소액투자 대시보드", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def toss_http_error(exc: TossApiError) -> HTTPException:
    status = exc.status_code if 400 <= exc.status_code < 500 else 502
    return HTTPException(status_code=status, detail={"message": str(exc), "requestId": exc.request_id})


def mark_connection_healthy() -> None:
    connection_status.update(state="CONNECTED", connected=True, ip_not_allowed=False,
                             message="토스 API 연결 정상",
                             last_checked=datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"))
    for status in (auto_buy_status, auto_exit_status):
        status["ip_not_allowed"] = False


def mark_connection_failure(exc: TossApiError) -> None:
    if block_for_connection_error(exc, connection_status):
        return
    connection_status.update(state="ERROR", connected=False, ip_not_allowed=False,
                             message=str(exc),
                             last_checked=datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"))


class BuyRequest(BaseModel):
    symbol: str
    quantity: int = Field(gt=0, le=1000)


class BacktestRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=12)
    initial_cash: int = Field(default=1_000_000, ge=100_000, le=100_000_000)
    stop_loss_percent: float = Field(default=5, gt=0, le=50)
    take_profit_percent: float = Field(default=10, gt=0, le=100)
    max_holding_days: int = Field(default=20, ge=1, le=100)


class AutomationRequest(BaseModel):
    paused: bool


class AutomationConfigRequest(BaseModel):
    stop_loss_percent: float = Field(gt=0, le=50)
    take_profit_percent: float = Field(gt=0, le=100)
    trailing_stop_percent: float = Field(gt=0, le=50)
    max_holding_days: int = Field(ge=1, le=365)
    min_score: int = Field(ge=1, le=100)
    max_positions: int = Field(ge=1, le=20)
    max_order_amount: int = Field(ge=1000, le=100_000_000)
    daily_loss_limit_percent: float = Field(gt=0, le=50)


class PaperResetRequest(BaseModel):
    confirm: str


def now_kst_date() -> str:
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def save_recommendations(items: list[dict]) -> None:
    with db() as conn:
        for item in items:
            conn.execute(
                """INSERT OR IGNORE INTO recommendation_history
                   (recommendation_date,symbol,name,price,score,ma5,ma20,rsi14,volume_ratio,reasons)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (now_kst_date(), item["symbol"], item["name"], item["price"], item["score"],
                 item["ma5"], item["ma20"], item["rsi14"], item["volume_ratio"],
                 json.dumps(item["reasons"], ensure_ascii=False)),
            )


def daily_report() -> dict:
    today = now_kst_date()
    with db() as conn:
        trades = [dict(row) for row in conn.execute(
            """SELECT * FROM paper_trades
               WHERE date(created_at,'+9 hours')=? ORDER BY id""", (today,))]
        risk = conn.execute("SELECT * FROM daily_risk WHERE risk_date=?", (today,)).fetchone()
    sells = [item for item in trades if item["side"] == "SELL"]
    return {"date": today, "buy_count": sum(item["side"] == "BUY" for item in trades),
            "sell_count": len(sells),
            "realized_pnl": sum(int(item.get("realized_pnl", 0)) for item in sells),
            "start_equity": int(risk["start_equity"]) if risk else None,
            "current_equity": int(risk["current_equity"]) if risk else None,
            "return_rate": float(risk["loss_rate"]) if risk else 0,
            "halted": bool(risk["halted"]) if risk else False}


def describe_market(calendar: dict) -> dict:
    now = datetime.now(timezone(timedelta(hours=9)))
    today = calendar.get("today") or {}
    integrated = today.get("integrated") or {}
    regular = integrated.get("regularMarket")
    if regular and market_is_regular_open(calendar, now):
        return {"status": "OPEN", "label": "정규장 운영 중", "next_event": regular["endTime"]}
    if regular and now < datetime.fromisoformat(regular["startTime"]):
        return {"status": "BEFORE_OPEN", "label": "장 시작 전", "next_event": regular["startTime"]}
    next_day = calendar.get("nextBusinessDay") or {}
    next_regular = ((next_day.get("integrated") or {}).get("regularMarket"))
    return {"status": "CLOSED", "label": "휴장 또는 장 마감",
            "next_event": next_regular.get("startTime") if next_regular else None}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={
        "mode": settings.trading_mode,
        "default_budget": settings.paper_auto_buy_budget,
        "paper_initial_cash": settings.paper_initial_cash,
    })


@app.get("/api/dashboard")
async def dashboard(budget: int = settings.paper_auto_buy_budget):
    if budget < 10_000 or budget > 100_000_000:
        raise HTTPException(400, "투자금은 1만~1억원 사이로 입력하세요.")
    with db() as conn:
        cash = conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"]
        all_trades = [dict(x) for x in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
        trades = list(reversed(all_trades[-20:]))
        positions = build_positions(all_trades)
    recommendations = recommend(min(budget, cash))
    data_source, data_timestamp, warnings = "SAMPLE", None, []
    filter_stats = None
    if settings.toss_configured:
        try:
            live_stocks, warnings, data_timestamp, filter_stats = await load_live_stocks(toss, min(budget, cash))
            recommendations = recommend_stocks(live_stocks, min(budget, cash))
            data_source = "TOSS LIVE"
        except TossApiError as exc:
            mark_connection_failure(exc)
            warnings = [f"실시간 데이터 연결 실패: {exc}"]
    if data_source == "TOSS LIVE":
        save_recommendations(recommendations)

    holdings = list(positions.values())
    if holdings and settings.toss_configured:
        try:
            current = await toss.prices([item["symbol"] for item in holdings])
            current_map = {item["symbol"]: int(float(item["lastPrice"])) for item in current}
            for item in holdings:
                item["current_price"] = current_map.get(item["symbol"], item["avg_price"])
                item["market_value"] = item["current_price"] * item["quantity"]
                item["unrealized_pnl"] = (item["current_price"] - item["avg_price"]) * item["quantity"]
                item["return_rate"] = (item["current_price"] / item["avg_price"] - 1) if item["avg_price"] else 0
        except TossApiError as exc:
            mark_connection_failure(exc)
            warnings.append(f"보유 종목 평가 실패: {exc}")
    for item in holdings:
        item.setdefault("current_price", item["avg_price"])
        item.setdefault("market_value", item["avg_price"] * item["quantity"])
        item.setdefault("unrealized_pnl", 0)
        item.setdefault("return_rate", 0)

    realized_pnl = sum(int(trade.get("realized_pnl", 0)) for trade in all_trades)
    unrealized_pnl = sum(item["unrealized_pnl"] for item in holdings)
    sell_trades = [trade for trade in all_trades if trade["side"] == "SELL"]
    wins = sum(1 for trade in sell_trades if int(trade.get("realized_pnl", 0)) > 0)
    equity = cash + sum(item["market_value"] for item in holdings)
    with db() as conn:
        recommendation_count = conn.execute("SELECT COUNT(*) FROM recommendation_history").fetchone()[0]
        history = [dict(row) for row in conn.execute(
            "SELECT * FROM recommendation_history ORDER BY id DESC LIMIT 10")]
        auto_signals = [dict(row) for row in conn.execute(
            "SELECT * FROM auto_signals ORDER BY id DESC LIMIT 10")]
        signal_symbols = [item["symbol"] for item in auto_signals if not item.get("name")]
        signal_names = {}
        if signal_symbols:
            placeholders = ",".join("?" for _ in signal_symbols)
            trade_rows = conn.execute(
                f"""SELECT symbol, name FROM paper_trades
                    WHERE symbol IN ({placeholders}) ORDER BY id DESC""",
                signal_symbols,
            ).fetchall()
            history_rows = conn.execute(
                f"""SELECT symbol, name FROM recommendation_history
                    WHERE symbol IN ({placeholders}) ORDER BY id DESC""",
                signal_symbols,
            ).fetchall()
            for row in list(history_rows) + list(trade_rows):
                signal_names[row["symbol"]] = row["name"]
        automation_events = [dict(row) for row in conn.execute(
            "SELECT * FROM automation_events ORDER BY id DESC LIMIT 30")]
        latest_backtest_row = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
        daily_performance_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM daily_performance ORDER BY performance_date DESC LIMIT 30")]
    if history and settings.toss_configured:
        try:
            history_prices = await toss.prices(list(dict.fromkeys(item["symbol"] for item in history)))
            history_map = {item["symbol"]: int(float(item["lastPrice"])) for item in history_prices}
            for item in history:
                item["current_price"] = history_map.get(item["symbol"], item["price"])
                item["return_rate"] = item["current_price"] / item["price"] - 1
        except TossApiError:
            pass
    for item in history:
        item.setdefault("current_price", item["price"])
        item.setdefault("return_rate", 0)
    missing_signal_symbols = [item["symbol"] for item in auto_signals
                              if not item.get("name") and item["symbol"] not in signal_names]
    if missing_signal_symbols and settings.toss_configured:
        try:
            signal_names.update(await load_stock_name_map(toss, list(dict.fromkeys(missing_signal_symbols))))
        except TossApiError as exc:
            mark_connection_failure(exc)
    for item in auto_signals:
        item["name"] = item.get("name") or signal_names.get(item["symbol"]) or item["symbol"]
    performance = {"equity": equity, "total_pnl": equity - settings.paper_initial_cash,
                   "total_return_rate": equity / settings.paper_initial_cash - 1,
                   "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl,
                   "sell_count": len(sell_trades), "win_rate": wins / len(sell_trades) if sell_trades else 0,
                   "recommendation_count": recommendation_count}
    runtime_config = get_automation_config()
    market = {"status": "UNKNOWN", "label": "토스 연결 필요", "next_event": None}
    if settings.toss_configured:
        try:
            market = describe_market(await toss.market_calendar_kr())
        except TossApiError as exc:
            mark_connection_failure(exc)
            market = {"status": "ERROR", "label": str(exc), "next_event": None}
    latest_backtest = dict(latest_backtest_row) if latest_backtest_row else None
    comparison = ({"backtest_return_rate": latest_backtest["total_return_rate"],
                   "paper_return_rate": performance["total_return_rate"],
                   "difference": performance["total_return_rate"] - latest_backtest["total_return_rate"]}
                  if latest_backtest else None)
    return {"mode": settings.trading_mode, "cash": cash, "recommendations": recommendations,
            "holdings": holdings, "trades": trades, "data_source": data_source,
            "data_timestamp": data_timestamp, "warnings": warnings, "filter_stats": filter_stats,
            "performance": performance, "recommendation_history": history,
            "auto_exit": {**auto_exit_status,
                          "stop_loss_rate": runtime_config["stop_loss_rate"],
                          "take_profit_rate": runtime_config["take_profit_rate"],
                          "trailing_stop_rate": runtime_config["trailing_stop_rate"],
                          "max_holding_days": runtime_config["max_holding_days"],
                          "interval_seconds": settings.paper_auto_exit_interval_seconds},
            "auto_buy": {**auto_buy_status, "state": get_automation_state(),
                         "interval_seconds": settings.paper_auto_buy_interval_seconds,
                         "min_score": runtime_config["min_score"],
                         "max_positions": runtime_config["max_positions"],
                         "max_order_amount": runtime_config["max_order_amount"],
                         "daily_loss_limit_rate": runtime_config["daily_loss_limit_rate"],
                         "signals": auto_signals},
            "market": market, "daily_report": daily_report(),
            "automation_config": runtime_config, "automation_events": automation_events,
            "latest_backtest": latest_backtest, "comparison": comparison,
            "connection": connection_status, "daily_performance_history": daily_performance_rows}


@app.post("/api/paper/buy")
async def paper_buy(order: BuyRequest):
    stock = next((s for s in SAMPLE_STOCKS if s.symbol == order.symbol), None)
    name = stock.name if stock else order.symbol
    price = stock.price if stock else 0
    if settings.toss_configured:
        try:
            infos, active_warnings, current = await asyncio.gather(
                toss.stock_infos([order.symbol]), toss.warnings(order.symbol), toss.prices([order.symbol])
            )
            info = infos[0]
            detail = info.get("koreanMarketDetail") or {}
            blocked = any(item.get("warningType") in BLOCKED_WARNINGS for item in active_warnings)
            if (info.get("status") != "ACTIVE" or detail.get("krxTradingSuspended")
                    or detail.get("nxtTradingSuspended") or blocked):
                raise HTTPException(400, "현재 가상매수할 수 없는 종목입니다.")
            name = info["name"]
            price = int(float(current[0]["lastPrice"]))
        except HTTPException:
            raise
        except (TossApiError, KeyError, IndexError, ValueError):
            raise HTTPException(502, "실제 현재가를 확인할 수 없어 가상매수를 중단했습니다.")
    elif not stock or stock.warning:
        raise HTTPException(400, "가상매수할 수 없는 종목입니다.")
    fee = round(price * order.quantity * settings.paper_buy_fee_rate)
    amount = price * order.quantity + fee
    with db() as conn:
        cash = conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"]
        if amount > cash:
            raise HTTPException(400, "가상 현금이 부족합니다.")
        conn.execute("UPDATE paper_account SET cash=cash-? WHERE id=1", (amount,))
        conn.execute("INSERT INTO paper_trades(symbol,name,side,quantity,price,fee) VALUES(?,?,?,?,?,?)",
                     (order.symbol, name, "BUY", order.quantity, price, fee))
    return {"message": f"{name} {order.quantity}주 가상매수 완료", "amount": amount}


@app.post("/api/paper/reset")
def reset_paper_account(request: PaperResetRequest):
    if request.confirm != "RESET_PAPER":
        raise HTTPException(400, "초기화 확인 값이 올바르지 않습니다.")
    with db() as conn:
        conn.execute("UPDATE paper_account SET cash=? WHERE id=1", (settings.paper_initial_cash,))
        for table in ("paper_trades", "auto_signals", "daily_risk",
                      "daily_performance", "position_tracking", "automation_events"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT INTO automation_events(event_type,symbol,message,details) VALUES('PAPER_RESET',NULL,?,?)",
            (f"PAPER 계좌를 {settings.paper_initial_cash:,}원으로 초기화했습니다.",
             json.dumps({"initial_cash": settings.paper_initial_cash}, ensure_ascii=False)),
        )
    return {"message": f"PAPER 계좌를 {settings.paper_initial_cash:,}원으로 초기화했습니다."}


@app.post("/api/paper/sell")
async def paper_sell(order: BuyRequest):
    if not settings.toss_configured:
        raise HTTPException(400, "실제 현재가 연결 후 가상매도할 수 있습니다.")
    try:
        current = await toss.prices([order.symbol])
        price = int(float(current[0]["lastPrice"]))
    except (TossApiError, KeyError, IndexError, ValueError):
        raise HTTPException(502, "현재가를 확인할 수 없어 가상매도를 중단했습니다.")
    try:
        result = execute_paper_sell(order.symbol, order.quantity, price, "MANUAL", settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"message": f"{result['name']} {order.quantity}주 가상매도 완료",
            "proceeds": result["proceeds"], "realizedPnl": result["realized_pnl"]}


@app.post("/api/backtest")
async def backtest(request: BacktestRequest):
    if not settings.toss_configured:
        raise HTTPException(400, "토스 API를 연결한 뒤 백테스트할 수 있습니다.")
    symbol = request.symbol.strip().upper()
    try:
        infos, candles = await asyncio.gather(toss.stock_infos([symbol]), toss.candles(symbol, 200))
        rules = BacktestRules(
            initial_cash=request.initial_cash,
            stop_loss_rate=request.stop_loss_percent / 100,
            take_profit_rate=request.take_profit_percent / 100,
            max_holding_days=request.max_holding_days,
            buy_fee_rate=settings.paper_buy_fee_rate,
            sell_fee_rate=settings.paper_sell_fee_rate,
            sell_tax_rate=settings.paper_sell_tax_rate,
        )
        result = run_backtest(candles, rules)
        # 표시값은 최종 평가금과 총손익을 기준으로 다시 계산해 항상 일치시킨다.
        result["total_pnl"] = result["final_equity"] - request.initial_cash
        result["total_return_rate"] = result["total_pnl"] / request.initial_cash
        result.update(symbol=symbol, name=infos[0].get("name", symbol), rules={
            "stop_loss_percent": request.stop_loss_percent,
            "take_profit_percent": request.take_profit_percent,
            "max_holding_days": request.max_holding_days,
        })
        with db() as conn:
            conn.execute(
                """INSERT INTO backtest_runs
                   (symbol,name,initial_cash,final_equity,total_pnl,total_return_rate,
                    max_drawdown,trade_count,win_rate,rules)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (symbol, result["name"], request.initial_cash, result["final_equity"],
                 result["total_pnl"], result["total_return_rate"], result["max_drawdown"],
                 result["trade_count"], result["win_rate"],
                 json.dumps(result["rules"], ensure_ascii=False)),
            )
        return result
    except TossApiError as exc:
        raise toss_http_error(exc) from exc
    except (ValueError, KeyError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/paper/automation")
async def update_automation(request: AutomationRequest):
    if not request.paused and not settings.toss_configured:
        raise HTTPException(400, "토스 API를 연결한 뒤 자동매수를 시작할 수 있습니다.")
    try:
        if not request.paused:
            try:
                await toss.reconnect()
                mark_connection_healthy()
            except TossApiError as exc:
                mark_connection_failure(exc)
                raise toss_http_error(exc) from exc
        state = set_auto_buy_paused(request.paused,
                                    "USER_EMERGENCY_STOP" if request.paused else "USER_STARTED")
        message = "신규 PAPER 자동매수를 중지했습니다." if request.paused else "PAPER 자동매수를 시작했습니다."
        actions = []
        if not request.paused and settings.toss_configured:
            try:
                auto_buy_status["last_check"] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
                result = await check_auto_buys(toss, settings)
                actions = result["actions"]
                auto_buy_status.update(last_actions=actions, warnings=result["warnings"],
                                       risk=result["risk"], market_filter=result.get("market_filter"),
                                       last_error=None)
                if actions:
                    message += f" {len(actions)}개 종목을 가상매수했습니다."
            except TossApiError as exc:
                auto_buy_status["last_error"] = str(exc)
                mark_connection_failure(exc)
                set_auto_buy_paused(True, "CONNECTION_CHECK_FAILED")
                raise toss_http_error(exc) from exc
            except Exception as exc:
                auto_buy_status["last_error"] = str(exc)
                set_auto_buy_paused(True, "FIRST_CHECK_FAILED")
                raise HTTPException(502, f"첫 자동매수 검사에 실패해 다시 중지했습니다: {exc}") from exc
        return {"message": message, "state": state, "actions": actions}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/paper/automation/config")
def save_automation_config(request: AutomationConfigRequest):
    values = {"stop_loss_rate": request.stop_loss_percent / 100,
              "take_profit_rate": request.take_profit_percent / 100,
              "trailing_stop_rate": request.trailing_stop_percent / 100,
              "max_holding_days": request.max_holding_days,
              "min_score": request.min_score, "max_positions": request.max_positions,
              "max_order_amount": request.max_order_amount,
              "daily_loss_limit_rate": request.daily_loss_limit_percent / 100}
    return {"message": "자동매매 설정을 저장했습니다.",
            "config": update_automation_config(values)}


@app.get("/api/paper/trades.csv")
def export_trades_csv():
    with db() as conn:
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["ID", "종목코드", "종목명", "구분", "수량", "가격", "수수료",
                     "세금", "실현손익", "사유", "거래시각"])
    for item in trades:
        writer.writerow([item["id"], item["symbol"], item["name"], item["side"],
                         item["quantity"], item["price"], item.get("fee", 0),
                         item.get("tax", 0), item.get("realized_pnl", 0),
                         item.get("reason", "MANUAL"), item["created_at"]])
    headers = {"Content-Disposition": "attachment; filename=paper-trades.csv"}
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers=headers)


@app.get("/health")
def health():
    return {"status": "ok", "trading_mode": settings.trading_mode,
            "toss_configured": settings.toss_configured}


@app.get("/api/toss/status")
async def toss_status():
    if not settings.toss_configured:
        return {"configured": False, "connected": False, "accounts": []}
    try:
        accounts = await toss.accounts()
        mark_connection_healthy()
        return {"configured": True, "connected": True, "accounts": accounts}
    except TossApiError as exc:
        mark_connection_failure(exc)
        raise toss_http_error(exc) from exc


@app.post("/api/toss/reconnect")
async def toss_reconnect():
    if not settings.toss_configured:
        raise HTTPException(400, "먼저 .env에 토스 API 키를 입력하세요.")
    try:
        accounts = await toss.reconnect()
        mark_connection_healthy()
        auto_buy_status.update(last_error=None, consecutive_errors=0)
        auto_exit_status.update(last_error=None, consecutive_errors=0)
        return {"configured": True, "connected": True, "accounts": accounts,
                "message": "새 토큰으로 토스 API 연결 복구를 확인했습니다. 자동매수는 직접 시작하세요."}
    except TossApiError as exc:
        mark_connection_failure(exc)
        raise toss_http_error(exc) from exc


@app.get("/api/toss/holdings")
async def toss_holdings():
    try:
        return {"result": await toss.holdings()}
    except TossApiError as exc:
        raise toss_http_error(exc) from exc


@app.get("/api/toss/prices")
async def toss_prices(symbols: str = "005930,000660"):
    try:
        return {"result": await toss.prices(symbols.split(","))}
    except TossApiError as exc:
        raise toss_http_error(exc) from exc


@app.get("/api/toss/candles/{symbol}")
async def toss_candles(symbol: str, count: int = 30):
    try:
        return {"result": await toss.candles(symbol, count)}
    except TossApiError as exc:
        raise toss_http_error(exc) from exc
