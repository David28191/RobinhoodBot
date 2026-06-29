"""
scout_pairs.py  --  weekly pair-DISCOVERY report (cloud-runnable, data-agnostic)
================================================================================
Scans a universe of same-sector tickers and ranks the best pair-trade candidates,
then writes a human-readable report (intended to be EMAILED weekly). It reuses the
discovery math from find_pairs.py (correlation, half-life, ADF cointegration test,
and a backtest of your actual strategy) and ADDS the CURRENT z-score so the report
tells you which pairs are stretched and ACTIONABLE right now -- not just which are
structurally good.

Data-agnostic like cloud_decide.py: it reads prices the cloud agent already pulled
from the Robinhood MCP, so it never touches yfinance.
  data/mcp_prices.json  {ticker:[{"date","close"},...], ...}  (universe tickers)

  python scout_pairs.py [--top 5]

Output: prints the report, writes data/pair_scout_report.txt + data/pair_scout.json.
RESEARCH ONLY -- proposes candidates, places nothing.
"""

import argparse
import itertools
import json
import os

import numpy as np
import pandas as pd

import pairbot
import find_pairs as F

DATA = pairbot.DATA_DIR
PRICES_FILE = os.path.join(DATA, "mcp_prices.json")
REPORT_TXT = os.path.join(DATA, "pair_scout_report.txt")
REPORT_JSON = os.path.join(DATA, "pair_scout.json")


def prices_df(prices_json):
    cols = {}
    for t, bars in prices_json.items():
        cols[t] = pd.Series({pd.Timestamp(b["date"]): float(b["close"]) for b in bars}).sort_index()
    return pd.DataFrame(cols).sort_index().dropna(how="all").ffill()


def sector_trends(close):
    """MACRO context per sector: average of (last price / 200-day MA - 1) across a
    group's members. Negative = the sector is BELOW its trend (sinking). Because the
    pairs are long-only (you go LONG the cheap leg), this flags 'you'd be buying into
    a sinking sector' -- shown in the report, NOT hard-blocked (you decide)."""
    out = {}
    for group, syms in F.UNIVERSE.items():
        ma_vals, mo3 = [], []
        for s in syms:
            if s in close.columns:
                ser = close[s].dropna()
                if len(ser) >= 200:
                    ma_vals.append(float(ser.iloc[-1] / ser.tail(200).mean() - 1))
                if len(ser) > 63:
                    mo3.append(float(ser.iloc[-1] / ser.iloc[-63] - 1))
        if mo3:
            r3 = float(np.mean(mo3))                              # recent 3-month momentum
            vs200 = int(round(np.mean(ma_vals) * 100)) if ma_vals else 0  # structural trend
            lab = "DOWN" if r3 < -0.05 else ("up" if r3 > 0.05 else "flat")
            out[group] = (lab, int(round(r3 * 100)), vs200)
        else:
            out[group] = ("?", 0, 0)
    return out


