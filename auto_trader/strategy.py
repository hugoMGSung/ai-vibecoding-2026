from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

from .models import Candle
from .settings import settings


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    ma5: Decimal | None
    ma20: Decimal | None
    rsi: Decimal | None
    volume_ratio: Decimal | None
    sell_fraction: Decimal = Decimal("1")


def evaluate(
    candles: list[Candle],
    entry_price: Decimal | None = None,
    high_water: Decimal | None = None,
    holding_days: int = 0,
    partial_profit_taken: bool = False,
    current_price: Decimal | None = None,
) -> Signal:
    if len(candles) < 21:
        return Signal("WAIT", 0, "일봉 데이터가 21개 미만", None, None, None, None)
    ordered = list(reversed(candles))
    closes = [Decimal(c.close) for c in ordered]
    volumes = [Decimal(c.volume) for c in ordered]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    gains, losses = [], []
    for before, after in zip(closes[-15:-1], closes[-14:]):
        change = after - before
        gains.append(max(change, Decimal("0")))
        losses.append(max(-change, Decimal("0")))
    avg_gain, avg_loss = sum(gains) / 14, sum(losses) / 14
    rsi = Decimal("100") if avg_loss == 0 else Decimal("100") - (Decimal("100") / (Decimal("1") + avg_gain / avg_loss))
    average_volume = sum(volumes[-21:-1]) / 20
    volume_ratio = volumes[-1] / average_volume if average_volume else Decimal("0")
    current = current_price if current_price is not None else closes[-1]
    if entry_price and entry_price > 0:
        return_rate = (current - entry_price) / entry_price
        if return_rate <= settings.stop_loss_rate:
            return Signal("SELL", 100, "손절 기준 도달", ma5, ma20, rsi, volume_ratio)
        if holding_days >= settings.max_holding_days:
            return Signal("SELL", 100, "최대 보유 기간 도달", ma5, ma20, rsi, volume_ratio)
        if high_water and high_water >= entry_price * (Decimal("1") + settings.trailing_stop_activation_rate):
            if current <= high_water * (Decimal("1") - settings.trailing_stop_rate):
                return Signal("SELL", 100, "추적 손절 기준 도달", ma5, ma20, rsi, volume_ratio)
        if return_rate >= settings.take_profit_rate:
            return Signal("SELL", 100, "익절 기준 도달", ma5, ma20, rsi, volume_ratio)
        if return_rate >= settings.partial_take_profit_rate and not partial_profit_taken:
            return Signal("SELL", 75, "1차 목표 수익 도달", ma5, ma20, rsi, volume_ratio, settings.partial_take_profit_fraction)
        if current < ma20 and ma5 < ma20:
            return Signal("SELL", 80, "MA20 하향 이탈", ma5, ma20, rsi, volume_ratio)
    conditions = [closes[-1] > ma20, ma5 > ma20, Decimal("30") <= rsi <= Decimal("70"), volume_ratio >= Decimal("1")]
    score = sum(25 for condition in conditions)
    action = "BUY" if score == 100 else "WAIT"
    reason = "전략 조건 충족" if action == "BUY" else "MA5/MA20, RSI, 거래량 조건 미충족"
    return Signal(action, score, reason, ma5, ma20, rsi, volume_ratio)


def _tick_size(price: Decimal) -> Decimal:
    if price < 2000: return Decimal("1")
    if price < 5000: return Decimal("5")
    if price < 20000: return Decimal("10")
    if price < 50000: return Decimal("50")
    if price < 200000: return Decimal("100")
    if price < 500000: return Decimal("500")
    return Decimal("1000")


def _simulated_fill(price: Decimal, side: str, slippage_rate: Decimal) -> Decimal:
    tick = _tick_size(price)
    adjusted = price * (Decimal("1") + slippage_rate if side == "BUY" else Decimal("1") - slippage_rate)
    rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    return (adjusted / tick).to_integral_value(rounding=rounding) * tick


