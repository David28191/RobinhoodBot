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
import spy_wtd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = pairbot.DATA_DIR
PRICES_FILE = os.path.join(DATA, "mcp_prices.json")
STATE_FILE = os.path.join(DATA, "live_state.json")
OUT_FILE = os.path.join(DATA, "intended_orders.json")
JOURNAL_FILE = os.path.join(DATA, "bot_journal.jsonl")   # append-only run history
SWING_CFG_FILE = os.path.join(HERE, "swing.json")


def append_journal(result):
    """Append ONE line per run to bot_journal.jsonl (never overwritten), so there
    is a durable record of what the bot decided/ordered each run. `intended_orders`
    are the orders handed to the executor; the routine should also append the
    CONFIRMED fills (with broker order ids) after placing them -- see SKILL.md.
    Returns the entry written."""
    entry = {
        "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": result.get("as_of"),
        "bankroll": result.get("bankroll"),
        "cash_before": result.get("cash_before"),
        "cash_after_est": result.get("cash_after_est"),
        "notes": result.get("notes", []),
        "orders": [{"source": o.get("source"), "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "amount": o.get("dollar_amount") or o.get("quantity"),
                    "reason": o.get("reason"), "ref_id": o.get("ref_id")}
                   for o in result.get("intended_orders", [])],
        "dropped": len(result.get("dropped_orders", [])),
    }
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


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
    df = pd.DataFrame(cols)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df.dropna(how="all").ffill()


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------
def _pair_deployed(pair, pos):
    """$ currently in a pair's book = base + steps * step_dollars."""
    return (float(pair["capital_per_leg"])
            + int(pos.get("steps", 0)) * float(pair.get("step_dollars", 0)))


def decide_pairs(cfg, close, state, today, al):
    """Mirror live.main()'s pair loop for the base+ladder rotation. OPEN and ADD
    consume the pairs budget (gated); TRIM/SWAP free or recycle it. FIXED dollar
    steps -- the growing budget just lets more pairs ladder, it does NOT resize a
    step."""
    traded = cfg["pairs"]
    cfg_by_name = {p["name"]: p for p in traded}
    positions = state.get("pairs_positions", {})
    pairs_budget = al["pairs_budget"]
    deployed = sum(_pair_deployed(cfg_by_name[nm], pos)
                   for nm, pos in positions.items() if nm in cfg_by_name)
    max_positions = cfg.get("max_positions")
    open_slots = (max_positions - len(positions)) if max_positions is not None else None

    orders, notes = [], []
    for pair in traded:
        try:
            z = float(pairbot.compute_spread(close, pair)["z"].dropna().iloc[-1])
        except (KeyError, IndexError):
            notes.append(f"{pair['name']}: no data — skipped")
            continue
        prev = positions.get(pair["name"])
        order = live.decide(pair, z, prev, today)
        if order is None:
            notes.append(f"{pair['name']}: z={z:+.2f} no action")
            continue
        act = order["action"]
        buy_cost = sum(l["dollars"] for l in order["legs"]
                       if l["side"] == "BUY" and "dollars" in l)
        if act == "OPEN" and open_slots is not None and open_slots <= 0:
            notes.append(f"{pair['name']}: OPEN skipped (at max {max_positions})")
            continue
        if act in ("OPEN", "ADD") and deployed + buy_cost > pairs_budget:
            notes.append(f"{pair['name']}: {act} skipped (over ${pairs_budget:.0f} pairs budget)")
            continue
        notes.append(f"{pair['name']}: z={z:+.2f} -> {act} ({order['reason']})")
        orders.append(order)
        if act == "OPEN":
            if open_slots is not None:
                open_slots -= 1
            deployed += buy_cost
        elif act == "ADD":
            deployed += buy_cost
        elif act == "TRIM":
            freed = (int(prev.get("steps", 0)) - int(order["steps"])) * float(pair.get("step_dollars", 0))
            deployed -= max(0.0, freed)
        elif act == "SWAP":
            deployed += float(pair["capital_per_leg"]) - _pair_deployed(pair, prev or {})
    return orders, notes


def decide_spy(cfg_acc, close, state, al):
    """Mirror live_spy.main()'s single-strategy decision."""
    strat = cfg_acc["strategies"][0]
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


