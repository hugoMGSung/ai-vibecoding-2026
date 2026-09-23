import asyncio
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.database import db
from app.portfolio import build_positions
from app.toss import TossInvestClient


KST = timezone(timedelta(hours=9))


def trades_until(target_date: str) -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM paper_trades
               WHERE date(created_at,'+9 hours')<=? ORDER BY id""", (target_date,)
        )]


def cash_from_trades(trades: list[dict], initial_cash: int) -> int:
    cash = initial_cash
    for item in trades:
        gross = int(item["price"]) * int(item["quantity"])
        if item["side"] == "BUY":
            cash -= gross + int(item.get("fee", 0))
        else:
            cash += gross - int(item.get("fee", 0)) - int(item.get("tax", 0))
    return cash


async def closing_prices(client: TossInvestClient, symbols: list[str], target_date: str,
                         current: bool) -> tuple[dict[str, int], str]:
    if not symbols:
        return {}, "NO_POSITIONS"
    if current:
        result = await client.prices(symbols)
        return ({item["symbol"]: int(float(item["lastPrice"])) for item in result},
                "TOSS_CURRENT")

    responses = await asyncio.gather(
        *(client.candles(symbol, 30) for symbol in symbols), return_exceptions=True
    )
    prices: dict[str, int] = {}
    for symbol, candles in zip(symbols, responses):
        if isinstance(candles, Exception):
            continue
        eligible = [item for item in candles if str(item.get("timestamp", ""))[:10] <= target_date]
        if eligible:
            latest = max(eligible, key=lambda item: str(item.get("timestamp", "")))
            prices[symbol] = int(float(latest["closePrice"]))
    return prices, "TOSS_DAILY_CLOSE" if len(prices) == len(symbols) else "PARTIAL_ESTIMATE"


async def save_daily_performance(client: TossInvestClient, config: Settings,
                                 target_date: str, final: bool) -> dict:
    trades = trades_until(target_date)
    positions = build_positions(trades)
    cash = cash_from_trades(trades, config.paper_initial_cash)
    today = datetime.now(KST).date().isoformat()
    try:
        prices, source = await closing_prices(client, list(positions), target_date,
                                              current=target_date >= today)
    except Exception:
        prices, source = {}, "AVERAGE_PRICE_ESTIMATE"

    market_value = 0
    unrealized_pnl = 0
    for symbol, position in positions.items():
        price = prices.get(symbol, int(position["avg_price"]))
        market_value += price * int(position["quantity"])
        unrealized_pnl += (price - int(position["avg_price"])) * int(position["quantity"])
    equity = cash + market_value
    sells = [item for item in trades if item["side"] == "SELL"]
    realized_pnl = sum(int(item.get("realized_pnl", 0)) for item in sells)
    # created_at은 UTC이므로 날짜별 건수는 SQLite의 KST 변환으로 다시 조회한다.
    with db() as conn:
        counts = conn.execute(
            """SELECT SUM(side='BUY') AS buys,SUM(side='SELL') AS sells
               FROM paper_trades WHERE date(created_at,'+9 hours')=?""", (target_date,)
        ).fetchone()
        previous = conn.execute(
            """SELECT equity FROM daily_performance
               WHERE performance_date<? ORDER BY performance_date DESC LIMIT 1""", (target_date,)
        ).fetchone()
    previous_equity = int(previous["equity"]) if previous else config.paper_initial_cash
    daily_pnl = equity - previous_equity
    daily_return = daily_pnl / previous_equity if previous_equity else 0
    total_pnl = equity - config.paper_initial_cash
    total_return = total_pnl / config.paper_initial_cash if config.paper_initial_cash else 0
    wins = sum(int(item.get("realized_pnl", 0)) > 0 for item in sells)
    snapshot = {
        "performance_date": target_date, "cash": cash, "market_value": market_value,
        "equity": equity, "daily_pnl": daily_pnl, "daily_return_rate": daily_return,
        "total_pnl": total_pnl, "total_return_rate": total_return,
        "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl,
        "buy_count": int(counts["buys"] or 0), "sell_count": int(counts["sells"] or 0),
        "position_count": len(positions), "win_rate": wins / len(sells) if sells else 0,
        "price_source": source, "is_final": 1 if final else 0,
    }
    with db() as conn:
        conn.execute(
            """INSERT INTO daily_performance
               (performance_date,cash,market_value,equity,daily_pnl,daily_return_rate,
                total_pnl,total_return_rate,realized_pnl,unrealized_pnl,buy_count,sell_count,
                position_count,win_rate,price_source,is_final)
               VALUES(:performance_date,:cash,:market_value,:equity,:daily_pnl,:daily_return_rate,
                      :total_pnl,:total_return_rate,:realized_pnl,:unrealized_pnl,:buy_count,
                      :sell_count,:position_count,:win_rate,:price_source,:is_final)
               ON CONFLICT(performance_date) DO UPDATE SET
                cash=excluded.cash,market_value=excluded.market_value,equity=excluded.equity,
                daily_pnl=excluded.daily_pnl,daily_return_rate=excluded.daily_return_rate,
                total_pnl=excluded.total_pnl,total_return_rate=excluded.total_return_rate,
                realized_pnl=excluded.realized_pnl,unrealized_pnl=excluded.unrealized_pnl,
                buy_count=excluded.buy_count,sell_count=excluded.sell_count,
                position_count=excluded.position_count,win_rate=excluded.win_rate,
                price_source=excluded.price_source,is_final=MAX(daily_performance.is_final,excluded.is_final),
                updated_at=CURRENT_TIMESTAMP""", snapshot,
        )
    return snapshot


async def ensure_daily_performance(client: TossInvestClient, config: Settings) -> list[dict]:
    today = datetime.now(KST).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    with db() as conn:
        prior = conn.execute(
            "SELECT 1 FROM daily_performance WHERE performance_date=?", (yesterday,)
        ).fetchone()
    saved = []
    if not prior:
        saved.append(await save_daily_performance(client, config, yesterday, final=True))
    saved.append(await save_daily_performance(client, config, today.isoformat(), final=False))
    return saved


async def daily_performance_loop(client: TossInvestClient, config: Settings) -> None:
    last_date = datetime.now(KST).date()
    while True:
        await asyncio.sleep(300)
        current_date = datetime.now(KST).date()
        try:
            if current_date != last_date:
                await save_daily_performance(client, config, last_date.isoformat(), final=True)
                last_date = current_date
            await save_daily_performance(client, config, current_date.isoformat(), final=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 일시적인 API/DB 오류로 장기 실행 루프가 종료되지 않게 한다.
            continue
