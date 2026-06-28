"""
tune_trim.py  --  two-stage trim analysis for the SPY accumulator
=================================================================
On the tuned base (steady $50/wk + extra on 1.5sigma dips, weekly signal):
  Stage 1: hold trim size = 5%, sweep the trim TRIGGER level (sell_z) -> best level
  Stage 2: at that best level, sweep trim SIZE from 1% to 10%

References shown for context: 'never sell' (trim 0) and plain DCA.
Run:  python tune_trim.py            (default: 33-yr 'max' history)
      python tune_trim.py 10y        (recent bull-market only)
"""

import sys
from pairbot import fetch_prices
import spy_accumulate as A

BASE, DIP, ENTRY_Z, SIGNAL, COOLDOWN = 50, 50, 1.5, "weekly", 5
SELL_Z_GRID = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0]
TRIM_PCT_GRID = [0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10]


def run(px, cfg, sell_z, trim_pct):
    z = A.build_signals({"strategies": [{"signal": SIGNAL}],
                         "weekly_vol_lookback": cfg["weekly_vol_lookback"],
                         "trend_ma_days": cfg["trend_ma_days"],
                         "trend_vol_lookback": cfg["trend_vol_lookback"]}, px)[SIGNAL]
    s = {"signal": SIGNAL, "base_buy_dollars": BASE, "buy_dollars": DIP,
         "entry_z": ENTRY_Z, "sell_z": sell_z, "trim_pct": trim_pct, "cooldown_days": COOLDOWN}
    actions, df = A.backtest_accumulate(px, z, s, cfg)
    return A.acc_metrics(actions, df)


def line(tag, m, ref_roi):
    return (f"{tag:<24}{m['roi']:>7.1f}%{m['roi']-ref_roi:>+8.2f}%"
            f"${m['profit']:>10,.0f}{m['shares']:>9.1f}${m['invested']:>10,.0f}{m['n_trims']:>7}")


def main():
    cfg = A.load_config()
    period = sys.argv[1] if len(sys.argv) > 1 else "max"
    cfg["history_period"] = period
    px = fetch_prices([cfg["symbol"]], period=period)[cfg["symbol"]].dropna()
    print(f"\n{cfg['symbol']} {period}: {len(px)} days "
          f"({px.index[0].date()} to {px.index[-1].date()})")

    dca = A.acc_metrics([], A.dca_benchmark(px, cfg))
    never = run(px, cfg, 99, 0.0)                    # the tuned 'never sell' winner
    ref = never["roi"]
    print(f"\nReferences:  DCA ROI {dca['roi']:.1f}% / profit ${dca['profit']:,.0f}"
          f"   |   NEVER-SELL ROI {never['roi']:.1f}% / profit ${never['profit']:,.0f}"
          f" / shares {never['shares']:.1f}  (vs-col is vs NEVER-SELL)\n")

    hdr = f"{'':<24}{'ROI':>8}{'vsNever':>9}{'Profit':>11}{'Shares':>9}{'Invested':>11}{'Trims':>7}"

    # ---- Stage 1: best trim TRIGGER level, trim size fixed at 5% ----
    print("STAGE 1 - trim 5%, sweep trigger level (sell_z):")
    print(hdr); print("-" * 79)
    stage1 = []
    for sz in SELL_Z_GRID:
        m = run(px, cfg, sz, 0.05)
        stage1.append((sz, m))
        print(line(f"  trim@+{sz}s", m, ref))
    best_sz, best_m = max(stage1, key=lambda t: t[1]["roi"])
    print(f"\n  -> Best trigger by ROI: +{best_sz}s  "
          f"(ROI {best_m['roi']:.1f}%, {best_m['roi']-ref:+.2f}% vs never-sell; "
          f"{best_m['n_trims']} trims)")

    # For Stage 2 the trim SIZE only matters if trims actually fire, so anchor at the
    # best-ROI trigger that still produces a meaningful number of trims (>=5).
    fires = [t for t in stage1 if t[1]["n_trims"] >= 5]
    anchor_sz = max(fires, key=lambda t: t[1]["roi"])[0] if fires else best_sz
    print(f"  -> Stage 2 anchored at +{anchor_sz}s (best trigger that still trims enough "
          f"for size to matter)\n")

    # ---- Stage 2: at anchor trigger, sweep trim SIZE 1%-10% ----
    print(f"STAGE 2 - trigger fixed at +{anchor_sz}s, sweep trim size:")
    print(hdr); print("-" * 79)
    for tp in TRIM_PCT_GRID:
        m = run(px, cfg, anchor_sz, tp)
        tag = "  no trim (never sell)" if tp == 0 else f"  trim {tp*100:.0f}%"
        print(line(tag, m, ref))


if __name__ == "__main__":
    main()