def decide_swing(cfg_swing, close, state, al):
    """Base+ladder swing sleeve on its own symbol (e.g. QQQ): always hold a base
    long, then ADD on weekly dips / TRIM on weekly rips AROUND it (never selling
    the base). Sized to the swing budget. Returns (orders, price, notes)."""
    sym = cfg_swing["symbol"]
    if sym not in close.columns:
        return [], None, [f"swing {sym}: no data — skipped"]
    px = close[sym].dropna()
    wf = spy_wtd.swing_frame(cfg_swing, px)
    price = float(px.iloc[-1])
    notes = []

    # Robinhood is the source of truth: reconcile the ledger against REAL shares.
    # Mutates `state` so the reconciliation is persisted via updated_state.json.
    real_q = float(state.get("shares", {}).get(sym) or 0.0)
    sw = state.get("swing", {}) or {}
    if sw.get("base_established") and real_q <= 0:
        # ledger thinks we hold a base but the account is empty -> stale; drop it
        # so the base gets re-established this run instead of wedging.
        notes.append(f"swing {sym}: ledger holds a base but account has no {sym} "
                     f"-- stale, reset (will re-establish the base)")
        state["swing"] = sw = {}
    elif not sw.get("base_established") and real_q > 0:
        # untracked {sym} already in the account -> adopt it AS the base (no
        # double-buy); the ladder then trades around it.
        sw = {"base_shares": round(real_q, spy_wtd.SHARE_DECIMALS), "trade_shares": 0.0,
              "trade_basis": 0.0, "steps": 0, "base_established": True,
              "last_action_date": None}
        state["swing"] = sw
        notes.append(f"swing {sym}: adopted existing {real_q:.6f} {sym} as the base "
                     f"(now trading around it)")

    budget = al.get("swing_budget", 0)
    orders = spy_wtd.swing_ladder_decide(cfg_swing, wf, sw, budget)
    zlast = float(wf["z"].iloc[-1]) if not np.isnan(wf["z"].iloc[-1]) else float("nan")
    if orders:
        notes.append(f"swing {sym} z={zlast:+.2f} -> {orders[0]['action']} ({orders[0]['reason']})")
    else:
        held = f"base+{int(sw.get('steps', 0))} step(s)" if sw.get("base_established") else "flat"
        notes.append(f"swing {sym} z={zlast:+.2f} ({held}) -> no action")
    return orders, price, notes


# ---------------------------------------------------------------------------
# Turn internal decisions into broker-ready intended orders (dollar/share sized)
# ---------------------------------------------------------------------------
def to_broker_orders(pair_orders, spy_orders, swing_orders, swing_symbol, state, account):
    shares = state.get("shares", {})
    out = []

    # --- pairs (long_only: legs are BUY $ / SELL $) ---
    for o in pair_orders:
        # A SWAP's two legs share a group id so cash_guard can keep them atomic
        # (never place the SELL if the paired BUY can't be funded).
        group = str(uuid.uuid4()) if o["action"] == "SWAP" else None
        for leg in o["legs"]:
            base = {"ref_id": str(uuid.uuid4()), "source": "pairs", "group": group,
                    "pair": o["pair"], "reason": f"{o['action']}:{o['reason']}",
                    "account_number": account, "symbol": leg["ticker"], "type": "market"}
            if leg["side"] == "BUY":
                out.append({**base, "side": "buy", "dollar_amount": f"{leg['dollars']:.2f}"})
            elif leg["side"] == "SELL":
                q = shares.get(leg["ticker"])
                if leg.get("trim_fraction") is not None:
                    # TRIM: sell a FRACTION of the real held shares (never the base)
                    sell = {**base, "side": "sell"}
                    if q:
                        sell["quantity"] = f"{float(q) * float(leg['trim_fraction']):.6f}"
                    else:
                        sell["note"] = "TRIM skipped: no real shares to size against"
                    out.append(sell)
                else:
                    # SWAP/close: sell the FULL real position of this ticker
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

    # --- swing sleeve (base + ladder on swing_symbol, e.g. QQQ) ---
    for o in swing_orders:
        base = {"ref_id": str(uuid.uuid4()), "source": "swing",
                "reason": o["reason"], "account_number": account,
                "symbol": swing_symbol, "type": "market"}
        if o["side"] == "BUY":                   # BASE_BUY / ADD -> dollar-sized buy
            out.append({**base, "side": "buy", "dollar_amount": f"{o['dollars']:.2f}"})
        elif o["side"] == "SELL":                # TRIM one step -> sell a SPECIFIC qty
            # NOT sell_full_position: the base is a protected floor and must never
            # be sold. Only the trimmed step quantity goes out.
            out.append({**base, "side": "sell", "quantity": f"{o['shares']:.6f}"})
    return out


