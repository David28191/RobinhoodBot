"""
optimize.py  --  Robustness + parameter analysis.

Part A: runs the strategy across DIFFERENT market regimes (a crash, a bear
        market, a recovery) to see if it holds up when the market isn't calm.
Part B: sweeps entry/exit z-score levels to see how win rate & P&L change.

Uses the long-history equity pairs only (crypto ETFs didn't exist in 2020/2022).
Run:  python optimize.py
"""

import pandas as pd
import yfinance as yf
import pairbot

# pairs with enough history to test old periods
EQUITY_PAIRS = [("C", "GS"), ("XOM", "CVX"), ("JPM", "WFC"), ("V", "MA")]

PERIODS = {
    "COVID crash + rebound (2020)": ("2020-01-01", "2020-12-31"),
    "2022 bear market":             ("2022-01-01", "2022-12-31"),
    "2023 recovery":                ("2023-01-01", "2023-12-31"),
    "Last ~2 years":                ("2024-06-01", "2026-06-16"),
}


def fetch_range(tickers, start, end):
    data = yf.download(sorted(set(tickers)), start=start, end=end,
                       progress=False, auto_adjust=True)
    close = data["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame()
    return close.dropna(how="all").ffill()


def run_pairs(close, defaults, entry=None, exit=None, period=None):
    """Backtest all equity pairs; optionally override z-levels and filter by date."""
    net = n = wins = 0
    for a, b in EQUITY_PAIRS:
        pair = dict(defaults)
        pair.update(a=a, b=b, name=f"{a}/{b}")
        if entry is not None:
            pair["entry_z"] = entry
        if exit is not None:
            pair["exit_z"] = exit
        if a not in close.columns or b not in close.columns:
            continue
        trades, _, _ = pairbot.backtest_pair(close, pair)
        for t in trades:
            if t["status"] != "CLOSED":
                continue
            if period and not (period[0] <= t["entry_date"] <= period[1]):
                continue
            net += t["pnl"]; n += 1; wins += t["pnl"] > 0
    wr = (wins / n * 100) if n else 0
    return net, n, wr


def main():
    defaults = pairbot.load_config().get("defaults", {})
    defaults.setdefault("mode", "long_only")
    tickers = [t for pair in EQUITY_PAIRS for t in pair]

    # ---------------- Part A: market regimes ----------------
    print("=" * 64)
    print("PART A  --  same strategy across different market regimes")
    print("=" * 64)
    print(f"{'period':32s} {'net$':>8s} {'trades':>7s} {'win%':>6s}")
    for name, (s, e) in PERIODS.items():
        lead_in = (pd.Timestamp(s) - pd.Timedelta(days=300)).date().isoformat()
        close = fetch_range(tickers, lead_in, e)
        net, n, wr = run_pairs(close, defaults, period=(s, e))
        print(f"{name:32s} {net:8.0f} {n:7d} {wr:5.0f}%")

    # ---------------- Part B: OUT-OF-SAMPLE z-score sweep ----------------
    # The honest way: choose the best z-levels on a TRAIN window, then judge them
    # on a TEST window the optimizer never saw. If test P&L collapses vs train,
    # the "best" params were just curve-fit to noise.
    TRAIN = ("2023-06-01", "2024-12-31")
    TEST = ("2025-01-01", "2026-06-16")
    print("\n" + "=" * 64)
    print("PART B  --  OUT-OF-SAMPLE validation (no curve-fitting)")
    print("=" * 64)
    print(f"Train: {TRAIN[0]} -> {TRAIN[1]}   |   Test: {TEST[0]} -> {TEST[1]}")

    lead_in = (pd.Timestamp(TRAIN[0]) - pd.Timedelta(days=300)).date().isoformat()
    close = fetch_range(tickers, lead_in, TEST[1])

    print(f"\nSweep on TRAIN only:")
    print(f"{'entry_z':>8s} {'exit_z':>7s} {'trades':>7s} {'win%':>6s} {'net$':>8s}")
    results = []
    for entry in [1.5, 2.0, 2.5, 3.0]:
        for exit in [0.25, 0.5, 1.0]:
            net, n, wr = run_pairs(close, defaults, entry=entry, exit=exit, period=TRAIN)
            results.append((entry, exit, n, wr, net))
            print(f"{entry:8.1f} {exit:7.2f} {n:7d} {wr:5.0f}% {net:8.0f}")

    # pick the params that looked best in TRAIN, then see how they hold up in TEST
    best = max(results, key=lambda r: r[4])
    be, bx = best[0], best[1]
    tr_net, tr_n, tr_wr = run_pairs(close, defaults, entry=be, exit=bx, period=TRAIN)
    te_net, te_n, te_wr = run_pairs(close, defaults, entry=be, exit=bx, period=TEST)

    print(f"\nBest-on-train params: entry={be}  exit={bx}")
    print(f"   TRAIN (in-sample)     : net ${tr_net:7.0f} | {tr_n:2d} trades | {tr_wr:3.0f}% win")
    print(f"   TEST  (out-of-sample) : net ${te_net:7.0f} | {te_n:2d} trades | {te_wr:3.0f}% win")
    verdict = ("holds up out-of-sample" if te_net > 0
               else "FAILS out-of-sample — likely curve-fit")
    print(f"   Verdict: {verdict}.")


if __name__ == "__main__":
    main()
