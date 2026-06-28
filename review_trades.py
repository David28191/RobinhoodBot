"""
review_trades.py  --  hindsight analysis of REAL fills ("did the strategy add value?")
=======================================================================================
Closes the learning loop: it takes the orders the bot actually got FILLED on and,
with the benefit of hindsight (later prices), scores each one:

  * unrealized/realized return since the fill
  * MAE  -- worst drawdown after entry (how far underwater it went)
  * MFE  -- best gain available after entry
  * entry timing vs the LOCAL low/high in the `window` trading days AFTER the fill
    ("did we buy near a short-term bottom, or did it keep dropping right after?")
  * for buys: a "patience cost" = how much cheaper the next-`window`-day low was
    (positive = we could have waited and paid less)

It also benchmarks the whole thing against a naive "put the same dollars into SPY
on the same day" alternative, so you can see if the timing/selection beat just
buying the index.

Data-agnostic like cloud_decide.py, so it can run in the cloud too:
  data/fills.json       [{ "symbol","side","dollars","qty","price","date","strategy"? }, ...]
                         (the agent builds this from Robinhood get_equity_orders,
                          placed_agent='agentic', state='filled')
  data/mcp_prices.json  {ticker:[{"date","close"},...], ...}  (incl. SPY for the benchmark)

  python review_trades.py [--window 10]

Output: console scorecard + data/trade_review.json. RESEARCH ONLY -- it suggests,
it does not place anything.
"""

import argparse
import json
import os

import numpy as np
import pandas as pd

import pairbot

DATA = pairbot.DATA_DIR
FILLS_FILE = os.path.join(DATA, "fills.json")
PRICES_FILE = os.path.join(DATA, "mcp_prices.json")
OUT_FILE = os.path.join(DATA, "trade_review.json")


def series_for(prices_json, ticker):
    bars = prices_json.get(ticker)
    if not bars:
        return None
    s = pd.Series({pd.Timestamp(b["date"]): float(b["close"]) for b in bars}).sort_index()
    return s


def score_fill(f, prices_json, spy, window):
    sym = f["symbol"]
    s = series_for(prices_json, sym)
    if s is None or s.empty:
        return {**f, "note": "no price data"}
    entry = float(f["price"])
    qty = float(f.get("qty") or (float(f["dollars"]) / entry))
    dollars = float(f.get("dollars") or qty * entry)
    sign = 1 if f["side"].lower() == "buy" else -1

    after = s[s.index > pd.Timestamp(f["date"])]
    now = float(s.iloc[-1])
    ret = (now / entry - 1) * sign
    pnl = qty * (now - entry) * sign

    win = after.iloc[:window] if len(after) else pd.Series(dtype=float)
    lo = float(win.min()) if len(win) else np.nan
    hi = float(win.max()) if len(win) else np.nan
    # MAE/MFE over the FULL post-entry path
    mae = (float(after.min()) / entry - 1) * sign if len(after) else np.nan   # worst
    mfe = (float(after.max()) / entry - 1) * sign if len(after) else np.nan   # best
    # patience cost for BUYS: how much cheaper the next-window low was
    patience = (entry / lo - 1) if (sign == 1 and not np.isnan(lo) and lo > 0) else np.nan

    # benchmark: same dollars into SPY on the same day
    bench = None
    if spy is not None and not spy.empty:
        spy_after = spy[spy.index >= pd.Timestamp(f["date"])]
        if len(spy_after):
            spy_entry = float(spy_after.iloc[0])
            spy_now = float(spy.iloc[-1])
            bench = (spy_now / spy_entry - 1)

    return {
        "date": f["date"], "symbol": sym, "side": f["side"], "strategy": f.get("strategy", "?"),
        "dollars": round(dollars, 2), "entry": round(entry, 4), "price_now": round(now, 4),
        "return_pct": round(ret * 100, 2), "pnl_$": round(pnl, 2),
        "MAE_pct": None if np.isnan(mae) else round(mae * 100, 2),
        "MFE_pct": None if np.isnan(mfe) else round(mfe * 100, 2),
        "patience_cost_pct": None if (patience is None or np.isnan(patience)) else round(patience * 100, 2),
        "spy_same_day_pct": None if bench is None else round(bench * 100, 2),
        "vs_spy_pct": None if bench is None else round((ret - bench) * 100, 2),
    }


def observations(rows):
    out = []
    buys = [r for r in rows if r["side"].lower() == "buy" and r.get("return_pct") is not None]
    if not buys:
        return ["No scored buys yet."]
    avg_ret = np.mean([r["return_pct"] for r in buys])
    win_rate = np.mean([1 if r["return_pct"] > 0 else 0 for r in buys]) * 100
    out.append(f"{len(buys)} buys scored: avg return {avg_ret:+.2f}%, win rate {win_rate:.0f}%.")
    pcs = [r["patience_cost_pct"] for r in buys if r.get("patience_cost_pct") is not None]
    if pcs:
        ap = np.mean(pcs)
        if ap > 0.5:
            out.append(f"Entries averaged {ap:.2f}% above the next-{'%d'%10}-day low — "
                       f"buying on slightly DEEPER dips (raise entry_z) may have paid less.")
        else:
            out.append(f"Entry timing was tight (avg only {ap:.2f}% above the local low).")
    vss = [r["vs_spy_pct"] for r in buys if r.get("vs_spy_pct") is not None]
    if vss:
        av = np.mean(vss)
        verb = "BEAT" if av > 0 else "TRAILED"
        out.append(f"On average these picks {verb} a same-day SPY buy by {abs(av):.2f}%.")
    return out


def main():
    ap = argparse.ArgumentParser(description="Hindsight trade review (research only).")
    ap.add_argument("--window", type=int, default=10, help="Trading days after a fill for local low/high.")
    args = ap.parse_args()

    with open(FILLS_FILE) as f:
        fills = json.load(f)
    with open(PRICES_FILE) as f:
        prices_json = json.load(f)
    spy = series_for(prices_json, "SPY")

    rows = [score_fill(f, prices_json, spy, args.window) for f in fills]
    scored = [r for r in rows if r.get("return_pct") is not None]
    total_pnl = round(sum(r["pnl_$"] for r in scored), 2)
    invested = round(sum(r["dollars"] for r in scored), 2)

    result = {
        "fills_reviewed": len(rows),
        "invested_$": invested,
        "open_pnl_$": total_pnl,
        "open_return_pct": round(total_pnl / invested * 100, 2) if invested else None,
        "observations": observations(rows),
        "trades": rows,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print("=== HINDSIGHT TRADE REVIEW ===")
    cols = ["date", "symbol", "side", "dollars", "entry", "price_now",
            "return_pct", "MAE_pct", "MFE_pct", "patience_cost_pct", "vs_spy_pct"]
    df = pd.DataFrame(rows)
    if not df.empty:
        for c in cols:
            if c not in df:
                df[c] = None
        print(df[cols].to_string(index=False))
    print(f"\nInvested ${invested:,.2f}  |  open P&L ${total_pnl:+,.2f}  "
          f"({result['open_return_pct']:+}%)" if invested else "\nNo scored trades.")
    print("\nObservations:")
    for o in result["observations"]:
        print("  -", o)
    print(f"\nWrote {OUT_FILE}  (research only — no orders placed)")


if __name__ == "__main__":
    main()
