import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.database import db
from app.portfolio import build_positions
from app.recommendation import load_live_stocks
from app.strategy import recommend_stocks
from app.toss import TossApiError, TossInvestClient


KST = timezone(timedelta(hours=9))


def is_ip_not_allowed_error(exc: Exception) -> bool:
    return isinstance(exc, TossApiError) and exc.is_ip_not_allowed


def block_for_connection_error(exc: Exception, connection: dict | None = None) -> bool:
    """IP 미허용 오류는 횟수와 관계없이 즉시 신규 자동매수를 잠근다."""
    if not is_ip_not_allowed_error(exc):
        return False
    state = connection if connection is not None else {}
    state.update(state="IP_NOT_ALLOWED", connected=False, ip_not_allowed=True,
                 message="WTS 허용 IP 등록 필요",
                 last_checked=datetime.now(KST).isoformat(timespec="seconds"))
    current = get_automation_state()
    if not current["auto_buy_paused"] or current.get("pause_reason") != "IP_ADDRESS_NOT_ALLOWED":
        set_auto_buy_paused(True, "IP_ADDRESS_NOT_ALLOWED")
    return True


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def get_automation_state() -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM automation_state WHERE id=1").fetchone()
    return dict(row) if row else {"auto_buy_paused": 1, "pause_reason": "STATE_NOT_FOUND"}


def get_automation_config() -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM automation_config WHERE id=1").fetchone()
    if not row:
        raise ValueError("자동매매 설정을 찾을 수 없습니다.")
    return dict(row)


def update_automation_config(values: dict) -> dict:
    with db() as conn:
        conn.execute(
            """UPDATE automation_config SET stop_loss_rate=?,take_profit_rate=?,
               trailing_stop_rate=?,max_holding_days=?,min_score=?,max_positions=?,
               max_order_amount=?,daily_loss_limit_rate=?,updated_at=CURRENT_TIMESTAMP WHERE id=1""",
            (values["stop_loss_rate"], values["take_profit_rate"], values["trailing_stop_rate"],
             values["max_holding_days"], values["min_score"], values["max_positions"],
             values["max_order_amount"], values["daily_loss_limit_rate"]),
        )
    log_event("CONFIG_CHANGED", None, "자동매매 설정을 변경했습니다.", values)
    return get_automation_config()


def log_event(event_type: str, symbol: str | None, message: str, details: dict | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO automation_events(event_type,symbol,message,details) VALUES(?,?,?,?)",
            (event_type, symbol, message, json.dumps(details or {}, ensure_ascii=False)),
        )


def set_auto_buy_paused(paused: bool, reason: str) -> dict:
    with db() as conn:
        if not paused:
            risk = conn.execute("SELECT halted FROM daily_risk WHERE risk_date=?", (today_kst(),)).fetchone()
            if risk and risk["halted"]:
                raise ValueError("오늘은 일일 손실 한도에 도달해 자동매수를 재개할 수 없습니다.")
        conn.execute(
            """UPDATE automation_state SET auto_buy_paused=?, pause_reason=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=1""",
            (1 if paused else 0, reason),
        )
    log_event("AUTO_BUY_PAUSED" if paused else "AUTO_BUY_STARTED", None, reason)
    return get_automation_state()


