import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.strategy import Stock, stock_from_candles

if TYPE_CHECKING:
    from app.toss import TossInvestClient
else:
    TossInvestClient = Any


BLOCKED_WARNINGS = {
    "LIQUIDATION_TRADING", "OVERHEATED", "INVESTMENT_WARNING",
    "INVESTMENT_RISK", "STOCK_WARRANTS", "VI_STATIC",
    "VI_DYNAMIC", "VI_STATIC_AND_DYNAMIC",
}
_universe_cache: tuple[float, dict[str, dict]] | None = None
_universe_lock = asyncio.Lock()


async def _load_universe(client: TossInvestClient) -> dict[str, dict]:
    global _universe_cache
    if _universe_cache and time.monotonic() - _universe_cache[0] < 86400:
        return _universe_cache[1]
    async with _universe_lock:
        if _universe_cache and time.monotonic() - _universe_cache[0] < 86400:
            return _universe_cache[1]
        kospi, kosdaq = await asyncio.gather(client.list_stocks("KOSPI"), client.list_stocks("KOSDAQ"))
        universe = {item["symbol"]: item for item in kospi + kosdaq}
        _universe_cache = (time.monotonic(), universe)
        return universe


async def load_stock_name_map(client: TossInvestClient, symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    universe = await _load_universe(client)
    return {symbol: universe[symbol]["name"] for symbol in symbols if symbol in universe}


def _is_tradeable(info: dict) -> bool:
    detail = info.get("koreanMarketDetail") or {}
    return (
        info.get("status") == "ACTIVE"
        and info.get("securityType") == "STOCK"
        and info.get("isCommonShare") is True
        and not detail.get("liquidationTrading", False)
        and not detail.get("krxTradingSuspended", False)
        and not detail.get("nxtTradingSuspended", False)
    )


async def load_live_stocks(client: TossInvestClient, budget: int) -> tuple[list[Stock], list[str], str | None, dict]:
    universe, ranking_result = await asyncio.gather(_load_universe(client), client.rankings(100))
    rankings = ranking_result.get("rankings", []) if isinstance(ranking_result, dict) else []
    ranked_at = ranking_result.get("rankedAt") if isinstance(ranking_result, dict) else None
    max_price = int(budget * 0.30)

    affordable = []
    for rank in rankings:
        item = universe.get(rank.get("symbol"))
        price = int(float((rank.get("price") or {}).get("lastPrice", 0)))
        trading_amount = int(float(rank.get("tradingAmount", 0)))
        if item and 0 < price <= max_price and trading_amount >= 1_000_000_000:
            affordable.append({"symbol": item["symbol"], "name": item["name"], "price": price})
    affordable = affordable[:30]
    if not affordable:
        return [], [], ranked_at, {"universe": len(universe), "affordable": 0, "safe": 0, "analyzed": 0}

    infos = await client.stock_infos([item["symbol"] for item in affordable])
    info_map = {item["symbol"]: item for item in infos if _is_tradeable(item)}
    detail_safe = [item for item in affordable if item["symbol"] in info_map]
    warning_semaphore = asyncio.Semaphore(4)

    async def check_warning(item: dict):
        async with warning_semaphore:
            warnings = await client.warnings(item["symbol"])
            blocked = any(warning.get("warningType") in BLOCKED_WARNINGS for warning in warnings)
            return None if blocked else item

    checked = await asyncio.gather(*(check_warning(item) for item in detail_safe), return_exceptions=True)
    safe = [item for item in checked if isinstance(item, dict)][:12]
    failed_warning_checks = sum(isinstance(item, Exception) for item in checked)
    warnings = [f"유의사항 확인 실패 종목 {failed_warning_checks}개 제외"] if failed_warning_checks else []
    candle_semaphore = asyncio.Semaphore(3)

    async def analyze(item: dict):
        async with candle_semaphore:
            try:
                candles = await client.candles(item["symbol"], 30)
                return stock_from_candles(item["symbol"], item["name"], item["price"], candles), None
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                return None, f"{item['name']}: {exc}"

    loaded = await asyncio.gather(*(analyze(item) for item in safe))
    stocks = [stock for stock, _ in loaded if stock]
    warnings.extend(warning for _, warning in loaded if warning)
    stats = {"universe": len(universe), "affordable": len(affordable),
             "safe": len(safe), "analyzed": len(stocks)}
    return stocks, warnings, ranked_at, stats