def backtest(candles: list[Candle], initial_cash: Decimal, commission_rate: Decimal, slippage_rate: Decimal = Decimal("0")) -> dict:
    """Simple daily-bar simulation; signals use completed bars and fill on next open."""
    ordered = list(reversed(candles))
    if len(ordered) < 22:
        return {"error": "백테스트에는 최소 22개 일봉이 필요합니다."}
    cash = initial_cash
    quantity = Decimal("0")
    average_cost = Decimal("0")
    high_water = Decimal("0")
    entry_day = 0
    curve: list[Decimal] = [initial_cash]
    trades: list[dict] = []
    partial_done = False
    for index in range(21, len(ordered)):
        signal = evaluate(list(reversed(ordered[:index])))
        bar = ordered[index]
        fill = Decimal(bar.open)
        if quantity == 0 and signal.action == "BUY":
            avg_volume = sum((Decimal(item.volume) for item in ordered[max(0, index - 20):index]), Decimal("0")) / Decimal("20")
            avg_turnover = sum((Decimal(item.volume) * Decimal(item.close) for item in ordered[max(0, index - 20):index]), Decimal("0")) / Decimal("20")
            if avg_volume < settings.paper_min_average_daily_volume or avg_turnover < settings.paper_min_average_daily_turnover:
                curve.append(cash)
                continue
            fill = _simulated_fill(fill, "BUY", slippage_rate)
            budget = cash * settings.recommended_trade_ratio
            buy_quantity = (budget / (fill * (Decimal("1") + commission_rate))).to_integral_value(rounding=ROUND_FLOOR)
            if buy_quantity > 0:
                amount = buy_quantity * fill
                fee = (amount * commission_rate).quantize(Decimal("0.01"))
                cash -= amount + fee
                quantity = buy_quantity
                average_cost = (amount + fee) / quantity
                high_water = fill
                entry_day = index
                partial_done = False
                trades.append({"side": "BUY", "date": bar.timestamp, "price": str(fill), "quantity": str(buy_quantity), "reason": signal.reason})
        elif quantity > 0:
            # Today's high is unknown when the signal is evaluated at the prior close.
            high_water = max(high_water, Decimal(ordered[index - 1].high))
            held_days = index - entry_day
            held_candles = list(reversed(ordered[:index]))
            exit_signal = evaluate(held_candles, average_cost, high_water, held_days, partial_done, fill)
            if settings.force_flatten_at_close and index == len(ordered) - 1:
                fill = Decimal(bar.close)
                exit_signal.action, exit_signal.reason, exit_signal.sell_fraction = "SELL", "백테스트 마지막 일자 청산", Decimal("1")
            if exit_signal.action == "SELL":
                sell_quantity = quantity if exit_signal.sell_fraction >= 1 else max(Decimal("1"), (quantity * exit_signal.sell_fraction).to_integral_value(rounding=ROUND_FLOOR))
                sell_quantity = min(sell_quantity, quantity)
                fill = _simulated_fill(fill, "SELL", slippage_rate)
                amount = sell_quantity * fill
                fee = (amount * commission_rate).quantize(Decimal("0.01"))
                cash += amount - fee
                quantity -= sell_quantity
                trades.append({"side": "SELL", "date": bar.timestamp, "price": str(fill), "quantity": str(sell_quantity), "reason": exit_signal.reason})
                if quantity == 0:
                    average_cost = Decimal("0")
                    partial_done = False
                else:
                    partial_done = True
        curve.append(cash + quantity * Decimal(bar.close))
    final_value = cash + quantity * Decimal(ordered[-1].close)
    peak = curve[0]
    max_drawdown = Decimal("0")
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    sells = [item for item in trades if item["side"] == "SELL"]
    replay_quantity = Decimal("0")
    closed_positions = 0
    for item in trades:
        item_quantity = Decimal(item["quantity"])
        replay_quantity += item_quantity if item["side"] == "BUY" else -item_quantity
        if item["side"] == "SELL" and replay_quantity <= 0:
            closed_positions += 1
            replay_quantity = Decimal("0")
    return {
        "period_start": ordered[0].timestamp,
        "period_end": ordered[-1].timestamp,
        "initial_cash": initial_cash,
        "final_value": final_value.quantize(Decimal("0.01")),
        "return_rate": (((final_value - initial_cash) / initial_cash * 100) if initial_cash else Decimal("0")).quantize(Decimal("0.01")),
        "max_drawdown_rate": (max_drawdown * 100).quantize(Decimal("0.01")),
        "order_count": len(trades),
        "sell_order_count": len(sells),
        "closed_position_count": closed_positions,
        "remaining_quantity": quantity,
        "trades": trades,
    }
