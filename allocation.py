"""
allocation.py  --  single source of truth for the bot's capital split
=====================================================================
The whole bot's money is split between the two strategies:
  * SPY accumulator (buy dips / trim tops / grow a core)  -- the bigger sleeve
  * Pair trading                                          -- the smaller sleeve

You set the split + total bankroll ONCE, in spy_accumulate.json under
"_bot_allocation" (total_capital, spy_accumulate_pct, pairs_pct). Both paper
brains (live_spy.py and live.py) read it from here so the 60/40 is enforced in
exactly one place. Change the numbers there, not in code.

'total_capital' is a paper/simulated bankroll for now -- edit it (or top up) as
you add real money.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_allocation(bankroll=None):
    """Return the bankroll split as dollars: {total, spy_pct, pairs_pct, swing_pct,
    spy_budget, pairs_budget, swing_budget}. Pass `bankroll` to size off the REAL
    account value (so weekly deposits + gains grow every sleeve automatically);
    omit it to use the static total_capital from the config."""
    with open(os.path.join(HERE, "spy_accumulate.json")) as f:
        block = json.load(f).get("_bot_allocation", {})

    total = float(bankroll) if bankroll is not None else float(block.get("total_capital", 1000))
    # SPY weekly-DCA accumulator retired 2026-08-15; its share became the TANK sleeve.
    # Back-compat: if an old config still has spy_accumulate_pct, fold it into tank.
    tank_pct = float(block.get("tank_pct", block.get("spy_accumulate_pct", 40)))
    pairs_pct = float(block.get("pairs_pct", 35))
    swing_pct = float(block.get("swing_pct", 25))
    denom = tank_pct + pairs_pct + swing_pct
    if denom <= 0:                                   # guard against a 0/0 config
        tank_pct, pairs_pct, swing_pct, denom = 40.0, 35.0, 25.0, 100.0

    return {
        "total": total,
        "tank_pct": tank_pct,
        "pairs_pct": pairs_pct,
        "swing_pct": swing_pct,
        "tank_budget": round(total * tank_pct / denom, 2),
        "pairs_budget": round(total * pairs_pct / denom, 2),
        "swing_budget": round(total * swing_pct / denom, 2),
    }


def summary_line(al=None):
    al = al or load_allocation()
    return (f"Bankroll ${al['total']:,.0f}  ->  "
            f"Tank {al['tank_pct']:.0f}% = ${al['tank_budget']:,.0f}   |   "
            f"Pairs {al['pairs_pct']:.0f}% = ${al['pairs_budget']:,.0f}   |   "
            f"Swing {al['swing_pct']:.0f}% = ${al['swing_budget']:,.0f}")


if __name__ == "__main__":
    print(summary_line())
