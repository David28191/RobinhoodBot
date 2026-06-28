"""
cloud_decide.py  --  data-agnostic decision brain for COMPUTER-OFF (cloud) trading
==================================================================================
This is the piece that lets the bot trade with your PC OFF. A cloud routine
(Anthropic-hosted, scheduled) can reach the Robinhood MCP but CANNOT use yfinance
(the sandbox blocks Yahoo) and CANNOT see your Desktop files. So this script:

  * gets NO data itself -- it reads prices the cloud agent already pulled from the
    Robinhood MCP (get_equity_historicals) and dropped into data/mcp_prices.json
  * reuses the EXACT same decision logic as live.py / live_spy.py (it imports their
    pure decide() functions + pairbot.compute_spread + spy_accumulate signals), so
    the orders it proposes match what your local scripts would propose
  * reads account state (cash + open positions + SPY ledger) from data/live_state.json
  * prints a JSON list of INTENDED ORDERS and writes data/intended_orders.json

It NEVER touches the broker. Execution is the cloud agent's job: for each intended
order it calls review_equity_order -> place_equity_order using the ref_id here.

INPUT FILES (the cloud agent builds these from MCP calls):
  data/mcp_prices.json  {ticker: [{"date":"YYYY-MM-DD","close":float}, ...], ...}
  data/live_state.json  {
      "cash": float,                         # from get_portfolio buying_power
      "account_number": "<your agentic account number>",
      "pairs_positions": { <positions.json shape> },
      "spy": { <spy_positions.json shape> }, # core/sleeve ledger carried between runs
      "shares": { ticker: float, ... }       # real shares held (from get_equity_positions)
  }

  python cloud_decide.py        # decide; prints + writes intended_orders.json
"""

import datetime as dt
import json
import os
import uuid

import numpy as np
import pandas as pd

import pairbot
import allocation
import live                      # reuse live.decide (pairs) -- pure function
import live_spy                  # reuse live_spy.decide (SPY) -- pure function
import spy_accumulate as A

DATA = pairbot.DATA_DIR
PRICES_FILE = os.path.join(DATA, "mcp_prices.json")
STATE_FILE = os.path.join(DATA, "live_state.json")
OUT_FILE = os.path.join(DATA, "intended_orders.json")


# ---------------------------------------------------------------------------
# Prices: build the SAME DataFrame shape pairbot.fetch_prices() returns, but
# from the MCP JSON instead of yfinance.
# ---------------------------------------------------------------------------
def prices_df(prices_json):
    cols = {}
    for ticker, bars in prices_json.items():
        s = pd.Series(
            {pd.Timestamp(b["date"]): float(b["close"]) for b in bars}
        ).sort_index()
        cols[ticker] = s
    df = pd.DataFrame(cols).sort_index()
    return df.dropna(how="all").ffill()


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------
def decide_pairs(cfg, close, state, today):
    """Mirror live.main()'s pair loop (budget + max_positions gating included)."""
    traded = cfg["pairs"]
    positions = state.get("pairs_positions", {})
    al = allocation.load_allocation()
    pairs_budget = al["pairs_budget"]
    deployed = sum(leg["dollars"] for p in positions.values()
                   for leg in p["legs"] if leg["side"] == "BUY")
    max_positions = cfg.get("max_positions")
    open_slots = (max_positions - len(positions)) if max_positions is not None else None

    orders, notes = [], []
    for pair in traded:
        try:
            z = float(pairbot.compute_spread(close, pair)["z"].dropna().iloc[-1])
        except (KeyError, IndexError):
            notes.append(f"{pair['name']}: no data — skipped")
            continue
        order = live.decide(pair, z, positions.get(pair["name"]), today)
        if order is None:
            notes.append(f"{pair['name']}: z={z:+.2f} no action")
            continue
        cost = sum(l["dollars"] for l in order["legs"] if l["side"] == "BUY")
        if order["action"] == "OPEN" and open_slots is not None and open_slots <= 0:
            notes.append(f"{pair['name']}: OPEN skipped (at max {max_positions})")
            continue
        if order["action"] == "OPEN" and deployed + cost > pairs_budget:
            notes.append(f"{pair['name']}: OPEN skipped (over ${pairs_budget:.0f} budget)")
            continue
        notes.append(f"{pair['name']}: z={z:+.2f} -> {order['action']} ({order['reason']})")
        orders.append(order)
        if order["action"] == "OPEN":
            if open_slots is not None:
                open_slots -= 1
            deployed += cost
        else:
            deployed -= sum(l["dollars"] for l in order["legs"] if l["side"] == "SELL")
    return orders, notes


def decide_spy(cfg_acc, close, state):
    """Mirror live_spy.main()'s single-strategy decision."""
    strat = cfg_acc["strategies"][0]
    al = allocation.load_allocation()
    px = close[cfg_acc["symbol"]].dropna()
    z = A.build_signals(cfg_acc, px)[strat["signal"]]
    z_now = float(z.dropna().iloc[-1])
    price = float(px.iloc[-1])
    week_str = str(px.index[-1].to_period("W-FRI"))
    sp = state.get("spy", {"core_shares": 0.0, "sleeve_shares": 0.0, "sleeve_basis": 0.0,
                           "net_deployed": 0.0, "last_base_week": None, "last_action_date": None})
    remaining = round(al["spy_budget"] - sp.get("net_deployed", 0.0), 2)
    orders = live_spy.decide(cfg_acc, strat, z_now, week_str, price, sp, remaining)
    real = [o for o in orders if o["action"] != "SKIP"]
    notes = [f"SPY z={z_now:+.2f} price=${price:.2f} week={week_str} -> "
             + (", ".join(f"{o['action']} ${o['dollars']:.2f}" for o in real) if real else "no order")]
    return real, price, week_str, notes