def cash_guard(orders, cash):
    """Drop buys that don't fit remaining real cash (most-conservative). If a
    dropped buy belongs to a SWAP group, drop that group's SELL too, so a
    rotation can never half-execute (sell a leg without its paired buy). Relies
    on the BUY leg being listed before the SELL leg (buy-first ordering)."""
    kept, dropped, remaining = [], [], float(cash)
    dropped_groups = set()
    for o in orders:
        grp = o.get("group")
        if grp and grp in dropped_groups:
            dropped.append({**o, "dropped_reason": "swap aborted (paired buy unfunded)"})
            continue
        if o["side"] == "buy":
            amt = float(o.get("dollar_amount", 0))
            if amt > remaining + 1e-9:
                dropped.append({**o, "dropped_reason": f"insufficient cash (${remaining:.2f} left)"})
                if grp:
                    dropped_groups.add(grp)
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
    with open(SWING_CFG_FILE) as f:
        cfg_swing = json.load(f)

    # Bankroll = REAL account value when provided (so weekly deposits + gains grow
    # every sleeve AND the per-run cap automatically); else the static config.
    bankroll = float(state.get("account_value") or allocation.load_allocation()["total"])
    al = allocation.load_allocation(bankroll)
    max_run_spend = min(round(0.25 * bankroll, 2), 150.0)   # 25% of account, hard ceiling $150

    # Reconcile pairs ledger against REAL shares (Robinhood = source of truth):
    # if we think we hold a leg but the account has ~none of that ticker, drop
    # the stale position so this run re-enters cleanly instead of wedging.
    _shares = state.get("shares", {})
    _cfgmap = {p["name"]: p for p in cfg_pairs["pairs"]}
    recon_notes = []
    for _nm, _p in list(state.get("pairs_positions", {}).items()):
        _cf = _cfgmap.get(_nm)
        if not _cf:
            continue
        _held = _cf["a"] if _p.get("direction") == +1 else _cf["b"]
        if float(_shares.get(_held) or 0.0) <= 1e-6:
            state["pairs_positions"].pop(_nm, None)
            recon_notes.append(f"{_nm}: ledger held {_held} but account has none -> reset to flat")

    pair_orders, pair_notes = decide_pairs(cfg_pairs, close, state, today, al)
    pair_notes = recon_notes + pair_notes
    spy_orders, spy_price, spy_week, spy_notes = decide_spy(cfg_acc, close, state, al)
    swing_orders, swing_price, swing_notes = decide_swing(cfg_swing, close, state, al)

    broker = to_broker_orders(pair_orders, spy_orders, swing_orders, cfg_swing["symbol"], state, account)
    broker, dropped, cash_left = cash_guard(broker, min(float(state.get("cash", 0)), max_run_spend))
    # Pairs whose order was (partially) dropped for cash must NOT update the
    # optimistic ledger below -- otherwise a swap that never placed would record
    # us into the new leg and poison next run (the classic stale-state bug).
    dropped_pairs = {o.get("pair") for o in dropped if o.get("source") == "pairs"}

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
        if o["pair"] in dropped_pairs:
            continue                              # order didn't place -> don't move the ledger
        act = o["action"]
        if act in ("OPEN", "SWAP"):
            pos[o["pair"]] = {"direction": o["direction"], "entry_date": today.isoformat(),
                              "entry_z": o["z"], "steps": int(o.get("steps", 0)),
                              "last_action_date": today.isoformat()}
        elif act in ("ADD", "TRIM"):
            p = pos.get(o["pair"], {})
            p["steps"] = int(o.get("steps", p.get("steps", 0)))
            p["last_action_date"] = today.isoformat()
            pos[o["pair"]] = p
        elif act == "CLOSE":
            pos.pop(o["pair"], None)
    sw = new_state.setdefault("swing", {})
    for o in swing_orders:
        spy_wtd.apply_swing_ladder(o, sw, swing_price)
    with open(os.path.join(DATA, "updated_state.json"), "w") as f:
        json.dump(new_state, f, indent=2)

    result = {
        "as_of": str(close.index[-1].date()),
        "account_number": account,
        "bankroll": round(bankroll, 2),
        "max_run_spend": max_run_spend,
        "cash_before": state.get("cash"),
        "cash_after_est": round(cash_left, 2),
        "notes": spy_notes + pair_notes + swing_notes,
        "intended_orders": broker,
        "dropped_orders": dropped,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    append_journal(result)                       # durable, append-only run history

    print(json.dumps(result, indent=2))
    print(f"\n[{len(broker)} intended order(s); {len(dropped)} dropped] -> {OUT_FILE}")
    print(f"[journal] appended run to {JOURNAL_FILE}")


if __name__ == "__main__":
    main()
