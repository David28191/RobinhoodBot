"""
live.py  --  Phase 3: the live-trading BRAIN (paper mode by default)
====================================================================
This is the piece that would actually place trades. It does NOT send a single
real order unless you explicitly run it with --live AND have wired up the
Robinhood MCP connection (a stub you must fill in). By default it runs in
PAPER mode: it computes today's signals for your TRADED pairs, decides what it
would do, logs those intended orders, and tracks open paper positions — so you
can watch it behave for days/weeks before risking a cent.

How it works (same rules as the backtester, applied to TODAY's data):
  1) Load pairs.json + download the latest prices.
  2) For each pair in "pairs" (the ones you actually trade), look at the
     current z-score.
  3) If flat and |z| >= entry_z  -> OPEN a position.
     If in a position and z reverts (<= exit_z), blows the stop (>= stop_z),
     or has been held too long (max_days) -> CLOSE it.
  4) PAPER mode  : write the intended orders to data/paper_orders.csv and update
                   data/positions.json. Nothing is sent anywhere.
     LIVE mode   : hand each order to execute_live() — a stub you must complete
                   with your Robinhood Agentic Trading MCP connection.

Run:
    python live.py            # paper mode (safe, default)
    python live.py --live     # attempts real orders (requires MCP wiring + funded account)
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys

import pairbot
import allocation

POSITIONS_FILE = os.path.join(pairbot.DATA_DIR, "positions.json")
ORDERS_LOG = os.path.join(pairbot.DATA_DIR, "paper_orders.csv")


# ---------------------------------------------------------------------------
# 1) POSITION STATE  (what the bot currently believes it holds)
# ---------------------------------------------------------------------------
def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    return {}


def save_positions(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


# ---------------------------------------------------------------------------
# 2) ORDER CONSTRUCTION  (turn a decision into concrete leg orders)
# ---------------------------------------------------------------------------
def entry_legs(mode, direction, a, b, capital):
    """The buy/short orders that OPEN a position. direction +1 = A is cheap."""
    if mode == "long_only":
        ticker = a if direction == +1 else b
        return [{"ticker": ticker, "side": "BUY", "dollars": capital}]
    if direction == +1:                      # long A / short B
        return [{"ticker": a, "side": "BUY", "dollars": capital},
                {"ticker": b, "side": "SHORT", "dollars": capital}]
    return [{"ticker": b, "side": "BUY", "dollars": capital},   # long B / short A
            {"ticker": a, "side": "SHORT", "dollars": capital}]


def closing_legs(open_legs):
    """Reverse each leg to CLOSE the position."""
    flip = {"BUY": "SELL", "SHORT": "COVER"}
    return [{**leg, "side": flip[leg["side"]]} for leg in open_legs]


# ---------------------------------------------------------------------------
# 3) DECISION  (same logic as the backtester, on the latest bar)
# ---------------------------------------------------------------------------
def decide(pair, z, position, today):
    """
    Return an order dict (or None) for one pair given today's z-score and the
    position we currently hold for it. Mirrors backtest_pair's entry/exit rules.
    """
    name = pair["name"]
    a, b = pair["a"], pair["b"]
    mode = pair.get("mode", "long_short")
    entry_z, exit_z = pair["entry_z"], pair["exit_z"]
    stop_z, max_days = pair.get("stop_z"), pair.get("max_days")
    capital = pair["capital_per_leg"]

    # --- long_only pairs = BASE + z-ladder ROTATION (this IS the pair trade) ---
    # Hold a base ($capital_per_leg) in the cheap leg; ADD fixed $step_dollars
    # steps as the spread diverges further past entry_z (up to n_steps), TRIM
    # them back as it converges, and ROTATE the whole book into the other leg at
    # the opposite extreme. cooldown_days gates add/trim. FIXED dollar steps (not
    # scaled to the account). n_steps:0 collapses to a plain flat rotation.
    if mode == "long_only":
        step_dollars = float(pair.get("step_dollars", 0))
        n_steps = int(pair.get("n_steps", 0))
        step_offsets = pair.get("step_offsets", [])
        cooldown = int(pair.get("cooldown_days", 0))

        if position is None:                     # flat -> enter the cheap leg's base
            if z <= -entry_z:
                direction = +1
            elif z >= entry_z:
                direction = -1
            else:
                return None
            tkr = a if direction == +1 else b
            return {"pair": name, "action": "OPEN", "direction": direction,
                    "z": round(z, 2), "steps": 0, "reason": "enter cheap-leg base",
                    "legs": [{"ticker": tkr, "side": "BUY", "dollars": capital}]}

        held = position["direction"]
        steps = int(position.get("steps", 0))
        held_tkr = a if held == +1 else b
        depth = (-z) if held == +1 else z        # how cheap our held leg still is

        if depth <= -entry_z:                    # opposite extreme -> ROTATE book
            new_tkr = b if held == +1 else a
            # BUY the new base FIRST, then SELL the whole old book, so a failed
            # buy leaves us still holding the old leg (never a naked half-close).
            return {"pair": name, "action": "SWAP", "direction": -held,
                    "z": round(z, 2), "steps": 0, "reason": "spread flipped -> rotate legs",
                    "legs": [{"ticker": new_tkr, "side": "BUY", "dollars": capital},
                             {"ticker": held_tkr, "side": "SELL", "sell_full": True}]}

        # cooldown-gate the ladder so one multi-day move isn't chased every day
        last = position.get("last_action_date")
        days_since = (today - dt.date.fromisoformat(last)).days if last else 10 ** 9
        if days_since < cooldown:
            return None
        target = pairbot.ladder_target_steps(depth, entry_z, step_offsets, n_steps)
        if target is None or target == steps:
            return None
        if target > steps:                       # ADD steps of the held leg
            dollars = (target - steps) * step_dollars
            return {"pair": name, "action": "ADD", "direction": held,
                    "z": round(z, 2), "steps": target,
                    "reason": f"diverging z={z:+.2f}: {steps}->{target}/{n_steps} steps",
                    "legs": [{"ticker": held_tkr, "side": "BUY", "dollars": dollars}]}
        # TRIM steps back toward the base (sell a fraction of the trade tranche)
        base_frac = capital + steps * step_dollars
        trim_frac = round((steps - target) * step_dollars / base_frac, 6)
        return {"pair": name, "action": "TRIM", "direction": held,
                "z": round(z, 2), "steps": target,
                "reason": f"converging z={z:+.2f}: {steps}->{target}/{n_steps} steps",
                "legs": [{"ticker": held_tkr, "side": "SELL", "trim_fraction": trim_frac}]}

    if position is None:                         # flat -> maybe OPEN
        if z >= entry_z:
            direction = -1
        elif z <= -entry_z:
            direction = +1
        else:
            return None
        legs = entry_legs(mode, direction, a, b, capital)
        return {"pair": name, "action": "OPEN", "direction": direction,
                "z": round(z, 2), "reason": "entry signal", "legs": legs}

    # in a position -> maybe CLOSE
    held = (today - dt.date.fromisoformat(position["entry_date"])).days
    hit_target = abs(z) <= exit_z
    hit_stop = stop_z is not None and abs(z) >= stop_z
    hit_time = max_days is not None and held >= max_days
    if not (hit_target or hit_stop or hit_time):
        return None
    reason = "reverted" if hit_target else ("stop-loss" if hit_stop else "time-stop")
    return {"pair": name, "action": "CLOSE", "direction": position["direction"],
            "z": round(z, 2), "reason": reason,
            "legs": closing_legs(position["legs"])}


# ---------------------------------------------------------------------------
# 4) EXECUTION
# ---------------------------------------------------------------------------
def log_paper_order(order, today):
    """Append an intended order to the paper-trading CSV (no real money)."""
    new_file = not os.path.exists(ORDERS_LOG)
    with open(ORDERS_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "pair", "action", "reason", "z", "ticker", "side", "dollars"])
        for leg in order["legs"]:
            w.writerow([today.isoformat(), order["pair"], order["action"],
                        order["reason"], order["z"],
                        leg["ticker"], leg["side"], leg["dollars"]])


def execute_live(order, mcp_url):
    """
    ===== STUB: wire up Robinhood Agentic Trading here =====
    Robinhood exposes an MCP server; an agent places orders through it. To make
    this real you need to, ONCE:
      1. In the Robinhood app, open a dedicated "Agentic Account" and fund it
         with ONLY money you can afford to lose.
      2. Connect an agent and copy the MCP server URL it gives you.
      3. Set it as an environment variable:  ROBINHOOD_MCP_URL=...
      4. Implement the call below using that MCP server's order tool
         (equities only in the beta: BUY/SELL; SHORT/COVER may be unavailable,
         which is why long_only is the safer default mode).

    Until that's done, this raises on purpose so no real order can slip out.
    """
    raise NotImplementedError(
        "Live execution is not wired up yet. Provide ROBINHOOD_MCP_URL and "
        "implement execute_live() against the Robinhood MCP order tool. "
        f"(Would have sent: {order['action']} {order['pair']} -> {order['legs']})")


# ---------------------------------------------------------------------------
# 5) MAIN
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Pair-trading live brain (paper by default).")
    ap.add_argument("--live", action="store_true",
                    help="Attempt REAL orders via Robinhood MCP (requires wiring + funded account).")
    args = ap.parse_args()

    mode_label = "LIVE (real orders)" if args.live else "PAPER (simulated, safe)"
    print(f"=== Pair-trading brain - {mode_label} ===")

    cfg = pairbot.load_config()
    traded = cfg["pairs"]
    if not traded:
        print("No traded pairs in pairs.json ('pairs' list is empty). Nothing to do.")
        return

    tickers = {t for p in traded for t in (p["a"], p["b"])}
    print(f"Downloading latest prices for {len(tickers)} tickers ...")
    close = pairbot.fetch_prices(tickers, period="1y")

    positions = load_positions()
    today = dt.date.today()
    mcp_url = os.environ.get("ROBINHOOD_MCP_URL")

    # 40%-of-bankroll sleeve for pairs (the other 60% is the SPY accumulator).
    al = allocation.load_allocation()
    pairs_budget = al["pairs_budget"]
    deployed = sum(leg["dollars"] for p in positions.values()
                   for leg in p["legs"] if leg["side"] == "BUY")
    print(allocation.summary_line(al))
    print(f"Pairs sleeve: ${deployed:,.0f} deployed of ${pairs_budget:,.0f}  "
          f"-> ${pairs_budget - deployed:,.0f} left\n")

    # Cap on how many positions can be open at once (protects a small account
    # from a morning where every pair signals at the same time). open_slots is
    # how many NEW positions we're still allowed to open today.
    max_positions = cfg.get("max_positions")
    open_slots = (max_positions - len(positions)) if max_positions is not None else None

    orders = []
    for pair in traded:
        try:
            sp = pairbot.compute_spread(close, pair)
            z = float(sp["z"].dropna().iloc[-1])
        except (KeyError, IndexError):
            print(f"  {pair['name']:12s}  no data — skipped")
            continue
        order = decide(pair, z, positions.get(pair["name"]), today)
        held = "  (holding)" if pair["name"] in positions else ""
        order_cost = (sum(l["dollars"] for l in order["legs"] if l["side"] == "BUY")
                      if order and order["action"] == "OPEN" else 0)
        if order is None:
            print(f"  {pair['name']:12s}  z={z:+5.2f}  -> no action{held}")
        elif order["action"] == "OPEN" and open_slots is not None and open_slots <= 0:
            print(f"  {pair['name']:12s}  z={z:+5.2f}  -> OPEN signal SKIPPED "
                  f"(at max {max_positions} positions)")
        elif order["action"] == "OPEN" and deployed + order_cost > pairs_budget:
            print(f"  {pair['name']:12s}  z={z:+5.2f}  -> OPEN signal SKIPPED "
                  f"(would exceed ${pairs_budget:,.0f} pairs budget)")
        else:
            print(f"  {pair['name']:12s}  z={z:+5.2f}  -> {order['action']} ({order['reason']})")
            orders.append(order)
            if order["action"] == "OPEN":
                if open_slots is not None:
                    open_slots -= 1
                deployed += order_cost
            elif order["action"] == "CLOSE":
                deployed -= sum(l["dollars"] for l in order["legs"] if l["side"] == "SELL")

    if not orders:
        print("\nNo orders to place today.")
        return

    print(f"\n{len(orders)} order(s) to place:")
    for o in orders:
        legs = ", ".join(f"{l['side']} ${l['dollars']:.0f} {l['ticker']}" for l in o["legs"])
        print(f"  {o['action']:5s} {o['pair']:12s} [{o['reason']}]: {legs}")

    # ---- carry out the orders ----
    if args.live:
        if not mcp_url:
            print("\nRefusing to go live: ROBINHOOD_MCP_URL is not set. Aborting.", file=sys.stderr)
            sys.exit(1)
        for o in orders:
            execute_live(o, mcp_url)             # will raise until you implement it
    else:
        for o in orders:
            log_paper_order(o, today)
            name = o["pair"]
            act = o["action"]
            if act == "OPEN":
                positions[name] = {"direction": o["direction"], "entry_date": today.isoformat(),
                                   "entry_z": o["z"], "steps": o.get("steps", 0),
                                   "last_action_date": today.isoformat(), "legs": o["legs"]}
            elif act == "SWAP":
                positions[name] = {"direction": o["direction"], "entry_date": today.isoformat(),
                                   "entry_z": o["z"], "steps": 0,
                                   "last_action_date": today.isoformat()}
            elif act in ("ADD", "TRIM"):
                p = positions.get(name, {})
                p["steps"] = o.get("steps", p.get("steps", 0))
                p["last_action_date"] = today.isoformat()
                positions[name] = p
            else:  # CLOSE (long_short)
                positions.pop(name, None)
        save_positions(positions)
        print(f"\nPaper orders logged to: {ORDERS_LOG}")
        print(f"Open paper positions ({len(positions)}): "
              f"{', '.join(positions) if positions else 'none'}")


if __name__ == "__main__":
    main()
