from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class Stock:
    symbol: str
    name: str
    price: int
    ma5: float
    ma20: float
    volume_ratio: float
    rsi14: float
    trading_amount: int
    warning: bool = False


SAMPLE_STOCKS = [
    Stock("000660", "SK하이닉스", 178000, 176200, 170500, 1.35, 58, 510_000_000_000),
    Stock("005930", "삼성전자", 72500, 71600, 70100, 1.62, 61, 890_000_000_000),
    Stock("034020", "두산에너빌리티", 24800, 24200, 23100, 1.78, 64, 122_000_000_000),
    Stock("010140", "삼성중공업", 13700, 13450, 12800, 1.54, 57, 98_000_000_000),
    Stock("028300", "HLB", 43800, 42500, 41700, 1.41, 55, 65_000_000_000),
    Stock("003520", "영진약품", 2450, 2380, 2410, 2.10, 69, 8_200_000_000),
    Stock("900001", "위험종목 예시", 980, 1050, 1200, 3.8, 82, 400_000_000, True),
]


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        raise ValueError("RSI 계산에 필요한 종가가 부족합니다.")
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-period:]
    gains = sum(max(change, 0) for change in changes) / period
    losses = sum(max(-change, 0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - (100 / (1 + gains / losses))


def stock_from_candles(symbol: str, name: str, price: int, candles: list[dict]) -> Stock:
    def number(row: dict, *keys: str) -> float:
        for key in keys:
            if row.get(key) is not None:
                return float(row[key])
        raise ValueError(f"일봉에 {keys[0]} 값이 없습니다.")

    rows = sorted(candles, key=lambda x: x.get("timestamp") or x.get("date") or "")
    closes = [number(row, "close", "closePrice") for row in rows]
    volumes = [number(row, "volume", "accumulatedVolume") for row in rows]
    if len(closes) < 20:
        raise ValueError("이동평균 계산에 필요한 일봉이 부족합니다.")
    kst_today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    latest_date = (rows[-1].get("timestamp") or rows[-1].get("date") or "")[:10]
    completed_volumes = volumes[:-1] if latest_date == kst_today and len(volumes) > 20 else volumes
    avg_volume20 = sum(completed_volumes[-20:]) / 20
    latest_volume = completed_volumes[-1]
    return Stock(
        symbol=symbol,
        name=name,
        price=price,
        ma5=sum(closes[-5:]) / 5,
        ma20=sum(closes[-20:]) / 20,
        volume_ratio=latest_volume / avg_volume20 if avg_volume20 else 0,
        rsi14=calculate_rsi(closes),
        trading_amount=int(price * latest_volume),
    )


def recommend_stocks(stocks: list[Stock], budget: int, limit: int = 5) -> list[dict]:
    max_price = int(budget * 0.30)
    results = []
    for stock in stocks:
        if stock.warning or stock.price > max_price or stock.trading_amount < 1_000_000_000:
            continue

        score, reasons, risks = 0, [], []
        if stock.price > stock.ma5:
            score += 20
            reasons.append("현재가가 5일 이동평균 위")
        if stock.ma5 > stock.ma20:
            score += 20
            reasons.append("5일선이 20일선 위")
        if stock.volume_ratio >= 1.5:
            score += 20
            reasons.append(f"거래량이 20일 평균의 {stock.volume_ratio:.1f}배")
        elif stock.volume_ratio >= 1.2:
            score += 10
        if 40 <= stock.rsi14 <= 65:
            score += 20
            reasons.append(f"RSI {stock.rsi14:.0f}, 과열 전 구간")
        elif stock.rsi14 > 65:
            risks.append("단기 과열 가능성")
        if stock.trading_amount >= 10_000_000_000:
            score += 20
            reasons.append("거래대금 유동성 양호")
        else:
            score += 10

        item = asdict(stock)
        item.update(score=score, reasons=reasons, risks=risks,
                    buyable_quantity=min(budget // stock.price, max(1, int(budget * .30) // stock.price)))
        results.append(item)

    return sorted(results, key=lambda x: (-x["score"], -x["trading_amount"]))[:limit]


def recommend(budget: int, limit: int = 5) -> list[dict]:
    return recommend_stocks(SAMPLE_STOCKS, budget, limit)
