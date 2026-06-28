"""
regime_test.py  --  Can we optimize a long-only strategy for a bear market?

Two experiments:
  A) Sweep z-levels on the 2022 bear market -> is ANY setting profitable?
  B) Add a market REGIME FILTER (only open trades when SPY is above its
     200-day average) -> does sitting out downtrends help?
"""

import numpy as np
import pandas as pd
import yfinance as yf

EQUITY_PAIRS = [("C", "GS"), ("XOM", "CVX"), ("JPM", "WFC"), ("V", "MA")]
LOOKBACK, STOP, CAP = 120, 3.5, 1000


def fetch(tickers, start, end):
    data = yf.download(sorted(set(tickers)), start=start, end=end,
                       progress=False, auto_adjust=True)["Close"]
    if isinstance(data, pd.Series):
        data = data.to_frame()
    return data.dropna(how="all").ffill()


def backtest(close, a, b, entry, exit, regime=None):
    """Long-only pair backtest. `regime` (bool Series) gates NEW entries only."""
    ratio = close[a] / close[b]
    z = (ratio - ratio.rolling(LOOKBACK).mean()) / ratio.rolling(LOOKBACK).std()
    pa, pb = close[a], close[b]
    pos = 0
    ea = eb = edt = None
    trades = []
    for i in range(len(z)):
        d = z.index[i]
        zz, av, bv = z.iloc[i], pa.iloc[i], pb.iloc[i]
        if np.isnan(zz) or np.isnan(av) or np.isnan(bv):
            continue
        gate = True if regime is None else bool(regime.get(d, False))
        if pos == 0:
            if gate and zz <= -entry:
                pos, ea, eb, edt = 1, av, bv, d
            elif gate and zz >= entry:
                pos, ea, eb, edt = -1, av, bv, d
        elif abs(zz) <= exit or abs(zz) >= STOP:
            ret = (av / ea - 1) if pos == 1 else (bv / eb - 1)
            trades.append((edt, CAP * ret))
            pos = 0
    return trades


def summarize(all_trades, start, end):
    sel = [pnl for dt, pnl in all_trades if start <= dt.date().isoformat() <= end]
    if not sel:
        return 0.0, 0, 0.0
    net = sum(sel)
    wr = sum(p > 0 for p in sel) / len(sel) * 100
    return net, len(sel), wr


def run_all(close, entry, exit, regime=None):
    trades = []
    for a, b in EQUITY_PAIRS:
        trades += backtest(close, a, b, entry, exit, regime)
    return trades


def main():
    tickers = [t for p in EQUITY_PAIRS for t in p] + ["SPY"]
    close = fetch(tickers, "2019-06-01", "2026-06-16")
    spy = close["SPY"]
    regime = spy > spy.rolling(200).mean()      # True when market is healthy

    BEAR = ("2022-01-01", "2022-12-31")
    RECENT = ("2024-06-01", "2026-06-16")

    # ---- A) z sweep on the 2022 bear market ----
    print("=" * 60)
    print("A) z-score sweep DURING the 2022 bear market")
    print("=" * 60)
    print(f"{'entry':>6s} {'exit':>5s} {'trades':>7s} {'win%':>6s} {'net$':>8s}")
    best = None
    for entry in [1.5, 2.0, 2.5, 3.0]:
        for exit in [0.25, 0.5]:
            t = run_all(close, entry, exit)
            net, n, wr = summarize(t, *BEAR)
            print(f"{entry:6.1f} {exit:5.2f} {n:7d} {wr:5.0f}% {net:8.0f}")
            if best is None or net > best[0]:
                best = (net, entry, exit, wr, n)
    print(f"\nBEST possible in 2022: net ${best[0]:.0f} "
          f"(entry {best[1]}, exit {best[2]}, {best[3]:.0f}% win, {best[4]} trades)")

    # ---- B) regime filter, default 2.0/0.5 ----
    print("\n" + "=" * 60)
    print("B) Regime filter: only trade when SPY > its 200-day average")
    print("=" * 60)
    base = run_all(close, 2.0, 0.5, regime=None)
    filt = run_all(close, 2.0, 0.5, regime=regime)
    for label, period in [("2022 bear market", BEAR), ("Last ~2 years", RECENT)]:
        bn, bc, bw = summarize(base, *period)
        fn, fc, fw = summarize(filt, *period)
        print(f"\n{label}:")
        print(f"   no filter : net ${bn:7.0f} | {bc:2d} trades | {bw:3.0f}% win")
        print(f"   w/ filter : net ${fn:7.0f} | {fc:2d} trades | {fw:3.0f}% win")


if __name__ == "__main__":
    main()
