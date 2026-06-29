#!/usr/bin/env python3
"""Extract {date, close} from MCP get_equity_historicals JSON result files into data/mcp_prices.json.

Usage:
    python extract_mcp_prices.py <mcp_result_file.json> [<mcp_result_file2.json> ...]

The MCP result file is the saved output of a get_equity_historicals call
(schema: {data: {results: [{symbol, bars: [{begins_at, close_price, ...}]}]}, ...}).
Run once per batch; results accumulate into data/mcp_prices.json.
"""
import json
import sys
import os

OUT_FILE = os.path.join(os.path.dirname(__file__), "data", "mcp_prices.json")


def extract_from_file(filepath, prices_dict):
    with open(filepath) as f:
        raw = json.load(f)
    results = raw["data"]["results"]
    for r in results:
        symbol = r["symbol"]
        bars = r.get("bars", [])
        if not bars:
            print(f"  SKIP {symbol}: no bars")
            continue
        entries = [{"date": b["begins_at"][:10], "close": float(b["close_price"])} for b in bars]
        prices_dict[symbol] = entries
        print(f"  {symbol}: {len(entries)} bars ({entries[0]['date']} -> {entries[-1]['date']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <mcp_result_file.json> [...]")
        sys.exit(1)

    prices = {}
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE) as f:
            prices = json.load(f)

    for src in sys.argv[1:]:
        print(f"Processing {src} ...")
        extract_from_file(src, prices)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(prices, f)

    print(f"Total tickers in mcp_prices.json: {len(prices)}")
