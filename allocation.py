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


def load_allocation():
    """Return the bankroll split as dollars: {total, spy_pct, pairs_pct,
    spy_budget, pairs_budget}."""
    with open(os.path.join(HERE, "spy_accumulate.json")) as f:
        block = json.load(f).get("_bot_allocation", {})

    total = float(block.get("total_capital", 1000))
    spy_pct = float(block.get("spy_accumulate_pct", 60))
    pairs_pct = float(block.get("pairs_pct", 40))
    denom = spy_pct + pairs_pct
    if denom <= 0:                                   # guard against a 0/0 config
        spy_pct, pairs_pct, denom = 60.0, 40.0, 100.0

    return {
        "total": total,
        "spy_pct": spy_pct,
        "pairs_pct": pairs_pct,
        "spy_budget": round(total * spy_pct / denom, 2),
        "pairs_budget": round(total * pairs_pct / denom, 2),
    }


def summary_line(al=None):
    al = al or load_allocation()
    return (f"Bankroll ${al['total']:,.0f}  ->  "
            f"SPY accumulator {al['spy_pct']:.0f}% = ${al['spy_budget']:,.0f}   |   "
            f"Pairs {al['pairs_pct']:.0f}% = ${al['pairs_budget']:,.0f}")


if __name__ == "__main__":
    print(summary_line())
