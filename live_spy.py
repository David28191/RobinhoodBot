"""
live_spy.py  --  SPY ACCUMULATOR live brain (paper mode by default)
===================================================================
The accumulator's version of live.py. It does NOT send a real order unless you
run it with --live AND have wired up the Robinhood MCP connection. By default it
runs in PAPER mode: it looks at SPY's current weekly z-score, decides what your
chosen strategy would do TODAY, logs the intended order(s), and updates a paper
share ledger -- so you can watch it behave for weeks before risking a cent.

The strategy it runs is the FIRST row of spy_accumulate.json's "strategies" (your
pick: buy 1.5sigma dips + trim 5% at +2.5sigma, with a steady weekly base buy):
  * BASE buy  : a fixed $ every week -> the protected CORE (never sold)
  * DIP buy   : extra $ when z <= -entry_z -> the opportunistic SLEEVE
  * TRIM      : sell trim_pct of the SLEEVE when z >= sell_z (core untouched)

SIZING: it spends only from its 60% sleeve of the bankroll (allocation.py). When
that sleeve runs low it warns you to top up total_capital in spy_accumulate.json.
All buys are dollar-sized and shares rounded to 6 decimals (Robinhood fractional).

Run:
    python live_spy.py            # paper mode (safe, default)
    python live_spy.py --live     # attempts real orders (requires MCP wiring + funds)
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys

import numpy as np

import pairbot
import allocation
import spy_accumulate as A

STATE_FILE = os.path.join(pairbot.DATA_DIR, "spy_positions.json")
ORDERS_LOG = os.path.join(pairbot.DATA_DIR, "spy_paper_orders.csv")
SHARE_DECIMALS = A.SHARE_DECIMALS


# ---------------------------------------------------------------------------
# 1) PAPER LEDGER STATE
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"core_shares": 0.0, "sleeve_shares": 0.0, "sleeve_basis": 0.0,
            "net_deployed": 0.0, "last_base_week": None, "last_action_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_order(rows):
    new_file = not os.path.exists(ORDERS_LOG)
    with open(ORDERS_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "action", "reason", "z", "side", "dollars",
                        "shares", "price", "total_shares_after"])
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# 2) EXECUTION (paper logging here; live is a gated stub)
# ---------------------------------------------------------------------------
def execute_live(order, mcp_url):
    """
    ===== STUB: wire up Robinhood Agentic Trading here =====
    Same setup as live.py: open/fund a dedicated Agentic Account, connect an
    agent, set ROBINHOOD_MCP_URL, and implement this against the MCP order tool.
    Accumulator orders are dollar-based market BUYs and fractional SELLs (no
    shorts), all regular-hours -- which is exactly what the fractional rules allow.
    Until implemented this raises on purpose so no real order can slip out.
    """
    raise NotImplementedError(
        "Live execution is not wired up yet. Provide ROBINHOOD_MCP_URL and implement "
        f"execute_live() against the Robinhood MCP order tool. (Would have sent: {order})")


# ---------------------------------------------------------------------------
# 3) DECIDE  --  same rules as the backtester, on the latest bar
# ---------------------------------------------------------------------------
def decide(cfg, strat, z_now, week_str, price, state, remaining):
    """Return a list of intended orders (dicts) for today. Mirrors
    backtest_accumulate: weekly BASE buy + cooldown-gated DIP buy / TRIM."""
    orders = []
    base = strat.get("base_buy_dollars", 0)
    sell_z = strat["sell_z"]
    trim_pct = strat["trim_pct"]
    cooldown = strat["cooldown_days"]
    cost_rate = cfg["cost_bps"] / 10000.0
    today = dt.date.today()

    # ---- BASE buy: once per week, into the protected core ----
    if base > 0 and state.get("last_base_week") != week_str:
        if remaining >= base:
            fee = base * cost_rate
            sh = round((base - fee) / price, SHARE_DECIMALS)
            orders.append({"action": "BASE_BUY", "side": "BUY", "reason": "weekly base buy",
                           "dollars": round(base, 2), "shares": sh, "bucket": "core",
                           "z": None})
            remaining -= base
        else:
            orders.append({"action": "SKIP", "side": "-", "reason": "base buy skipped: SPY sleeve out of cash",
                           "dollars": 0.0, "shares": 0.0, "bucket": "-", "z": None})

    # ---- opportunistic DIP / TRIM: cooldown-gated ----
    last = state.get("last_action_date")
    days_since = (today - dt.date.fromisoformat(last)).days if last else 10 ** 9
    if days_since >= cooldown and not np.isnan(z_now):
        dip_dollars = A.dip_buy_dollars(strat, z_now)          # bigger for deeper dips
        if dip_dollars > 0:                                    # LOW -> buy extra into sleeve
            if remaining >= dip_dollars:
                fee = dip_dollars * cost_rate
                sh = round((dip_dollars - fee) / price, SHARE_DECIMALS)
                orders.append({"action": "DIP_BUY", "side": "BUY", "reason": f"dip z={z_now:+.2f}",
                               "dollars": round(dip_dollars, 2), "shares": sh, "bucket": "sleeve",
                               "z": round(float(z_now), 2)})
            else:
                orders.append({"action": "SKIP", "side": "-", "reason": "dip buy skipped: SPY sleeve out of cash",
                               "dollars": 0.0, "shares": 0.0, "bucket": "-", "z": round(float(z_now), 2)})
        elif z_now >= sell_z and state["sleeve_shares"] > 0:   # HIGH -> trim sleeve only
            sell_sh = round(state["sleeve_shares"] * trim_pct, SHARE_DECIMALS)
            if sell_sh > 0:
                gross = sell_sh * price
                proceeds = gross - gross * cost_rate
                orders.append({"action": "TRIM", "side": "SELL", "reason": f"top z={z_now:+.2f}",
                               "dollars": round(proceeds, 2), "shares": sell_sh, "bucket": "sleeve",
                               "z": round(float(z_now), 2)})
    return orders


def apply_paper(order, state, week_str, price):
    """Update the paper ledger for one filled order."""
    today = dt.date.today().isoformat()
    if order["action"] == "BASE_BUY":
        state["core_shares"] = round(state["core_shares"] + order["shares"], SHARE_DECIMALS)
        state["net_deployed"] += order["dollars"]
        state["last_base_week"] = week_str
    elif order["action"] == "DIP_BUY":
        state["sleeve_shares"] = round(state["sleeve_shares"] + order["shares"], SHARE_DECIMALS)
        state["sleeve_basis"] += order["dollars"]
        state["net_deployed"] += order["dollars"]
        state["last_action_date"] = today
    elif order["action"] == "TRIM":
        # realized P&L just for the log; reduce sleeve + its basis proportionally
        avg = state["sleeve_basis"] / state["sleeve_shares"] if state["sleeve_shares"] else 0.0
        state["sleeve_basis"] -= avg * order["shares"]
        state["sleeve_shares"] = round(state["sleeve_shares"] - order["shares"], SHARE_DECIMALS)
        state["net_deployed"] -= order["dollars"]
        state["last_action_date"] = today


# ---------------------------------------------------------------------------
# 4) MAIN
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="SPY accumulator brain (paper by default).")
    ap.add_argument("--live", action="store_true",
                    help="Attempt REAL orders via Robinhood MCP (requires wiring + funded account).")
    args = ap.parse_args()
    print(f"=== SPY accumulator brain - {'LIVE (real orders)' if args.live else 'PAPER (simulated, safe)'} ===")

    cfg = A.load_config()
    strat = cfg["strategies"][0]                       # the headline 'YOUR PICK' strategy
    al = allocation.load_allocation()
    print(allocation.summary_line(al))
    print(f"Strategy: {strat['label']}")

    # latest SPY + current weekly z (2y is plenty for the 52-week vol lookback)
    print(f"Downloading latest {cfg['symbol']} ...")
    px = pairbot.fetch_prices([cfg["symbol"]], period="2y")[cfg["symbol"]].dropna()
    z = A.build_signals(cfg, px)[strat["signal"]]
    z_now = float(z.dropna().iloc[-1])
    price = float(px.iloc[-1])
    week_str = str(px.index[-1].to_period("W-FRI"))
    as_of = px.index[-1].date().isoformat()

    state = load_state()
    remaining = round(al["spy_budget"] - state["net_deployed"], 2)
    print(f"\nAs of {as_of}: {cfg['symbol']} ${price:,.2f}  weekly z {z_now:+.2f}sigma")
    print(f"Sleeve budget ${al['spy_budget']:,.2f}  -  deployed ${state['net_deployed']:,.2f}  "
          f"=  ${remaining:,.2f} left")

    orders = decide(cfg, strat, z_now, week_str, price, state, remaining)
    real = [o for o in orders if o["action"] != "SKIP"]
    skips = [o for o in orders if o["action"] == "SKIP"]
    for o in skips:
        print(f"  !! {o['reason']}")

    if not real:
        print("\nNo orders today (no new week / no dip / no top, or sleeve out of cash).")
        # still persist last_base_week if a base was attempted? No state change on no-op.
        _print_ledger(state, price, al)
        return

    print(f"\n{len(real)} intended order(s):")
    for o in real:
        print(f"  {o['action']:9s} {o['side']:4s} ${o['dollars']:>7,.2f}  "
              f"{o['shares']:.6f} sh @ ${price:,.2f}   [{o['reason']}]")

    if args.live:
        mcp_url = os.environ.get("ROBINHOOD_MCP_URL")
        if not mcp_url:
            print("\nRefusing to go live: ROBINHOOD_MCP_URL is not set. Aborting.", file=sys.stderr)
            sys.exit(1)
        for o in real:
            execute_live(o, mcp_url)                    # raises until you implement it
    else:
        rows = []
        for o in real:
            apply_paper(o, state, week_str, price)
            total_after = round(state["core_shares"] + state["sleeve_shares"], SHARE_DECIMALS)
            rows.append([as_of, o["action"], o["reason"], o["z"], o["side"],
                         o["dollars"], o["shares"], round(price, 2), total_after])
        log_order(rows)
        save_state(state)
        print(f"\nPaper orders logged to: {ORDERS_LOG}")
        _print_ledger(state, price, al)


def _print_ledger(state, price, al):
    core, sleeve = state["core_shares"], state["sleeve_shares"]
    total = core + sleeve
    value = total * price
    remaining = round(al["spy_budget"] - state["net_deployed"], 2)
    print(f"\nPaper ledger: core {core:.6f} sh + sleeve {sleeve:.6f} sh "
          f"= {total:.6f} sh  (${value:,.2f})")
    print(f"Net deployed ${state['net_deployed']:,.2f} of ${al['spy_budget']:,.2f} sleeve  "
          f"-> ${remaining:,.2f} left")
    if remaining < 50:
        print("  !! SPY sleeve is low — raise 'total_capital' in spy_accumulate.json (or add funds) to keep buying.")


if __name__ == "__main__":
    main()
