"""
run.py  --  Run this to build/refresh the dashboard.

What it does:
  1. Reads your pairs.json
  2. Downloads the latest free price history
  3. Backtests every pair and computes current watchlist signals
  4. Writes dashboard.html and opens it in your browser

How to run (in a terminal, from this folder):
    python run.py
"""

import webbrowser

import pairbot
import spy_wtd
import spy_accumulate


def main():
    print("Loading config from pairs.json ...")
    cfg = pairbot.load_config()

    tickers = set()
    for p in cfg["pairs"] + cfg["watchlist"]:
        tickers.add(p["a"])
        tickers.add(p["b"])

    print(f"Downloading {len(tickers)} tickers ({cfg['history_period']} history) ...")
    close = pairbot.fetch_prices(tickers, period=cfg["history_period"])
    print(f"Got {len(close)} days of data, {close.shape[1]} tickers.")

    # Build the SPY sections. Each is guarded: if anything goes wrong (e.g. network),
    # we still produce the pairs dashboard — that block just shows an error note.
    # The accumulator (your main 60%-of-the-bot SPY strategy) goes first; the
    # weekly-to-date swing engine stays below as research.
    print("Building SPY accumulator section ...")
    try:
        acc_section = spy_accumulate.section_html()
    except Exception as e:
        acc_section = (
            '<h2 style="border-left:3px solid #3fb950;font-size:20px;margin-top:40px">'
            'SPY — Accumulator</h2>'
            f'<div class="note">Could not build the accumulator section this run: {e}</div>')
        print(f"  (accumulator section skipped: {e})")

    print("Building SPY weekly-to-date section ...")
    try:
        spy_section = spy_wtd.section_html()
    except Exception as e:
        spy_section = (
            '<h2 style="border-left:3px solid #f0b90b;font-size:20px;margin-top:40px">'
            'SPY — Weekly-to-Date Strategy</h2>'
            f'<div class="note">Could not build the SPY section this run: {e}</div>')
        print(f"  (SPY section skipped: {e})")

    print("Backtesting and building dashboard ...")
    out, trades = pairbot.build_dashboard(cfg, close, extra_html=acc_section + spy_section)

    closed = [t for t in trades if t["status"] == "CLOSED"]
    print(f"Done. {len(closed)} closed trades, {len(trades) - len(closed)} open.")
    print(f"Dashboard written to: {out}")
    print("Trade log saved to:   data/trades.csv")

    webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
