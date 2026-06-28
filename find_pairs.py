"""
find_pairs.py  --  Pair DISCOVERY screener.

Scans a universe of tickers (grouped by sector), and for every same-sector
combination measures:
  - correlation   : how tightly the two move together (want >= 0.7)
  - half-life      : how many days the spread takes to revert halfway to normal
                     (want a sensible number, ~5-40 days; tiny or huge = bad)
  - backtest       : runs YOUR actual strategy (from pairs.json defaults) on it
                     and reports net P&L + win rate.

Then it prints the best candidates, ranked. Edit UNIVERSE to change what's scanned.
Run:  python find_pairs.py
"""

import itertools
import numpy as np
import pandas as pd

import pairbot

# Tickers grouped by theme. Pairs are only formed WITHIN a group (that's where
# real economic links live). Add/remove freely.
UNIVERSE = {
    "staples":   ["KO", "PEP", "PG", "CL", "MDLZ"],
    "payments":  ["V", "MA", "AXP", "PYPL"],
    "energy":    ["XOM", "CVX", "COP", "SLB"],
    "retail":    ["HD", "LOW", "TGT", "WMT", "COST"],
    "pharma":    ["MRK", "PFE", "LLY", "ABBV", "BMY", "JNJ"],
    "bigtech":   ["AAPL", "MSFT", "GOOGL", "META"],
    "semis":     ["NVDA", "AMD", "INTC", "AVGO", "TSM"],
    "banks":     ["JPM", "BAC", "WFC", "C", "GS"],
    "crypto":    ["IBIT", "BSOL", "ETHA", "FBTC"],
}

MIN_CORR = 0.70          # ignore pairs below this correlation
MIN_DAYS = 200           # need at least this much shared history to trust it


def half_life(spread):
    """Days for the spread to revert halfway to its mean (Ornstein-Uhlenbeck est.)."""
    s = spread.dropna()
    lag = s.shift(1).dropna()
    s = s.loc[lag.index]
    delta = s - lag
    beta = np.polyfit(lag.values, delta.values, 1)[0]
    if beta >= 0:
        return np.inf                      # not mean-reverting
    return -np.log(2) / beta


# Standard ADF 5% critical value (constant, no trend, large sample). A spread
# whose ADF t-stat is BELOW this is statistically mean-reverting = cointegrated.
ADF_CRIT_5PCT = -2.86


def adf_tstat(series):
    """
    Augmented Dickey-Fuller t-statistic (regression with a constant), in pure
    numpy so we don't need statsmodels. MORE NEGATIVE = stronger evidence the
    spread is stationary / mean-reverting. Correlation tells you two stocks move
    together; this tells you their SPREAD actually comes back — the thing pair
    trading relies on. Returns np.nan if there isn't enough data.
    """
    y = np.asarray(series.dropna(), dtype=float)
    n = len(y)
    if n < 30:
        return np.nan
    dy = np.diff(y)                                   # Δy_t
    lag_level = y[:-1]                                # y_{t-1}
    L = len(dy)
    p = int(np.floor(12 * (n / 100.0) ** 0.25))       # Schwert rule for lag count
    p = max(0, min(p, L // 4))
    target = dy[p:]
    cols = [np.ones(L - p), lag_level[p:]]            # constant + level term
    for j in range(1, p + 1):                         # augmenting lagged diffs
        cols.append(dy[p - j: L - j])
    X = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(X, target, rcond=None)
    resid = target - X @ beta
    dof = len(target) - X.shape[1]
    if dof <= 0:
        return np.nan
    sigma2 = (resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return np.nan
    se_level = np.sqrt(cov[1, 1])
    if se_level == 0:
        return np.nan
    return float(beta[1] / se_level)                 # t-stat on y_{t-1}


def main():
    tickers = sorted({t for group in UNIVERSE.values() for t in group})
    print(f"Downloading {len(tickers)} tickers ...")
    close = pairbot.fetch_prices(tickers, period="2y")

    defaults = pairbot.load_config().get("defaults", {})
    rows = []

    for group, syms in UNIVERSE.items():
        for a, b in itertools.combinations(syms, 2):
            if a not in close.columns or b not in close.columns:
                continue
            joint = close[[a, b]].dropna()
            if len(joint) < MIN_DAYS:
                continue

            rets = joint.pct_change().dropna()
            corr = rets[a].corr(rets[b])
            if corr < MIN_CORR:
                continue

            ratio = joint[a] / joint[b]
            hl = half_life(ratio)
            adf = adf_tstat(ratio)
            coint = "yes" if (np.isfinite(adf) and adf < ADF_CRIT_5PCT) else "no"

            # run the real strategy on this candidate
            pair = dict(defaults)
            pair.update({"a": a, "b": b, "name": f"{a}/{b}"})
            trades, _, _ = pairbot.backtest_pair(close, pair)
            closed = [t for t in trades if t["status"] == "CLOSED"]
            net = sum(t["pnl"] for t in closed)
            n = len(closed)
            wr = (sum(t["pnl"] > 0 for t in closed) / n * 100) if n else 0

            rows.append({
                "group": group, "pair": f"{a}/{b}", "corr": round(corr, 2),
                "half_life": round(hl, 1) if np.isfinite(hl) else 999,
                "adf": round(adf, 2) if np.isfinite(adf) else 99,
                "coint": coint,
                "trades": n, "win%": round(wr), "net$": round(net),
            })

    if not rows:
        print("No pairs cleared the filters. Try lowering MIN_CORR.")
        return

    df = pd.DataFrame(rows)
    # quality rank: positive P&L, decent win rate, sane half-life, high corr, and
    # a bonus for how strongly the spread is cointegrated (ADF below -2.86).
    coint_bonus = (-df["adf"] - (-ADF_CRIT_5PCT)).clip(lower=0) * 3
    df["score"] = ((df["net$"] / 100) + (df["win%"] - 50) / 5
                   + (df["corr"] - 0.7) * 20 + coint_bonus)
    df = df.sort_values("score", ascending=False)

    pd.set_option("display.width", 140)
    print("\n=== Candidate pairs, best first ===")
    print(df.drop(columns="score").to_string(index=False))
    print("\nGuide: want coint = yes (ADF < -2.86), corr >= 0.8, half-life roughly "
          "5-40 days, positive net$, win% > 50.")
    n_coint = (df["coint"] == "yes").sum()
    print(f"{n_coint} of {len(df)} candidates are statistically cointegrated.")


if __name__ == "__main__":
    main()
