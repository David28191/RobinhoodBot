"""
optimize_spy.py  --  Tune the SPY weekly-to-date strategy WITHOUT fooling ourselves.
====================================================================================
The honest method (same as optimize.py for the pairs):

  1. Backtest on the FULL history once per parameter set, so the weekly-σ warmup
     is always intact.
  2. Split the resulting trades by ENTRY DATE into a TRAIN window and a later
     TEST window the optimizer never gets to look at.
  3. Sweep parameters and pick the best set on TRAIN only.
  4. Re-judge that winner on TEST. If TEST P&L collapses vs TRAIN, the "best"
     params were just curve-fit to noise.

We tune the realistically-tradable case first: direction = fade, mode = long_only
(your Agentic cash account can't short). A momentum/long-short baseline is printed
too, to reconfirm those lose.

Run:  python optimize_spy.py
"""

import pandas as pd
import yfinance as yf

import spy_wtd

SYMBOL = "SPY"
VOL_LOOKBACK = 52

# Train on older history (incl. COVID + the 2022 bear); test on unseen recent years.
TRAIN = ("2016-01-01", "2022-12-31")
TEST = ("2023-01-01", "2026-06-19")

# parameter grid for the headline sweep (fade, long-only)
ENTRY_GRID = [1.0, 1.5, 2.0, 2.5]
EXIT_GRID = [0.25, 0.5, 1.0]
MAXDAYS_GRID = [10, 20, 40]
MIN_TRAIN_TRADES = 8          # ignore combos too sparse to mean anything


def fetch(start, end):
    data = yf.download(SYMBOL, start=start, end=end, progress=False, auto_adjust=True)
    close = data["Close"]
    if isinstance(close, pd.DataFrame):
        close = close[SYMBOL] if SYMBOL in close.columns else close.iloc[:, 0]
    return close.dropna()


def make_sc(base, **over):
    sc = dict(base)
    sc.update(over)
    sc.setdefault("mode", "long_only")
    sc.setdefault("stop_z", 3.5)
    sc.setdefault("capital", 1000)
    sc.setdefault("cost_bps", 2)
    sc["label"] = (f'{sc["direction"]}·{sc["entry_z"]}/{sc["exit_z"]}'
                   f'·{sc.get("max_days")}d·{sc["mode"]}')
    return sc


def run(wf, sc, window):
    """Backtest once, keep only CLOSED trades whose ENTRY falls inside `window`."""
    trades, _ = spy_wtd.backtest(wf, sc)
    net = n = wins = 0
    for t in trades:
        if t["status"] != "CLOSED":
            continue
        if not (window[0] <= t["entry_date"] <= window[1]):
            continue
        net += t["pnl"]; n += 1; wins += t["pnl"] > 0
    wr = (wins / n * 100) if n else 0.0
    avg = (net / n) if n else 0.0
    return net, n, wr, avg


def main():
    print(f"Downloading {SYMBOL} (full history for warmup) ...")
    # lead-in well before TRAIN so the 52-week σ is warm by 2016
    px = fetch("2013-01-01", TEST[1])
    wf = spy_wtd.weekly_frame(px, VOL_LOOKBACK)
    print(f"Got {len(px)} trading days "
          f"({px.index[0].date()} -> {px.index[-1].date()}).\n")

    base = {"direction": "fade"}

    # ---------------- Baseline: direction x mode over full period ----------------
    print("=" * 70)
    print("BASELINE  --  direction x mode (entry 2.0, exit 0.5, full history)")
    print("=" * 70)
    print(f"{'direction':>9s} {'mode':>11s} {'trades':>7s} {'win%':>6s} {'net$':>9s} {'avg$':>7s}")
    full = (px.index[0].date().isoformat(), px.index[-1].date().isoformat())
    for direction in ("fade", "momentum"):
        for mode in ("long_only", "long_short"):
            sc = make_sc(base, direction=direction, mode=mode,
                         entry_z=2.0, exit_z=0.5, max_days=20)
            net, n, wr, avg = run(wf, sc, full)
            print(f"{direction:>9s} {mode:>11s} {n:7d} {wr:5.0f}% {net:9.0f} {avg:7.1f}")

    # ---------------- OOS sweep: fade, long-only ----------------
    print("\n" + "=" * 70)
    print("OUT-OF-SAMPLE TUNING  --  fade, long-only (the tradable case)")
    print("=" * 70)
    print(f"Train: {TRAIN[0]} -> {TRAIN[1]}   |   Test: {TEST[0]} -> {TEST[1]}")
    print(f"\nSweep on TRAIN only (need >= {MIN_TRAIN_TRADES} trades to qualify):")
    print(f"{'entry':>6s} {'exit':>5s} {'maxd':>5s} {'trades':>7s} {'win%':>6s} {'net$':>8s} {'avg$':>7s}")

    results = []
    for entry in ENTRY_GRID:
        for exit in EXIT_GRID:
            for md in MAXDAYS_GRID:
                sc = make_sc(base, direction="fade", mode="long_only",
                             entry_z=entry, exit_z=exit, max_days=md)
                net, n, wr, avg = run(wf, sc, TRAIN)
                results.append((entry, exit, md, n, wr, net, avg))
                flag = "" if n >= MIN_TRAIN_TRADES else "  (sparse)"
                print(f"{entry:6.1f} {exit:5.2f} {md:5d} {n:7d} {wr:5.0f}% {net:8.0f} {avg:7.1f}{flag}")

    # pick best on TRAIN by net P&L, among combos with enough trades
    qualified = [r for r in results if r[3] >= MIN_TRAIN_TRADES]
    pool = qualified or results
    best = max(pool, key=lambda r: r[5])
    be, bx, bmd = best[0], best[1], best[2]

    sc_best = make_sc(base, direction="fade", mode="long_only",
                      entry_z=be, exit_z=bx, max_days=bmd)
    tr = run(wf, sc_best, TRAIN)
    te = run(wf, sc_best, TEST)

    print(f"\nBest-on-train: entry={be}  exit={bx}  max_days={bmd}")
    print(f"   TRAIN (in-sample)     : net ${tr[0]:7.0f} | {tr[1]:2d} trades | "
          f"{tr[2]:3.0f}% win | avg ${tr[3]:.1f}")
    print(f"   TEST  (out-of-sample) : net ${te[0]:7.0f} | {te[1]:2d} trades | "
          f"{te[2]:3.0f}% win | avg ${te[3]:.1f}")

    if te[1] == 0:
        verdict = "INCONCLUSIVE — no trades in the test window"
    elif te[0] > 0 and te[3] > 0:
        verdict = "HOLDS UP out-of-sample (positive, profitable per trade)"
    elif te[0] > 0:
        verdict = "marginal — positive total but thin per trade"
    else:
        verdict = "FAILS out-of-sample — the train 'edge' did not carry over"
    print(f"   Verdict: {verdict}.")

    print("\nTo adopt these, set spy.json defaults to:")
    print(f'   "direction": "fade", "mode": "long_only",')
    print(f'   "entry_z": {be}, "exit_z": {bx}, "stop_z": 3.5, "max_days": {bmd}')
    print("\nReminder: a backtest edge this small is easily erased by real-world")
    print("slippage and a different future. Treat as research, not a green light.")


if __name__ == "__main__":
    main()
