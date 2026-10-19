"""Extract {date, close} bars from a raw historicals batch JSON and merge into mcp_prices.json."""
import json, sys, os

raw_file = sys.argv[1]
prices_file = sys.argv[2] if len(sys.argv) > 2 else "/home/user/RobinhoodBot/data/mcp_prices.json"

with open(raw_file) as f:
    raw = json.load(f)

results = raw["data"]["results"]

# Load existing prices if any
if os.path.exists(prices_file):
    with open(prices_file) as f:
        prices = json.load(f)
else:
    prices = {}

extracted = []
for r in results:
    sym = r["symbol"]
    bars = r.get("bars", [])
    if not bars:
        print(f"  SKIP {sym}: no bars")
        continue
    series = [{"date": b["begins_at"][:10], "close": float(b["close_price"])} for b in bars]
    prices[sym] = series
    extracted.append(sym)

with open(prices_file, "w") as f:
    json.dump(prices, f)

print(f"  Extracted: {extracted}")
print(f"  Total tickers in prices file: {len(prices)}")
