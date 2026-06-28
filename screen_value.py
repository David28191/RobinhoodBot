"""
screen_value.py  --  S&P 500 "buy-low value" screener
======================================================
Finds beaten-down S&P 500 names -- candidates for the accumulator's buy-low logic
(the same "buy when it's cheap" idea, applied to single stocks instead of just SPY).

What it does (no trading, pure research):
  1. Pulls the current S&P 500 constituent list (symbol, company, GICS sector) from a
     CSV over stdlib urllib -- no scraping libs needed.
  2. Bulk-downloads ~1 year of daily auto-adjusted closes for all ~503 names in ONE
     yfinance call.
  3. For each name computes:
       * price now
       * % below its 52-week HIGH      (how beaten-down it is  -> the buy-low signal)
       * position in its 52-week range  (0.00 = at the low, 1.00 = at the high)
       * 1-month and 3-month return     (momentum -- to spot falling knives)
       * price vs its 200-day average   (above = uptrend dip, below = downtrend)
  4. Ranks by how far below the 52-week high it is and prints the top candidates.
  5. Saves the FULL ranked list to data/value_screen.csv.

Optional: pass --fundamentals to enrich the top names with P/E, P/B and dividend
yield from yfinance (slower, one lookup per name, sometimes patchy).

  python screen_value.py                  # top 25, price metrics only
  python screen_value.py --top 40         # show more
  python screen_value.py --fundamentals   # add P/E, P/B, div yield to the shortlist

NOTE: "beaten-down" is NOT automatically "good value" -- a stock can be cheap because
the business is deteriorating (a value trap). Use the momentum / 200d columns to judge,
and always check live fundamentals + the earnings calendar before buying.
"""

import argparse
import io
import os
import sys
import urllib.request

import numpy as np
import pandas as pd

import pairbot  # reuse its DATA_DIR

SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
OUT_CSV = os.path.join(pairbot.DATA_DIR, "value_screen.csv")


def get_sp500():
    """Return DataFrame[symbol, name, sector] of current S&P 500 members."""
    req = urllib.request.Request(SP500_CSV, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    df = pd.read_csv(io.BytesIO(raw))
    df = df.rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector"})
    return df[["symbol", "name", "sector"]].copy()


def yf_symbol(sym):
    """yfinance uses '-' where the index uses '.' (BRK.B -> BRK-B)."""
    return sym.replace(".", "-")


def download_closes(symbols, period):
    import yfinance as yf
    tickers = [yf_symbol(s) for s in symbols]
    data = yf.download(tickers, period=period, auto_adjust=True, progress=False)
    close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    return close


def compute_metrics(close):
    """One row per ticker with the buy-low metrics. Expects a Close DataFrame
    (columns = yfinance tickers)."""
    rows = []
    for col in close.columns:
        s = close[col].dropna()
        if len(s) < 60:                    # need enough history to mean anything
            continue
        price = float(s.iloc[-1])
        hi = float(s.max())
        lo = float(s.min())
        rng = hi - lo
        pos = (price - lo) / rng if rng > 0 else np.nan       # 0=at low, 1=at high
        below_high = price / hi - 1.0                          # negative = below high
        ma200 = float(s.tail(200).mean())
        vs_ma200 = price / ma200 - 1.0
        ret_1m = price / float(s.iloc[-22]) - 1.0 if len(s) > 22 else np.nan
        ret_3m = price / float(s.iloc[-63]) - 1.0 if len(s) > 63 else np.nan
        rows.append({
            "yf": col, "price": price,
            "pct_below_52w_high": below_high,
            "range_pos": pos,
            "ret_1m": ret_1m, "ret_3m": ret_3m,
            "vs_200d": vs_ma200,
        })
    return pd.DataFrame(rows)


def add_fundamentals(df_top):
    """Enrich the shortlist with P/E, P/B, dividend yield from yfinance .info.
    Slow + occasionally missing -- wrapped so one bad ticker can't kill the run."""
    import yfinance as yf
    pe, pb, dy = [], [], []
    for yfsym in df_top["yf"]:
        info = {}
        try:
            info = yf.Ticker(yfsym).info or {}
        except Exception:
            pass
        pe.append(info.get("trailingPE"))
        pb.append(info.get("priceToBook"))
        d = info.get("dividendYield")
        dy.append(d * 100 if isinstance(d, (int, float)) and d < 1 else d)
    df_top = df_top.copy()
    df_top["pe"], df_top["pb"], df_top["div_yield_%"] = pe, pb, dy
    return df_top


def main():
    ap = argparse.ArgumentParser(description="S&P 500 buy-low value screener (research only).")
    ap.add_argument("--top", type=int, default=25, help="How many candidates to print.")
    ap.add_argument("--history", default="1y", help="yfinance history window (e.g. 1y, 2y).")
    ap.add_argument("--fundamentals", action="store_true",
                    help="Add P/E, P/B, div yield to the shortlist (slower).")
    args = ap.parse_args()

    print("Fetching S&P 500 constituent list ...")
    members = get_sp500()
    print(f"  {len(members)} names.")

    print(f"Downloading {args.history} of daily closes for all names (one bulk call) ...")
    close = download_closes(members["symbol"].tolist(), args.history)
    print(f"  got price history for {close.shape[1]} tickers, {close.shape[0]} days.")

    metrics = compute_metrics(close)

    # map yfinance ticker back to symbol/name/sector
    members["yf"] = members["symbol"].map(yf_symbol)
    out = metrics.merge(members, on="yf", how="left")
    out = out.sort_values("pct_below_52w_high")     # most beaten-down first

    cols = ["symbol", "name", "sector", "price", "pct_below_52w_high",
            "range_pos", "ret_1m", "ret_3m", "vs_200d"]
    out[cols].to_csv(OUT_CSV, index=False)

    top = out.head(args.top).reset_index(drop=True)
    if args.fundamentals:
        print("Pulling fundamentals for the shortlist (slower) ...")
        top = add_fundamentals(top)

    pd.set_option("display.width", 200, "display.max_columns", 20)
    show = top.copy()
    show["price"] = show["price"].map(lambda x: f"${x:,.2f}")
    show["pct_below_52w_high"] = (show["pct_below_52w_high"] * 100).map(lambda x: f"{x:+.0f}%")
    show["range_pos"] = show["range_pos"].map(lambda x: f"{x:.2f}")
    for c in ("ret_1m", "ret_3m", "vs_200d"):
        show[c] = (show[c] * 100).map(lambda x: f"{x:+.0f}%")
    disp_cols = ["symbol", "sector", "price", "pct_below_52w_high",
                 "range_pos", "ret_1m", "ret_3m", "vs_200d"]
    if args.fundamentals:
        for c in ("pe", "pb", "div_yield_%"):
            show[c] = top[c].map(lambda x: f"{x:.1f}" if isinstance(x, (int, float)) and pd.notna(x) else "-")
        disp_cols += ["pe", "pb", "div_yield_%"]

    print(f"\n=== Most beaten-down S&P 500 names (top {args.top}) ===")
    print("pct_below_52w_high = how far under the 1-yr high | range_pos 0=at low,1=at high")
    print("ret_1m/3m = recent momentum (deeply negative = falling knife) | vs_200d = vs 200-day avg\n")
    print(show[disp_cols].to_string(index=False))
    print(f"\nFull ranked list ({len(out)} names) saved to: {OUT_CSV}")
    print("\nRESEARCH ONLY -- beaten-down != good value. Check live fundamentals + earnings before buying.")


if __name__ == "__main__":
    main()