def calculate_order_quantity(price: int, cash: int, max_order_amount: int,
                             suggested_quantity: int) -> int:
    if price <= 0 or cash <= 0 or max_order_amount <= 0 or suggested_quantity <= 0:
        return 0
    return min(cash // price, max_order_amount // price, suggested_quantity)


def evaluate_exit(avg_price: int, current_price: int, stop_loss_rate: float,
                  take_profit_rate: float, highest_price: int | None = None,
                  trailing_stop_rate: float = 0, holding_days: int = 0,
                  max_holding_days: int = 0) -> str | None:
    if current_price <= avg_price * (1 - stop_loss_rate):
        return "AUTO_STOP_LOSS"
    if current_price >= avg_price * (1 + take_profit_rate):
        return "AUTO_TAKE_PROFIT"
    if highest_price and trailing_stop_rate and highest_price > avg_price:
        if current_price <= highest_price * (1 - trailing_stop_rate):
            return "AUTO_TRAILING_STOP"
    if max_holding_days and holding_days >= max_holding_days:
        return "AUTO_MAX_HOLDING"
    return None


def market_is_regular_open(calendar: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    today = calendar.get("today") or {}
    integrated = today.get("integrated")
    regular = integrated.get("regularMarket") if isinstance(integrated, dict) else None
    if not regular:
        return False
    start = datetime.fromisoformat(regular["startTime"])
    end = datetime.fromisoformat(regular["endTime"])
    return start <= now < end


def assess_market_filter(stocks: list, base_min_score: int) -> dict:
    analyzed = [stock for stock in stocks if getattr(stock, "ma20", 0) > 0]
    if not analyzed:
        return {"mode": "UNKNOWN", "label": "장세 판단 데이터 부족",
                "breadth": 0, "avg_trend": 0, "min_score": base_min_score, "allow_buy": False}
    breadth = sum(1 for stock in analyzed if stock.price >= stock.ma20) / len(analyzed)
    avg_trend = sum((stock.price / stock.ma20) - 1 for stock in analyzed) / len(analyzed)
    if breadth >= 0.60 and avg_trend >= 0:
        mode, label, score_add, allow_buy = "FAVORABLE", "장세 양호", 0, True
    elif breadth >= 0.45:
        mode, label, score_add, allow_buy = "CAUTIOUS", "장세 보통", 5, True
    elif breadth >= 0.35:
        mode, label, score_add, allow_buy = "WEAK", "장세 약함", 10, True
    else:
        mode, label, score_add, allow_buy = "DEFENSIVE", "장세 방어", 0, False
    return {"mode": mode, "label": label, "breadth": breadth, "avg_trend": avg_trend,
            "min_score": min(base_min_score + score_add, 95), "allow_buy": allow_buy}


def execute_paper_sell(symbol: str, quantity: int, price: int, reason: str,
                       config: Settings) -> dict:
    with db() as conn:
        all_trades = [dict(x) for x in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
        position = build_positions(all_trades).get(symbol)
        if not position or quantity > position["quantity"]:
            raise ValueError("가상 보유 수량이 부족합니다.")
        gross = price * quantity
        fee = round(gross * config.paper_sell_fee_rate)
        tax = round(gross * config.paper_sell_tax_rate)
        proceeds = gross - fee - tax
        realized_pnl = proceeds - position["avg_price"] * quantity
        conn.execute("UPDATE paper_account SET cash=cash+? WHERE id=1", (proceeds,))
        conn.execute(
            """INSERT INTO paper_trades
               (symbol,name,side,quantity,price,fee,tax,realized_pnl,reason)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (symbol, position["name"], "SELL", quantity, price, fee, tax, realized_pnl, reason),
        )
        remaining = position["quantity"] - quantity
        if remaining <= 0:
            conn.execute("DELETE FROM position_tracking WHERE symbol=?", (symbol,))
        conn.execute(
            "INSERT INTO automation_events(event_type,symbol,message,details) VALUES('AUTO_SELL',?,?,?)",
            (symbol, reason, json.dumps({"price": price, "quantity": quantity,
                                        "realized_pnl": realized_pnl}, ensure_ascii=False)),
        )
    return {"name": position["name"], "proceeds": proceeds, "realized_pnl": realized_pnl}


def execute_paper_buy(candidate: dict, quantity: int, price: int, config: Settings,
                      rules: dict | None = None) -> dict:
    signal_date = today_kst()
    symbol, score = candidate["symbol"], int(candidate["score"])
    with db() as conn:
        duplicate = conn.execute(
            "SELECT 1 FROM auto_signals WHERE signal_date=? AND symbol=? AND action='BUY'",
            (signal_date, symbol),
        ).fetchone()
        if duplicate:
            raise ValueError("오늘 이미 처리한 자동매수 신호입니다.")
        trades = [dict(x) for x in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
        positions = build_positions(trades)
        if symbol in positions:
            raise ValueError("이미 가상 보유 중인 종목입니다.")
        max_positions = int((rules or {}).get("max_positions", config.paper_max_positions))
        max_order_amount = int((rules or {}).get("max_order_amount", config.paper_max_order_amount))
        if len(positions) >= max_positions:
            raise ValueError("최대 보유 종목 수에 도달했습니다.")
        cash = int(conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
        fee = round(price * quantity * config.paper_buy_fee_rate)
        amount = price * quantity + fee
        if quantity <= 0 or amount > cash or amount > max_order_amount + fee:
            raise ValueError("자동매수 주문 한도를 초과했습니다.")
        reason = f"AUTO_BUY_SCORE_{score}"
        conn.execute(
            """INSERT INTO auto_signals(signal_date,symbol,name,action,score,price,reason)
               VALUES(?,?,?,'BUY',?,?,?)""",
            (signal_date, symbol, candidate["name"], score, price, reason),
        )
        conn.execute("UPDATE paper_account SET cash=cash-? WHERE id=1", (amount,))
        conn.execute(
            """INSERT INTO paper_trades(symbol,name,side,quantity,price,fee,reason)
               VALUES(?,?,?,?,?,?,?)""",
            (symbol, candidate["name"], "BUY", quantity, price, fee, reason),
        )
        conn.execute(
            """INSERT INTO position_tracking(symbol,highest_price,first_bought_at)
               VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET
               highest_price=MAX(highest_price,excluded.highest_price),updated_at=CURRENT_TIMESTAMP""",
            (symbol, price, today_kst()),
        )
        conn.execute(
            "INSERT INTO automation_events(event_type,symbol,message,details) VALUES('AUTO_BUY',?,?,?)",
            (symbol, reason, json.dumps({"price": price, "quantity": quantity, "score": score},
                                        ensure_ascii=False)),
        )
    return {"symbol": symbol, "name": candidate["name"], "quantity": quantity,
            "price": price, "amount": amount, "score": score, "reason": reason}


def update_daily_risk(equity: int, config: Settings, rules: dict | None = None) -> dict:
    risk_date = today_kst()
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO daily_risk
               (risk_date,start_equity,current_equity,loss_rate) VALUES(?,?,?,0)""",
            (risk_date, equity, equity),
        )
        row = conn.execute("SELECT * FROM daily_risk WHERE risk_date=?", (risk_date,)).fetchone()
        start_equity = int(row["start_equity"])
        loss_rate = equity / start_equity - 1 if start_equity else 0
        limit = float((rules or {}).get("daily_loss_limit_rate", config.paper_daily_loss_limit_rate))
        halted = loss_rate <= -limit
        conn.execute(
            """UPDATE daily_risk SET current_equity=?,loss_rate=?,halted=?,
               updated_at=CURRENT_TIMESTAMP WHERE risk_date=?""",
            (equity, loss_rate, 1 if halted else 0, risk_date),
        )
        if halted:
            conn.execute(
                """UPDATE automation_state SET auto_buy_paused=1,
                   pause_reason='DAILY_LOSS_LIMIT',updated_at=CURRENT_TIMESTAMP WHERE id=1"""
            )
            conn.execute(
                """INSERT INTO automation_events(event_type,message,details)
                   VALUES('DAILY_LOSS_HALT','일일 손실 한도로 자동매수를 중지했습니다.',?)""",
                (json.dumps({"equity": equity, "loss_rate": loss_rate}, ensure_ascii=False),),
            )
    return {"risk_date": risk_date, "start_equity": start_equity,
            "current_equity": equity, "loss_rate": loss_rate, "halted": halted}


async def check_auto_exits(client: TossInvestClient, config: Settings) -> list[dict]:
    with db() as conn:
        trades = [dict(x) for x in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
    positions = build_positions(trades)
    if not positions:
        return []
    calendar = await client.market_calendar_kr()
    if not market_is_regular_open(calendar):
        return []
    prices = await client.prices(list(positions))
    price_map = {item["symbol"]: int(float(item["lastPrice"])) for item in prices}
    rules = get_automation_config()
    actions = []
    for symbol, position in positions.items():
        current_price = price_map.get(symbol)
        if not current_price:
            continue
        with db() as conn:
            tracking = conn.execute("SELECT * FROM position_tracking WHERE symbol=?", (symbol,)).fetchone()
            if tracking:
                highest_price = max(int(tracking["highest_price"]), current_price)
                first_bought_at = tracking["first_bought_at"][:10]
                conn.execute(
                    "UPDATE position_tracking SET highest_price=?,updated_at=CURRENT_TIMESTAMP WHERE symbol=?",
                    (highest_price, symbol),
                )
            else:
                highest_price, first_bought_at = current_price, today_kst()
                conn.execute(
                    "INSERT INTO position_tracking(symbol,highest_price,first_bought_at) VALUES(?,?,?)",
                    (symbol, highest_price, first_bought_at),
                )
        holding_days = (datetime.now(KST).date() - datetime.fromisoformat(first_bought_at).date()).days
        reason = evaluate_exit(position["avg_price"], current_price,
                               float(rules["stop_loss_rate"]), float(rules["take_profit_rate"]),
                               highest_price, float(rules["trailing_stop_rate"]),
                               holding_days, int(rules["max_holding_days"]))
        if reason:
            result = execute_paper_sell(symbol, position["quantity"], current_price, reason, config)
            actions.append({"symbol": symbol, "price": current_price, "reason": reason,
                            "highest_price": highest_price, "holding_days": holding_days, **result})
    return actions


async def check_auto_buys(client: TossInvestClient, config: Settings) -> dict:
    state = get_automation_state()
    if not config.paper_auto_buy_enabled or state["auto_buy_paused"]:
        return {"actions": [], "warnings": [], "state": state, "risk": None, "market_filter": None}
    calendar = await client.market_calendar_kr()
    if not market_is_regular_open(calendar):
        return {"actions": [], "warnings": [], "state": state, "risk": None, "market_filter": None}

    rules = get_automation_config()
    log_event("SCAN_STARTED", None, "자동매수 후보 검색을 시작했습니다.")

    with db() as conn:
        cash = int(conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
        trades = [dict(x) for x in conn.execute("SELECT * FROM paper_trades ORDER BY id")]
    positions = build_positions(trades)
    current_prices = await client.prices(list(positions)) if positions else []
    price_map = {item["symbol"]: int(float(item["lastPrice"])) for item in current_prices}
    equity = cash + sum(price_map.get(symbol, item["avg_price"]) * item["quantity"]
                        for symbol, item in positions.items())
    risk = update_daily_risk(equity, config, rules)
    if risk["halted"]:
        return {"actions": [], "warnings": [], "state": get_automation_state(),
                "risk": risk, "market_filter": None}
    if len(positions) >= int(rules["max_positions"]):
        log_event("SCAN_SKIPPED", None, "최대 보유 종목 수에 도달했습니다.",
                  {"positions": len(positions)})
        return {"actions": [], "warnings": [], "state": get_automation_state(),
                "risk": risk, "market_filter": None}

    scan_budget = min(config.paper_auto_buy_budget, cash)
    if scan_budget < 10_000:
        return {"actions": [], "warnings": ["가상 현금이 부족합니다."],
                "state": state, "risk": risk, "market_filter": None}
    stocks, warnings, _, _ = await load_live_stocks(client, scan_budget)
    market_filter = assess_market_filter(stocks, int(rules["min_score"]))
    log_event("MARKET_FILTER", None, market_filter["label"], market_filter)
    if not market_filter["allow_buy"]:
        log_event("SCAN_SKIPPED", None, "장세 방어 모드로 신규 자동매수를 보류했습니다.",
                  market_filter)
        return {"actions": [], "warnings": warnings, "state": get_automation_state(),
                "risk": risk, "market_filter": market_filter}
    recommendations = recommend_stocks(stocks, scan_budget)
    candidates = []
    for item in recommendations:
        if item["score"] < int(market_filter["min_score"]):
            log_event("CANDIDATE_SKIPPED", item["symbol"], "최소 추천 점수 미달",
                      {"score": item["score"], "minimum": market_filter["min_score"],
                       "market_mode": market_filter["mode"]})
        elif item["symbol"] in positions:
            log_event("CANDIDATE_SKIPPED", item["symbol"], "이미 보유 중인 종목")
        else:
            candidates.append(item)
    if not candidates:
        log_event("SCAN_COMPLETED", None, "자동매수 조건을 충족한 종목이 없습니다.")
        return {"actions": [], "warnings": warnings, "state": state,
                "risk": risk, "market_filter": market_filter}

    fresh = await client.prices([item["symbol"] for item in candidates])
    fresh_map = {item["symbol"]: int(float(item["lastPrice"])) for item in fresh}
    actions = []
    slots = int(rules["max_positions"]) - len(positions)
    for candidate in candidates[:slots]:
        price = fresh_map.get(candidate["symbol"], candidate["price"])
        with db() as conn:
            current_cash = int(conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
        quantity = calculate_order_quantity(price, current_cash, int(rules["max_order_amount"]),
                                            int(candidate["buyable_quantity"]))
        if not quantity:
            log_event("CANDIDATE_SKIPPED", candidate["symbol"], "주문 가능 수량이 0입니다.",
                      {"price": price, "cash": current_cash})
            continue
        try:
            actions.append(execute_paper_buy(candidate, quantity, price, config, rules))
        except ValueError as exc:
            warnings.append(f"{candidate['name']}: {exc}")
            log_event("BUY_REJECTED", candidate["symbol"], str(exc))
    log_event("SCAN_COMPLETED", None, f"자동매수 후보 검색 완료: {len(actions)}건 실행",
              {"actions": len(actions), "warnings": warnings, "market_filter": market_filter})
    return {"actions": actions, "warnings": warnings, "state": get_automation_state(),
            "risk": risk, "market_filter": market_filter}


async def auto_exit_loop(client: TossInvestClient, config: Settings, status: dict,
                         connection: dict | None = None) -> None:
    while True:
        interval = max(config.paper_auto_exit_interval_seconds, 10)
        try:
            status["last_check"] = datetime.now(KST).isoformat(timespec="seconds")
            status["last_actions"] = await check_auto_exits(client, config)
            status["last_error"] = None
            status["consecutive_errors"] = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status["last_error"] = str(exc)
            status["consecutive_errors"] = status.get("consecutive_errors", 0) + 1
            log_event("API_ERROR", None, "자동매도 감시 오류", {"error": str(exc)})
            if block_for_connection_error(exc, connection):
                status["ip_not_allowed"] = True
            elif status["consecutive_errors"] == 3:
                set_auto_buy_paused(True, "CONSECUTIVE_API_ERRORS")
        status["next_check"] = (datetime.now(KST) + timedelta(seconds=interval)).isoformat(timespec="seconds")
        await asyncio.sleep(interval)


async def auto_buy_loop(client: TossInvestClient, config: Settings, status: dict,
                        connection: dict | None = None) -> None:
    while True:
        interval = max(config.paper_auto_buy_interval_seconds, 60)
        try:
            status["last_check"] = datetime.now(KST).isoformat(timespec="seconds")
            result = await check_auto_buys(client, config)
            status["last_actions"] = result["actions"]
            status["warnings"] = result["warnings"]
            status["risk"] = result["risk"]
            status["market_filter"] = result.get("market_filter")
            status["last_error"] = None
            status["consecutive_errors"] = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status["last_error"] = str(exc)
            status["consecutive_errors"] = status.get("consecutive_errors", 0) + 1
            log_event("API_ERROR", None, "자동매수 검색 오류", {"error": str(exc)})
            if block_for_connection_error(exc, connection):
                status["ip_not_allowed"] = True
            elif status["consecutive_errors"] == 3:
                set_auto_buy_paused(True, "CONSECUTIVE_API_ERRORS")
        status["next_check"] = (datetime.now(KST) + timedelta(seconds=interval)).isoformat(timespec="seconds")
        await asyncio.sleep(interval)