# ---------------------------------------------------------------------------
# Turn internal decisions into broker-ready intended orders (dollar/share sized)
# ---------------------------------------------------------------------------
def to_broker_orders(pair_orders, spy_orders, spy_price, state, account):
    shares = state.get("shares", {})
    out = []

    # --- pairs (long_only: legs are BUY $ / SELL $) ---
    for o in pair_orders:
        for leg in o["legs"]:
            base = {"ref_id": str(uuid.uuid4()), "source": "pairs",
                    "pair": o["pair"], "reason": f"{o['action']}:{o['reason']}",
                    "account_number": account, "symbol": leg["ticker"], "type": "market"}
            if leg["side"] == "BUY":
                out.append({**base, "side": "buy", "dollar_amount": f"{leg['dollars']:.2f}"})
            elif leg["side"] == "SELL":
                # CLOSE: sell the FULL real position of this ticker (use live shares)
                q = shares.get(leg["ticker"])
                sell = {**base, "side": "sell", "sell_full_position": True}
                if q:
                    sell["quantity"] = f"{float(q):.6f}"
                out.append(sell)
            else:
                out.append({**base, "side": leg["side"].lower(), "note": "SHORT/COVER unsupported in cash acct"})

    # --- SPY accumulator ---
    for o in spy_orders:
        base = {"ref_id": str(uuid.uuid4()), "source": "spy",
                "reason": o["reason"], "account_number": account,
                "symbol": "SPY", "type": "market"}
        if o["side"] == "BUY":
            out.append({**base, "side": "buy", "dollar_amount": f"{o['dollars']:.2f}",
                        "bucket": o.get("bucket")})
        elif o["side"] == "SELL":      # TRIM -> sell a share quantity
            out.append({**base, "side": "sell", "quantity": f"{o['shares']:.6f}",
                        "bucket": o.get("bucket")})
    return out


def cash_guard(orders, cash):
    """Drop buys that don't fit remaining real cash (most-conservative)."""
    kept, dropped, remaining = [], [], float(cash)
    for o in orders:
        if o["side"] == "buy":
            amt = float(o.get("dollar_amount", 0))
            if amt > remaining + 1e-9:
                dropped.append({**o, "dropped_reason": f"insufficient cash (${remaining:.2f} left)"})
                continue
            remaining -= amt
        kept.append(o)
    return kept, dropped, remaining


def main():
    with open(PRICES_FILE) as f:
        prices_json = json.load(f)
    with open(STATE_FILE) as f:
        state = json.load(f)

    account = state.get("account_number") or os.environ.get("ROBINHOOD_ACCOUNT")
    if not account:
        raise SystemExit("No account_number in live_state.json and ROBINHOOD_ACCOUNT not set.")
    close = prices_df(prices_json)
    today = dt.date.today()

    cfg_pairs = pairbot.load_config()
    cfg_acc = A.load_config()

    pair_orders, pair_notes = decide_pairs(cfg_pairs, close, state, today)
    spy_orders, spy_price, spy_week, spy_notes = decide_spy(cfg_acc, close, state)

    broker = to_broker_orders(pair_orders, spy_orders, spy_price, state, account)
    broker, dropped, cash_left = cash_guard(broker, state.get("cash", 0))

    # Optimistic post-trade state (assumes the market orders fill) so the live
    # routine can persist an accurate ledger AFTER it confirms the places.
    import copy
    new_state = copy.deepcopy(state)
    sp = new_state.setdefault("spy", {"core_shares": 0.0, "sleeve_shares": 0.0,
                                      "sleeve_basis": 0.0, "net_deployed": 0.0,
                                      "last_base_week": None, "last_action_date": None})
    for o in spy_orders:
        live_spy.apply_paper(o, sp, spy_week, spy_price)
    pos = new_state.setdefault("pairs_positions", {})
    for o in pair_orders:
        if o["action"] == "OPEN":
            pos[o["pair"]] = {"direction": o["direction"], "entry_date": today.isoformat(),
                              "entry_z": o["z"], "legs": o["legs"]}
        elif o["action"] == "CLOSE":
            pos.pop(o["pair"], None)
    with open(os.path.join(DATA, "updated_state.json"), "w") as f:
        json.dump(new_state, f, indent=2)

    result = {
        "as_of": str(close.index[-1].date()),
        "account_number": account,
        "cash_before": state.get("cash"),
        "cash_after_est": round(cash_left, 2),
        "notes": spy_notes + pair_notes,
        "intended_orders": broker,
        "dropped_orders": dropped,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))
    print(f"\n[{len(broker)} intended order(s); {len(dropped)} dropped] -> {OUT_FILE}")


if __name__ == "__main__":
    main()
