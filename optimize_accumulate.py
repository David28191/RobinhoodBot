"""
optimize_accumulate.py  --  parameter sweep for the SPY accumulator
===================================================================
Holds your contribution sizes fixed (base + dip buy = $50 each) and sweeps the
LEVERS that change behavior:
  * signal   : weekly vs trend
  * entry_z  : how deep a dip to buy extra
  * trim cfg : when/how much to trim (including 'no trim' = just baseline+dips)

It ranks every combo against the DCA benchmark so you can see whether the
buy-low/sell-high overlay actually beats just steadily buying. Run:
    python optimize_accumulate.py
"""

import sys
from pairbot import fetch_prices
import spy_accumulate as A

# what to hold fixed (your personal contribution amounts)
BASE = 50
DIP = 50

SIGNALS = ["weekly", "trend"]
ENTRY_Z = [0.5, 0.75, 1.0, 1.5]
# label, sell_z, trim_pct   (sell_z=99 => effectively never trim)
TRIMS = [
    ("no trim",      99,  0.0),
    ("1.5s / 10%",  1.5, 0.10),
    ("2.0s / 10%",  2.0, 0.10),
    ("2.0s / 25%",  2.0, 0.25),
    ("2.5s / 5%",   2.5, 0.05),
]


def main():
    cfg = A.load_config()
    if len(sys.argv) > 1:                     # optional: python optimize_accumulate.py max
        cfg["history_period"] = sys.argv[1]
    px = fetch_prices([cfg["symbol"]], period=cfg["history_period"])[cfg["symbol"]].dropna()
    print(f"{cfg['symbol']} {cfg['history_period']}: {len(px)} days "
          f"({px.index[0].date()} to {px.index[-1].date()})\n")

    dca = A.acc_metrics([], A.dca_benchmark(px, cfg))
    print(f"DCA benchmark: invested ${dca['invested']:,.0f}  shares {dca['shares']:.1f}  "
          f"profit ${dca['profit']:,.0f}  ROI {dca['roi']:.1f}%  avgBuy ${dca['avg_buy']:.2f}\n")

    rows = []
    for sig in SIGNALS:
        z = A.build_signals({"strategies": [{"signal": sig}],
                             "weekly_vol_lookback": cfg["weekly_vol_lookback"],
                             "trend_ma_days": cfg["trend_ma_days"],
                             "trend_vol_lookback": cfg["trend_vol_lookback"]}, px)[sig]
        for ez in ENTRY_Z:
            for tlabel, sz, tp in TRIMS:
                s = {"signal": sig, "base_buy_dollars": BASE, "buy_dollars": DIP,
                     "entry_z": ez, "sell_z": sz, "trim_pct": tp, "cooldown_days": 5}
                actions, df = A.backtest_accumulate(px, z, s, cfg)
                m = A.acc_metrics(actions, df)
                rows.append({
                    "label": f"{sig:<6} dip<=-{ez:<4} {tlabel}",
                    "roi": m["roi"], "profit": m["profit"], "shares": m["shares"],
                    "invested": m["invested"], "avg_buy": m["avg_buy"],
                    "edge": m["roi"] - dca["roi"], "trims": m["n_trims"],
                })

    rows.sort(key=lambda r: r["roi"], reverse=True)
    print(f"{'Strategy':<34}{'ROI':>8}{'vsDCA':>8}{'Profit':>10}{'Shares':>9}{'Invested':>10}{'AvgBuy':>9}{'Trims':>7}")
    print("-" * 95)
    for r in rows:
        print(f"{r['label']:<34}{r['roi']:>7.1f}%{r['edge']:>+7.1f}%"
              f"${r['profit']:>9,.0f}{r['shares']:>9.1f}${r['invested']:>9,.0f}"
              f"${r['avg_buy']:>8.2f}{r['trims']:>7}")

    best = rows[0]
    print(f"\nBest by ROI: {best['label']}  ({best['roi']:.1f}%, {best['edge']:+.1f}% vs DCA)")
    # also the best that actually trims (buys low AND sells high)
    best_trim = max((r for r in rows if r["trims"] > 0), key=lambda r: r["roi"])
    print(f"Best that still trims: {best_trim['label']}  "
          f"({best_trim['roi']:.1f}%, {best_trim['edge']:+.1f}% vs DCA)")


if __name__ == "__main__":
    main()
