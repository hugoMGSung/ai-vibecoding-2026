from collections import defaultdict


def build_positions(trades: list[dict]) -> dict[str, dict]:
    positions: dict[str, dict] = defaultdict(lambda: {"quantity": 0, "cost": 0.0, "name": ""})
    for trade in sorted(trades, key=lambda x: x["id"]):
        position = positions[trade["symbol"]]
        position["name"] = trade["name"]
        quantity = int(trade["quantity"])
        if trade["side"] == "BUY":
            position["cost"] += quantity * int(trade["price"]) + int(trade.get("fee", 0))
            position["quantity"] += quantity
        elif position["quantity"] > 0:
            average = position["cost"] / position["quantity"]
            sold = min(quantity, position["quantity"])
            position["cost"] -= average * sold
            position["quantity"] -= sold
            if position["quantity"] == 0:
                position["cost"] = 0
    return {symbol: {"symbol": symbol, "name": item["name"], "quantity": item["quantity"],
                     "avg_price": round(item["cost"] / item["quantity"]) if item["quantity"] else 0}
            for symbol, item in positions.items() if item["quantity"] > 0}