def scout(close):
    cfg = pairbot.load_config()
    traded = {p["name"] for p in cfg.get("pairs", [])}
    defaults = cfg.get("defaults", {})
    entry_z = float(defaults.get("entry_z", 2.0))
    st = sector_trends(close)
    rows = []
    for group, syms in F.UNIVERSE.items():
        for a, b in itertools.combinations(syms, 2):
            if a not in close.columns or b not in close.columns:
                continue
            joint = close[[a, b]].dropna()
            if len(joint) < F.MIN_DAYS:
                continue
            rets = joint.pct_change().dropna()
            corr = rets[a].corr(rets[b])
            if corr < F.MIN_CORR:
                continue
            ratio = joint[a] / joint[b]
            hl = F.half_life(ratio)
            adf = F.adf_tstat(ratio)
            coint = bool(np.isfinite(adf) and adf < F.ADF_CRIT_5PCT)
            pair = dict(defaults); pair.update({"a": a, "b": b, "name": f"{a}/{b}"})
            try:
                z_now = float(pairbot.compute_spread(close, pair)["z"].dropna().iloc[-1])
            except (KeyError, IndexError):
                continue
            trades, _, _ = pairbot.backtest_pair(close, pair)
            closed = [t for t in trades if t["status"] == "CLOSED"]
            net = sum(t["pnl"] for t in closed)
            n = len(closed)
            wr = (sum(t["pnl"] > 0 for t in closed) / n * 100) if n else 0
            rows.append({
                "group": group, "pair": f"{a}/{b}", "corr": round(float(corr), 2),
                "half_life": round(float(hl), 1) if np.isfinite(hl) else 999,
                "adf": round(float(adf), 2) if np.isfinite(adf) else 99.0,
                "coint": "yes" if coint else "no",
                "z_now": round(z_now, 2), "signal": "YES" if abs(z_now) >= entry_z else "",
                "trades": n, "win%": round(wr), "net$": round(float(net)),
                "traded": "yes" if f"{a}/{b}" in traded else "",
                "sector": group, "sector_trend": st[group][0],
                "sector_3mo": st[group][1], "sector_200": st[group][2],
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    coint_bonus = (-df["adf"] - (-F.ADF_CRIT_5PCT)).clip(lower=0) * 3
    stretch_bonus = df["z_now"].abs() * 2          # reward currently-stretched pairs
    df["score"] = ((df["net$"] / 100) + (df["win%"] - 50) / 5
                   + (df["corr"] - 0.7) * 20 + coint_bonus + stretch_bonus)
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def build_report(df, top, as_of):
    if df.empty:
        return f"Pair Scout {as_of}: no candidates cleared the filters (corr>={F.MIN_CORR})."
    head = df.head(top)
    lines = [f"PAIR SCOUT — weekly candidates (as of {as_of})", "=" * 52, ""]
    actionable = df[df["signal"] == "YES"]
    if len(actionable):
        lines.append(f">> {len(actionable)} pair(s) ACTIONABLE NOW (|z| past entry): "
                     + ", ".join(f"{r['pair']} z={r['z_now']:+.2f}" for _, r in actionable.iterrows()))
        lines.append("")
    add = df[(df["coint"] == "yes") & (df["net$"] > 0) & (df["traded"] != "yes")
             & (df["half_life"] >= 5) & (df["half_life"] <= 60)]
    if len(add):
        lines.append(f">> {len(add)} ADD-CANDIDATE pair(s) — cointegrated + profitable backtest, NOT yet traded:")
        for _, r in add.head(top).iterrows():
            warn = "  <<! sector sinking — you'd be long a falling sector" if r["sector_trend"] == "DOWN" else ""
            lines.append(f"   + {r['pair']:11s}  sector: {r['sector']}  (3mo {r['sector_3mo']:+d}%, vs200d {r['sector_200']:+d}%)" + warn)
            lines.append(f"       ADF {r['adf']} | half-life {r['half_life']}d | corr {r['corr']} "
                         f"| backtest net ${r['net$']} ({r['win%']}% win) | z_now {r['z_now']:+.2f}")
        lines.append("   -> review these to ADD to pairs.json (expands the live traded set).")
        lines.append("")

    lines.append(f"Top {len(head)} by quality score (* = already traded):")
    for i, r in head.iterrows():
        mark = "*" if r.get("traded") == "yes" else " "
        flag = f" 3mo{r['sector_3mo']:+d}%" + (" !DOWN" if r["sector_trend"] == "DOWN" else "")
        lines.append(
            f"{i+1}.{mark}{r['pair']:11s} [{r['sector']}{flag}] | corr {r['corr']:.2f} | half-life {r['half_life']}d "
            f"| coint {r['coint']} (ADF {r['adf']}) | z_now {r['z_now']:+.2f}"
            f"{'  <-- SIGNAL' if r['signal'] else ''} | net ${r['net$']} ({r['win%']}% win)")
    lines += ["",
              "Guide: want coint=yes (ADF<-2.86), corr>=0.8, half-life ~5-40d, positive net$.",
              "z_now = how stretched the spread is now; |z|>=entry triggers a trade.",
              "sector: 3mo = sector's avg recent 3-month move (DOWN if <-5%); vs200d = structural trend",
              "  (pairs are LONG-ONLY, so opening = going LONG the cheap leg's sector — mind a DOWN tag).",
              "RESEARCH ONLY — review before trading."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Weekly pair-discovery report (research only).")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    with open(PRICES_FILE) as f:
        prices_json = json.load(f)
    close = prices_df(prices_json)
    as_of = str(close.index[-1].date()) if len(close.index) else "n/a"

    df = scout(close)
    report = build_report(df, args.top, as_of)

    with open(REPORT_TXT, "w") as f:
        f.write(report + "\n")
    payload = {"as_of": as_of, "top": df.head(args.top).to_dict("records"),
               "all": df.to_dict("records") if not df.empty else []}
    with open(REPORT_JSON, "w") as f:
        json.dump(payload, f, indent=2)

    print(report)
    print(f"\nWrote {REPORT_TXT} + {REPORT_JSON}")


if __name__ == "__main__":
    main()
