from dataclasses import dataclass
from decimal import Decimal

from .models import Candle


@dataclass
class Signal:
    action: str
    score: int
    reason: str
    ma5: Decimal | None
    ma20: Decimal | None
    rsi: Decimal | None
    volume_ratio: Decimal | None


def evaluate(candles: list[Candle]) -> Signal:
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
    conditions = [closes[-1] > ma20, ma5 > ma20, Decimal("30") <= rsi <= Decimal("70"), volume_ratio >= Decimal("1")]
    score = sum(25 for condition in conditions)
    action = "BUY" if score == 100 else "WAIT"
    reason = "전략 조건 충족" if action == "BUY" else "MA5/MA20, RSI, 거래량 조건 미충족"
    return Signal(action, score, reason, ma5, ma20, rsi, volume_ratio)
