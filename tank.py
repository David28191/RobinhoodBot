"""
tank.py — TANK ACCUMULATOR engine (pure decide + local dry-run).
Active 'buy the tank, sell the bounce' with a melt-ice-cube backstop. Long-only (cash account).
cloud_decide.py imports `decide` (data-agnostic); `main()` here is a LOCAL dry-run via yfinance.
Ledger (persisted in live_state.json under "tank"):
  {"positions": {SYM: {shares, entry_px, units, entry_date, last_action_date}}, "last_buy_date": iso|None}
"""
import os, json, datetime as dt
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SHARE_DP = 6

def load_config():
    with open(os.path.join(HERE, "tank.json")) as f:
        return json.load(f)

def _rsi(px, n):
    d = px.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    ag = up.ewm(alpha=1/n, adjust=False).mean(); al = dn.ewm(alpha=1/n, adjust=False).mean()
    return (100 - 100/(1 + ag/al.replace(0, np.nan))).fillna(50)

def _units(dd, r2, cfg):
    u = 0
    for thr, units in cfg["dd_ladder"]:
        if dd <= thr: u = max(u, int(units))
    if r2 < cfg["rsi_tank"]: u += 1
    return u

def _metrics(cfg, close, sym):
    if sym not in close.columns: return None
    px = close[sym].dropna()
    if len(px) < cfg["hi_lookback"] + 2: return None
    p = float(px.iloc[-1])
    hi = float(px.tail(cfg["hi_lookback"]).max())
    dd = p/hi - 1
    r2 = float(_rsi(px, cfg["rsi_len"]).iloc[-1])
    ma = float(px.tail(cfg["ma_exit"]).mean())
    return dict(price=p, dd=dd, rsi2=r2, ma=ma)

def decide(cfg, close, state, budget, today):
    """Pure. Returns (orders, notes, new_ledger). orders: internal dicts
    {source:'tank', side:'BUY'|'SELL', symbol, dollars|shares, reason}."""
    cost = cfg.get("cost_bps", 2) / 1e4
    unit = float(cfg["unit_dollars"])
    led = json.loads(json.dumps(state.get("tank", {"positions": {}, "last_buy_date": None})))
    positions = led.setdefault("positions", {})
    orders, notes = [], []

    # rank watch names by severity (deepest drawdown first)
    scored = []
    for sym in cfg["watch"]:
        m = _metrics(cfg, close, sym)
        if m: scored.append((sym, m))
    scored.sort(key=lambda kv: kv[1]["dd"])

    deployed = sum(int(p.get("units", 0)) * unit for p in positions.values())
    open_names = len([s for s, p in positions.items() if p.get("units", 0) > 0])
    tanked = False

    for sym, m in scored:
        held = positions.get(sym)
        held_u = int(held["units"]) if held else 0
        bounced = (m["rsi2"] > cfg["trim_rsi"]) or (m["price"] > m["ma"])
        # cooldown on an existing name
        ready = True
        if held and held.get("last_action_date"):
            ready = (today - dt.date.fromisoformat(held["last_action_date"])).days >= cfg["cooldown_days"]

        # SELL the bounce — trim the whole opportunistic tranche (core is separate, never here)
        if held and held_u > 0 and bounced and ready:
            orders.append({"source": "tank", "side": "SELL", "symbol": sym,
                           "shares": round(float(held["shares"]), SHARE_DP),
                           "reason": f"bounce trim (RSI2={m['rsi2']:.0f}, >{cfg['ma_exit']}DMA) after {m['dd']*100:+.1f}% tank"})
            deployed -= held_u * unit
            positions.pop(sym, None); open_names -= 1
            notes.append(f"{sym}: bounced -> TRIM"); continue

        # BUY the tank — ladder deeper as it falls
        target_u = _units(m["dd"], m["rsi2"], cfg)
        if target_u > held_u and ready and not bounced:
            if held_u == 0 and open_names >= cfg["max_names"]:
                notes.append(f"{sym}: tank {target_u}u but at max_names {cfg['max_names']} — skip"); continue
            add_u = target_u - held_u
            add_dollars = min(add_u * unit, max(0.0, budget - deployed))
            if add_dollars >= 1:
                sh = round((add_dollars - add_dollars*cost) / m["price"], SHARE_DP)
                prev_sh = float(held["shares"]) if held else 0.0
                positions[sym] = {"shares": round(prev_sh + sh, SHARE_DP), "entry_px": m["price"],
                                  "units": target_u, "entry_date": (held or {}).get("entry_date", today.isoformat()),
                                  "last_action_date": today.isoformat()}
                orders.append({"source": "tank", "side": "BUY", "symbol": sym,
                               "dollars": round(add_dollars, 2),
                               "reason": f"TANK {m['dd']*100:+.1f}% off {cfg['hi_lookback']}d-high, RSI2={m['rsi2']:.0f} -> {target_u}u"})
                deployed += add_dollars
                if held_u == 0: open_names += 1
                tanked = True; led["last_buy_date"] = today.isoformat()
                notes.append(f"{sym}: TANK -> BUY ${add_dollars:.0f} ({target_u}u)")
            else:
                notes.append(f"{sym}: tank {target_u}u but budget spent (${budget:.0f})")

    # MELT-ICE-CUBE backstop — nothing tanked; if it's been quiet, feed the core
    lb = led.get("last_buy_date")
    if not tanked:
        if lb is None:
            led["last_buy_date"] = today.isoformat()      # start the clock
            notes.append("backstop clock started (no history yet)")
        elif (today - dt.date.fromisoformat(lb)).days >= cfg["backstop_days"]:
            core = cfg["core"][0]
            bd = min(float(cfg["backstop_dollars"]), budget)
            if bd >= 1:
                orders.append({"source": "tank", "side": "BUY", "symbol": core,
                               "dollars": round(bd, 2), "reason": f"melt-ice-cube: {cfg['backstop_days']}d no tank -> feed core"})
                led["last_buy_date"] = today.isoformat()
                notes.append(f"BACKSTOP -> BUY {core} ${bd:.0f} (nothing tanked in {cfg['backstop_days']}d)")
    return orders, notes, led

# --------------------------------------------------------------------------- local dry-run
def main():
    from pairbot import fetch_prices
    cfg = load_config()
    syms = sorted(set(cfg["watch"] + cfg["core"]))
    print(f"[dry-run] fetching {len(syms)} symbols (yfinance, local only) ...")
    close = fetch_prices(syms, period="3mo")
    state = {"tank": {"positions": {}, "last_buy_date": None}}
    budget = 40.0
    orders, notes, led = decide(cfg, close, state, budget, dt.date.today())
    print(f"\nTANK dry-run · budget ${budget:.0f} · {dt.date.today()}\n")
    print("INTENDED ORDERS:")
    for o in orders:
        amt = f"${o['dollars']:.2f}" if o["side"] == "BUY" else f"{o['shares']:.4f} sh"
        print(f"  {o['side']:<4} {o['symbol']:<5} {amt:<12} — {o['reason']}")
    if not orders: print("  (none)")
    print("\nNOTES:")
    for n in notes: print(f"  · {n}")

if __name__ == "__main__":
    main()
