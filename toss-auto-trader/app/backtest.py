from dataclasses import dataclass

from app.strategy import calculate_rsi


@dataclass(frozen=True)
class BacktestRules:
    initial_cash: int = 1_000_000
    stop_loss_rate: float = 0.05
    take_profit_rate: float = 0.10
    max_holding_days: int = 20
    buy_fee_rate: float = 0.0
    sell_fee_rate: float = 0.0
    sell_tax_rate: float = 0.0


def _number(row: dict, *keys: str) -> float:
    for key in keys:
        if row.get(key) is not None:
            return float(row[key])
    raise ValueError(f"일봉에 {keys[0]} 값이 없습니다.")


def run_backtest(candles: list[dict], rules: BacktestRules) -> dict:
    """종가 신호 후 다음 거래일 시가 매수, 일중 손절·익절을 보수적으로 평가한다."""
    rows = sorted(candles, key=lambda x: x.get("timestamp") or x.get("date") or "")
    if len(rows) < 30:
        raise ValueError("백테스트에는 최소 30개의 일봉이 필요합니다.")
    if rules.initial_cash <= 0 or not 0 < rules.stop_loss_rate < 1 or not 0 < rules.take_profit_rate < 1:
        raise ValueError("백테스트 설정값이 올바르지 않습니다.")

    closes = [_number(row, "closePrice", "close") for row in rows]
    cash, quantity = float(rules.initial_cash), 0
    entry_price, entry_index, entry_cost = 0.0, -1, 0.0
    trades: list[dict] = []
    equity_curve: list[float] = [cash]

    for index in range(20, len(rows)):
        row = rows[index]
        close = closes[index]

        if quantity:
            low = _number(row, "lowPrice", "low")
            high = _number(row, "highPrice", "high")
            stop_price = entry_price * (1 - rules.stop_loss_rate)
            target_price = entry_price * (1 + rules.take_profit_rate)
            exit_price, reason = None, None
            # 같은 일봉에서 둘 다 닿으면 손절이 먼저 발생한 것으로 처리한다.
            if low <= stop_price:
                exit_price, reason = stop_price, "STOP_LOSS"
            elif high >= target_price:
                exit_price, reason = target_price, "TAKE_PROFIT"
            elif index - entry_index >= rules.max_holding_days:
                exit_price, reason = close, "MAX_HOLDING"

            if exit_price is not None:
                gross = exit_price * quantity
                costs = gross * (rules.sell_fee_rate + rules.sell_tax_rate)
                proceeds = gross - costs
                pnl = proceeds - entry_cost
                cash += proceeds
                trades.append({
                    "entry_date": (rows[entry_index].get("timestamp") or rows[entry_index].get("date"))[:10],
                    "exit_date": (row.get("timestamp") or row.get("date"))[:10],
                    "entry_price": round(entry_price), "exit_price": round(exit_price),
                    "quantity": quantity, "pnl": round(pnl),
                    "return_rate": pnl / entry_cost if entry_cost else 0, "reason": reason,
                })
                quantity, entry_price, entry_index, entry_cost = 0, 0.0, -1, 0.0

        # 신호 당일 평가자산을 기록한 뒤 다음 거래일 시가 주문을 예약한다.
        equity_curve.append(cash + quantity * close)

        if not quantity and index < len(rows) - 1:
            ma5 = sum(closes[index - 4:index + 1]) / 5
            ma20 = sum(closes[index - 19:index + 1]) / 20
            rsi14 = calculate_rsi(closes[:index + 1])
            if close > ma5 > ma20 and 40 <= rsi14 <= 65:
                next_row = rows[index + 1]
                buy_price = _number(next_row, "openPrice", "open")
                buy_fee_per_share = buy_price * rules.buy_fee_rate
                quantity = int(cash // (buy_price + buy_fee_per_share))
                if quantity:
                    entry_price, entry_index = buy_price, index + 1
                    entry_cost = quantity * (buy_price + buy_fee_per_share)
                    cash -= entry_cost

    if quantity:
        exit_price = closes[-1]
        gross = exit_price * quantity
        proceeds = gross * (1 - rules.sell_fee_rate - rules.sell_tax_rate)
        pnl = proceeds - entry_cost
        cash += proceeds
        trades.append({
            "entry_date": (rows[entry_index].get("timestamp") or rows[entry_index].get("date"))[:10],
            "exit_date": (rows[-1].get("timestamp") or rows[-1].get("date"))[:10],
            "entry_price": round(entry_price), "exit_price": round(exit_price),
            "quantity": quantity, "pnl": round(pnl),
            "return_rate": pnl / entry_cost if entry_cost else 0, "reason": "PERIOD_END",
        })

    peak, max_drawdown = equity_curve[0], 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    wins = sum(1 for trade in trades if trade["pnl"] > 0)
    return {
        "initial_cash": rules.initial_cash,
        "final_equity": round(cash),
        "total_pnl": round(cash - rules.initial_cash),
        "total_return_rate": cash / rules.initial_cash - 1,
        "max_drawdown": max_drawdown,
        "trade_count": len(trades),
        "win_rate": wins / len(trades) if trades else 0,
        "trades": list(reversed(trades[-20:])),
        "start_date": (rows[0].get("timestamp") or rows[0].get("date"))[:10],
        "end_date": (rows[-1].get("timestamp") or rows[-1].get("date"))[:10],
    }
