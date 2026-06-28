"""
spy_accumulate.py  --  SPY "buy dips, trim rips, slowly accumulate" backtester + dashboard
==========================================================================================
This is the ACCUMULATION cousin of spy_wtd.py. The difference matters:

  * spy_wtd.py is a ROUND-TRIP swing trader: it puts a fixed amount in, then takes
    it ALL back out when the move reverts. After every trade you own zero SPY again.

  * spy_accumulate.py keeps a SHARE LEDGER that grows over the years. It can do two
    things on any week:
      - a steady BASELINE buy (a fixed dollar amount, every week, never sold) -- the
        protected accumulation CORE; and
      - opportunistic moves: buy EXTRA dollars when SPY is 'low', and trim a small
        FRACTION of the opportunistic sleeve when it's 'high'. The core is never trimmed,
        so "keep a core" is guaranteed.

Each row of spy_accumulate.json's "strategies" list is one approach, backtested and
compared side by side, plus a plain DCA benchmark (buy weekly, never sell). Two ways
to judge 'low vs high' (per strategy via "signal"):
  * "weekly" = this week's move vs a normal week (short, sharp dips). Same z idea as spy_wtd.
  * "trend"  = price vs its 200-day moving average ('below the long-term line = on sale').

FRACTIONAL SHARES: buys are sized in DOLLARS and share counts rounded to 6 decimals,
matching Robinhood Agentic fractional rules (dollar-based market orders, regular hours,
<=6 decimals, no fractional shorts). So the simulation reflects what would really fill.

You normally edit spy_accumulate.json, not this file. Then run:  python spy_accumulate.py
"""

import json
import os
import datetime as dt
import webbrowser

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from pairbot import fetch_prices, GREEN, RED, BLUE, AMBER, MUTED
from spy_wtd import weekly_frame

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SHARE_DECIMALS = 6          # Robinhood fractional-share precision


# ---------------------------------------------------------------------------
# 1) CONFIG
# ---------------------------------------------------------------------------
def load_config():
    with open(os.path.join(HERE, "spy_accumulate.json")) as f:
        cfg = json.load(f)
    cfg["symbol"] = cfg.get("symbol", "SPY").upper().strip()
    cfg.setdefault("history_period", "10y")
    cfg.setdefault("cost_bps", 2)
    cfg.setdefault("weekly_vol_lookback", 52)
    cfg.setdefault("trend_ma_days", 200)
    cfg.setdefault("trend_vol_lookback", 200)

    defaults = cfg.get("defaults", {})
    merged = []
    for i, s in enumerate(cfg.get("strategies", [{}])):
        m = dict(defaults)
        m.update(s)
        m.setdefault("signal", "weekly")
        m.setdefault("base_buy_dollars", 0)
        m.setdefault("buy_dollars", 50)
        m.setdefault("trim_pct", 0.10)
        m.setdefault("entry_z", 1.0)
        m.setdefault("sell_z", 2.0)
        m.setdefault("cooldown_days", 5)
        m.setdefault("label", f'{m["signal"]} · {m["entry_z"]}σ')
        m["idx"] = i
        merged.append(m)
    cfg["strategies"] = merged
    return cfg


# ---------------------------------------------------------------------------
# 2) SIGNALS  --  each returns a daily z-score: negative = cheap, positive = rich
# ---------------------------------------------------------------------------
def weekly_signal(px, vol_lookback_weeks):
    """How far THIS WEEK's move is from a normal week, in standard deviations."""
    return weekly_frame(px, vol_lookback_weeks)["z"]


def trend_signal(px, ma_days, vol_lookback):
    """How far price is from its long-term moving average, in standard deviations.
    z<0 = below trend (on sale); z>0 = stretched above trend."""
    ma = px.rolling(ma_days).mean()
    dist = np.log(px / ma)
    sd = dist.rolling(vol_lookback).std()
    return dist / sd


def build_signals(cfg, px):
    """Compute each distinct signal once, return {name: z-series}."""
    out = {}
    for name in {s["signal"] for s in cfg["strategies"]}:
        if name == "weekly":
            out[name] = weekly_signal(px, cfg["weekly_vol_lookback"])
        elif name == "trend":
            out[name] = trend_signal(px, cfg["trend_ma_days"], cfg["trend_vol_lookback"])
        else:
            raise ValueError(f"unknown signal '{name}' (use 'weekly' or 'trend')")
    return out


# ---------------------------------------------------------------------------
# 2b) DIP SIZING  --  buy bigger for deeper pullbacks (optional ladder)
# ---------------------------------------------------------------------------
def _shallowest_entry(s):
    """The shallowest dip depth that triggers ANY buy (for 'approaching' hints)."""
    ladder = s.get("dip_ladder")
    return min(t["z"] for t in ladder) if ladder else s["entry_z"]


def dip_buy_dollars(s, z):
    """How many dollars to buy on a dip, given this week's z (negative on a dip).
    If the strategy has a 'dip_ladder' (list of {z, dollars}), buy the amount of
    the DEEPEST tier the pullback has reached -- so a -3sigma week buys more than a
    -1.5sigma week. With no ladder, it's a flat buy_dollars once past entry_z."""
    if z is None or (isinstance(z, float) and np.isnan(z)):
        return 0
    ladder = s.get("dip_ladder")
    if ladder:
        amount = 0
        for tier in sorted(ladder, key=lambda t: t["z"]):     # shallow -> deep
            if -z >= tier["z"]:
                amount = tier["dollars"]                        # keep the deepest breached
        return amount
    return s["buy_dollars"] if z <= -s["entry_z"] else 0


# ---------------------------------------------------------------------------
# 3) ACCUMULATION BACKTEST  --  protected core + opportunistic sleeve
# ---------------------------------------------------------------------------
def backtest_accumulate(px, z, s, cfg):
    """
    Walk day by day. Decide on today's close, FILL at the next session's close
    (no look-ahead). Three things can happen:
      * BASE buy  -> adds to the CORE (first trading day of each week; never sold)
      * BUY  (dip)-> adds to the opportunistic sleeve (cooldown-gated)
      * TRIM (rip)-> sells a fraction of the SLEEVE only (cooldown-gated)
    Share counts rounded to 6 decimals (Robinhood fractional).
    Returns (actions list, daily DataFrame).
    """
    idx = px.index
    n = len(px)
    base = s.get("base_buy_dollars", 0)
    buy_dollars = s["buy_dollars"]
    trim_pct = s["trim_pct"]
    entry_z = s["entry_z"]
    sell_z = s["sell_z"]
    cooldown = s["cooldown_days"]
    cost_rate = cfg["cost_bps"] / 10000.0

    weeks = px.index.to_period("W-FRI")
    first_of_week = ~pd.Series(weeks, index=px.index).duplicated().values

    core = 0.0              # baseline shares, never sold
    sleeve = 0.0            # opportunistic shares, trimmable
    sleeve_basis = 0.0      # $ cost of sleeve shares (for realized P&L on trims)
    cash_in = 0.0
    cash_out = 0.0
    bought_sh = 0.0         # total shares bought (base + dip), for avg-buy-price
    bought_usd = 0.0
    last_i = -10**9
    actions, daily = [], []

    for i in range(n):
        date = idx[i]
        price = px.iat[i]
        zz = z.iat[i]
        has_next = i + 1 < n
        fpx = px.iat[i + 1] if has_next else np.nan
        fdate = idx[i + 1] if has_next else date
        can_fill = has_next and not np.isnan(fpx)

        # 1) baseline weekly buy -> CORE (ignores cooldown; never sold)
        if base > 0 and first_of_week[i] and can_fill:
            fee = base * cost_rate
            add = round((base - fee) / fpx, SHARE_DECIMALS)
            if add > 0:
                core = round(core + add, SHARE_DECIMALS)
                cash_in += base
                bought_sh += add
                bought_usd += base
                actions.append({"side": "BASE", "date": fdate.date().isoformat(),
                                "z": (None if np.isnan(zz) else round(float(zz), 2)),
                                "price": round(float(fpx), 2), "dollars": round(base, 2),
                                "shares_delta": add, "shares_after": round(core + sleeve, SHARE_DECIMALS),
                                "realized": 0.0})

        # 2) opportunistic dip-buy / rip-trim (cooldown-gated)
        ready = (i - last_i) >= cooldown
        if can_fill and ready and not np.isnan(zz):
            dip_dollars = dip_buy_dollars(s, zz)                # bigger for deeper dips
            if dip_dollars > 0:                                 # LOW: buy extra -> sleeve
                fee = dip_dollars * cost_rate
                add = round((dip_dollars - fee) / fpx, SHARE_DECIMALS)
                if add > 0:
                    sleeve = round(sleeve + add, SHARE_DECIMALS)
                    sleeve_basis += dip_dollars
                    cash_in += dip_dollars
                    bought_sh += add
                    bought_usd += dip_dollars
                    last_i = i
                    actions.append({"side": "BUY", "date": fdate.date().isoformat(),
                                    "z": round(float(zz), 2), "price": round(float(fpx), 2),
                                    "dollars": round(dip_dollars, 2), "shares_delta": add,
                                    "shares_after": round(core + sleeve, SHARE_DECIMALS), "realized": 0.0})
            elif zz >= sell_z and sleeve > 0:                   # HIGH: trim the sleeve only
                sell_sh = round(sleeve * trim_pct, SHARE_DECIMALS)
                if sell_sh > 0:
                    gross = sell_sh * fpx
                    fee = gross * cost_rate
                    proceeds = gross - fee
                    avg = sleeve_basis / sleeve
                    realized = proceeds - avg * sell_sh
                    sleeve_basis -= avg * sell_sh
                    sleeve = round(sleeve - sell_sh, SHARE_DECIMALS)
                    cash_out += proceeds
                    last_i = i
                    actions.append({"side": "TRIM", "date": fdate.date().isoformat(),
                                    "z": round(float(zz), 2), "price": round(float(fpx), 2),
                                    "dollars": round(proceeds, 2), "shares_delta": -sell_sh,
                                    "shares_after": round(core + sleeve, SHARE_DECIMALS),
                                    "realized": round(float(realized), 2)})

        shares = core + sleeve
        holdings = shares * price
        profit = holdings + cash_out - cash_in
        daily.append((date, shares, holdings, cash_in, cash_out, profit))

    df = pd.DataFrame(daily, columns=["date", "shares", "holdings",
                                      "invested", "pocketed", "profit"]).set_index("date")
    df.attrs["bought_shares_total"] = bought_sh
    df.attrs["bought_dollars_total"] = bought_usd
    return actions, df


def dca_benchmark(px, cfg):
    """The honest baseline: buy $50 of SPY at the start of every week, never sell."""
    buy_dollars = cfg["strategies"][0]["buy_dollars"] if cfg["strategies"] else 50
    cost_rate = cfg["cost_bps"] / 10000.0
    weeks = px.index.to_period("W-FRI")
    first_of_week = ~pd.Series(weeks, index=px.index).duplicated().values

    shares = 0.0
    cash_in = 0.0
    bought_sh = 0.0
    bought_usd = 0.0
    daily = []
    for i in range(len(px)):
        date = px.index[i]
        price = px.iat[i]
        if first_of_week[i]:
            fee = buy_dollars * cost_rate
            add = round((buy_dollars - fee) / price, SHARE_DECIMALS)
            shares = round(shares + add, SHARE_DECIMALS)
            cash_in += buy_dollars
            bought_sh += add
            bought_usd += buy_dollars
        holdings = shares * price
        daily.append((date, shares, holdings, cash_in, 0.0, holdings - cash_in))
    df = pd.DataFrame(daily, columns=["date", "shares", "holdings",
                                      "invested", "pocketed", "profit"]).set_index("date")
    df.attrs["bought_shares_total"] = bought_sh
    df.attrs["bought_dollars_total"] = bought_usd
    return df


# ---------------------------------------------------------------------------
# 4) METRICS
# ---------------------------------------------------------------------------
def acc_metrics(actions, df):
    last = df.iloc[-1]
    invested = float(last["invested"])
    profit = float(last["profit"])
    roi = (profit / invested * 100) if invested > 0 else 0.0
    bought_sh = df.attrs.get("bought_shares_total", 0.0)
    bought_usd = df.attrs.get("bought_dollars_total", 0.0)
    avg_buy = (bought_usd / bought_sh) if bought_sh > 0 else 0.0
    p = df["profit"]
    max_dd = float((p.cummax() - p).max()) if len(p) > 1 else 0.0
    return {
        "invested": invested, "pocketed": float(last["pocketed"]),
        "holdings": float(last["holdings"]), "shares": float(last["shares"]),
        "profit": profit, "roi": roi, "avg_buy": avg_buy, "max_dd": max_dd,
        "n_base": sum(1 for a in actions if a["side"] == "BASE"),
        "n_buys": sum(1 for a in actions if a["side"] == "BUY"),
        "n_trims": sum(1 for a in actions if a["side"] == "TRIM"),
    }


# ---------------------------------------------------------------------------
# 5) CURRENT READOUT  --  what each strategy would do right now
# ---------------------------------------------------------------------------
def current_readout(px, signals, cfg):
    price = float(px.iloc[-1])
    rows = []
    for s in cfg["strategies"]:
        z = signals[s["signal"]].dropna()
        zz = float(z.iloc[-1]) if z.size else float("nan")
        bits = []
        lvl = "muted"
        if s["base_buy_dollars"] > 0:
            bits.append(f"steady buy ${s['base_buy_dollars']:.0f}/wk")
        dip_d = dip_buy_dollars(s, zz) if not np.isnan(zz) else 0
        if np.isnan(zz):
            bits.append("signal warming up")
        elif dip_d > 0:
            bits.append(f"BUY +${dip_d:.0f} (low)"); lvl = "long"
        elif zz >= s["sell_z"]:
            bits.append(f"TRIM {s['trim_pct']*100:.0f}% of sleeve (high)"); lvl = "short"
        elif zz <= -_shallowest_entry(s) * 0.75:
            bits.append("approaching a buy — watch"); lvl = "warn"
        else:
            bits.append("no dip/rip — hold")
            if s["base_buy_dollars"] > 0:
                lvl = "long"
        rows.append({"label": s["label"], "signal_name": s["signal"],
                     "z": zz, "action": " · ".join(bits), "level": lvl})
    return {"date": px.index[-1].date().isoformat(), "price": price, "rows": rows}


# ---------------------------------------------------------------------------
# 6) COMPUTE
# ---------------------------------------------------------------------------
def compute(cfg, px):
    signals = build_signals(cfg, px)
    results = []
    for s in cfg["strategies"]:
        actions, df = backtest_accumulate(px, signals[s["signal"]], s, cfg)
        results.append({"s": s, "label": s["label"], "actions": actions, "df": df,
                        "metrics": acc_metrics(actions, df), "z": signals[s["signal"]]})

    dca_df = dca_benchmark(px, cfg)
    dca = {"df": dca_df, "metrics": acc_metrics([], dca_df)}

    readout = current_readout(px, signals, cfg)
    best = max(results, key=lambda r: r["metrics"]["profit"])    # most dollars grown
    return results, dca, best, readout


# ---------------------------------------------------------------------------
# 7) DASHBOARD
# ---------------------------------------------------------------------------
def _series_chart(results, dca, col, ytitle):
    fig = go.Figure()
    palette = [BLUE, AMBER, GREEN, "#c678dd", RED]
    for k, r in enumerate(results):
        fig.add_trace(go.Scatter(x=r["df"].index, y=r["df"][col], mode="lines",
                                 name=r["label"], line=dict(color=palette[k % len(palette)], width=2)))
    fig.add_trace(go.Scatter(x=dca["df"].index, y=dca["df"][col], mode="lines",
                             name="DCA (buy weekly)", line=dict(color=MUTED, width=1.5, dash="dot")))
    if col == "profit":
        fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dot"))
    fig.update_layout(template="plotly_dark", height=340,
                      margin=dict(l=55, r=20, t=10, b=30),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.16), yaxis_title=ytitle)
    return fig


def _detail_figure(px, best):
    z, actions, name = best["z"], best["actions"], best["label"]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=(f"SPY price — buys (green) & trims (red) · {name}",
                                        f"{best['s']['signal']} signal z-score"))
    fig.add_trace(go.Scatter(x=px.index, y=px.values, name="SPY", line=dict(color=BLUE)), 1, 1)
    for a in actions:
        if a["side"] == "BASE":
            continue                                            # don't clutter with weekly base buys
        d = pd.Timestamp(a["date"])
        if d in px.index:
            buy = a["side"] == "BUY"
            fig.add_trace(go.Scatter(x=[d], y=[px.loc[d]], mode="markers",
                          marker=dict(symbol="triangle-up" if buy else "triangle-down",
                                      size=10, color=GREEN if buy else RED),
                          showlegend=False, hovertext=f'{a["side"]} {a["shares_delta"]:+.4f} sh'), 1, 1)
    fig.add_trace(go.Scatter(x=z.index, y=z.values, name="z", line=dict(color="#c9d1d9")), 2, 1)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1), row=2, col=1)
    fig.add_hline(y=-best["s"]["entry_z"], line=dict(color=GREEN, width=1, dash="dash"), row=2, col=1)
    fig.add_hline(y=best["s"]["sell_z"], line=dict(color=RED, width=1, dash="dash"), row=2, col=1)
    fig.update_layout(template="plotly_dark", height=520,
                      margin=dict(l=55, r=20, t=30, b=30),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.1))
    return fig


def _body(cfg, px, results, dca, best, readout, embed):
    lvl_color = {"short": RED, "long": GREEN, "warn": AMBER, "muted": MUTED}

    sig_rows = ""
    for row in readout["rows"]:
        c = lvl_color.get(row["level"], MUTED)
        zt = "—" if np.isnan(row["z"]) else f'{row["z"]:+.2f}σ'
        sig_rows += (f'<tr><td class="mono">{row["label"]}</td>'
                     f'<td class="mono">{row["signal_name"]}</td><td class="mono">{zt}</td>'
                     f'<td style="color:{c};font-weight:600">{row["action"]}</td></tr>')

    def cmp_row(label, m, style, star=False):
        pnl_c = GREEN if m["profit"] >= 0 else RED
        roi_c = GREEN if m["roi"] >= 0 else RED
        s = ' <span style="color:%s">★</span>' % AMBER if star else ""
        return (f'<tr{" style=background:#19222e" if star else ""}>'
                f'<td class="mono">{label}{s}</td><td class="mono">{style}</td>'
                f'<td class="mono">${m["invested"]:,.0f}</td>'
                f'<td class="mono">${m["pocketed"]:,.0f}</td>'
                f'<td class="mono">{m["shares"]:.3f}</td>'
                f'<td class="mono">${m["holdings"]:,.0f}</td>'
                f'<td class="mono" style="color:{pnl_c}">${m["profit"]:,.0f}</td>'
                f'<td class="mono" style="color:{roi_c}">{m["roi"]:+.1f}%</td>'
                f'<td class="mono">${m["avg_buy"]:,.2f}</td>'
                f'<td class="mono">{m["n_base"]}/{m["n_buys"]}/{m["n_trims"]}</td></tr>')

    cmp_rows = ""
    for r in results:
        style = "core+timing" if r["s"]["base_buy_dollars"] > 0 else "pure timing"
        cmp_rows += cmp_row(r["label"], r["metrics"], style, star=(r is best))
    cmp_rows += cmp_row("DCA (buy weekly)", dca["metrics"], "never sells")

    act_rows = ""
    shown = [a for a in best["actions"] if a["side"] != "BASE"]
    for a in sorted(shown, key=lambda x: x["date"], reverse=True)[:25]:
        c = GREEN if a["side"] == "BUY" else RED
        rz = "" if a["side"] != "TRIM" else f' (realized ${a["realized"]:,.2f})'
        act_rows += (f'<tr><td style="color:{c};font-weight:600">{a["side"]}</td>'
                     f'<td class="mono">{a["date"]}</td>'
                     f'<td class="mono">{a["z"]:+.2f}σ</td>'
                     f'<td class="mono">${a["price"]:,.2f}</td>'
                     f'<td class="mono">${a["dollars"]:,.2f}{rz}</td>'
                     f'<td class="mono">{a["shares_delta"]:+.4f}</td>'
                     f'<td class="mono">{a["shares_after"]:.4f}</td></tr>')
    if not act_rows:
        act_rows = '<tr><td colspan="7" style="color:#8a8f98">No opportunistic actions (only steady buys).</td></tr>'

    prefix = "acc_" if embed else ""
    shares_html = _series_chart(results, dca, "shares", "SPY shares owned").to_html(
        full_html=False, include_plotlyjs=("cdn" if not embed else False), div_id=prefix + "shares")
    profit_html = _series_chart(results, dca, "profit", "Total profit ($)").to_html(
        full_html=False, include_plotlyjs=False, div_id=prefix + "profit")
    detail_html = _detail_figure(px, best).to_html(
        full_html=False, include_plotlyjs=False, div_id=prefix + "detail")

    bm = best["metrics"]
    return f"""
  <h2>Right now</h2>
  <div class="cards">
    <div class="card"><div class="label">As of</div><div class="value">{readout["date"]}</div></div>
    <div class="card"><div class="label">SPY price</div><div class="value">${readout["price"]:,.2f}</div></div>
  </div>
  <div class="panel"><table>
    <tr><th>Strategy</th><th>Signal</th><th>z now</th><th>What it would do this week</th></tr>
    {sig_rows}
  </table></div>

  <h2>Strategy comparison ({cfg["history_period"]})</h2>
  <div class="sub">★ = most dollars grown (profit). "Avg buy" = average price paid per share (lower = bought cheaper).
      "B/D/T" = baseline buys / dip buys / trims. DCA = just buy ${cfg["strategies"][0]["buy_dollars"]:,.0f} weekly, never sell.</div>
  <div class="panel"><table>
    <tr><th>Strategy</th><th>Style</th><th>Invested</th><th>Pocketed</th><th>Shares</th>
        <th>Holdings $</th><th>Profit</th><th>ROI</th><th>Avg buy</th><th>B/D/T</th></tr>
    {cmp_rows}
  </table></div>

  <h2>Shares accumulated over time</h2>
  <div class="sub">The pile growing. Strategies (solid) vs just buying weekly / DCA (dotted).</div>
  <div class="panel">{shares_html}</div>

  <h2>Total profit over time</h2>
  <div class="panel">{profit_html}</div>

  <h2>Best — {best["label"]}: buys & trims on the price</h2>
  <div class="sub">Baseline {bm["n_base"]}x · dip-bought {bm["n_buys"]}x · trimmed {bm["n_trims"]}x.
      Ended {bm["shares"]:.3f} shares (${bm["holdings"]:,.0f}), pocketed ${bm["pocketed"]:,.0f},
      profit ${bm["profit"]:,.0f} ({bm["roi"]:+.1f}% ROI).</div>
  <div class="panel">{detail_html}</div>

  <h2>Recent opportunistic actions — {best["label"]}</h2>
  <div class="panel"><table>
    <tr><th>Side</th><th>Date</th><th>z</th><th>Price</th><th>$ amount</th><th>Δ shares</th><th>Shares after</th></tr>
    {act_rows}
  </table></div>

  <div class="note">Research/backtest only — simulated, not a promise of real returns, not financial advice.
  Buys are dollar-sized and share counts rounded to 6 decimals (Robinhood Agentic fractional rules: market
  orders, regular hours, no fractional shorts). Long-only: only buys/trims, never shorts — safe for the cash
  Agentic account. Baseline (core) buys are never sold.</div>
"""


def build_dashboard(cfg, px):
    results, dca, best, readout = compute(cfg, px)
    body = _body(cfg, px, results, dca, best, readout, embed=False)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>SPY Accumulator</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
  h1{{margin:0 0 4px}} h2{{margin:28px 0 12px;font-size:18px;border-left:3px solid {GREEN};padding-left:10px}}
  .sub{{color:#8a8f98;font-size:13px;margin-bottom:18px}}
  .mono{{font-family:ui-monospace,Consolas,monospace}}
  .cards{{display:flex;gap:12px;flex-wrap:wrap}}
  .card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 18px;min-width:150px}}
  .card .label{{color:#8a8f98;font-size:12px}} .card .value{{font-size:22px;font-weight:700;margin-top:4px}}
  .panel{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:16px;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d}}
  th{{color:#8a8f98;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
  tr:hover td{{background:#1c2230}}
  .note{{background:#1a1f2b;border:1px solid #2a3344;border-radius:8px;padding:10px 14px;color:#9aa4b2;font-size:12px;margin-top:24px}}
</style></head><body>
  <h1>SPY — Accumulator (buy dips · trim rips · grow a core)</h1>
  <div class="sub">Generated {now} &nbsp;·&nbsp; {cfg["history_period"]} history &nbsp;·&nbsp;
      backtest only, no real money</div>
  {body}
</body></html>"""
    out = os.path.join(HERE, "spy_accumulate_dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out, results, dca, best


def section_html(px=None):
    """Embeddable fragment for the main dashboard (no <html>/<style> wrapper)."""
    cfg = load_config()
    if px is None:
        close = fetch_prices([cfg["symbol"]], period=cfg["history_period"])
        px = close[cfg["symbol"]].dropna()
    results, dca, best, readout = compute(cfg, px)
    body = _body(cfg, px, results, dca, best, readout, embed=True)
    return (f'<h2 style="border-left:3px solid {GREEN};font-size:20px;margin-top:40px">'
            f'SPY — Accumulator (buy dips · trim rips · grow a core)</h2>'
            f'<div class="sub">Buy dollar chunks when SPY is low, trim a slice when high; the pile grows '
            f'over time. Edit <span class="mono">spy_accumulate.json</span> to tune.</div>{body}')


def main():
    print("Loading spy_accumulate.json ...")
    cfg = load_config()
    print(f"Downloading {cfg['symbol']} ({cfg['history_period']}) ...")
    close = fetch_prices([cfg["symbol"]], period=cfg["history_period"])
    px = close[cfg["symbol"]].dropna()
    print(f"Got {len(px)} trading days.")

    out, results, dca, best = build_dashboard(cfg, px)

    print("\nStrategy comparison:")
    rows = [{"label": r["label"], "metrics": r["metrics"]} for r in results]
    rows.append({"label": "DCA (weekly)", "metrics": dca["metrics"]})
    for r in rows:
        m = r["metrics"]
        print(f"  {r['label']:<30} invested ${m['invested']:>7,.0f}  shares {m['shares']:>8.3f}  "
              f"holdings ${m['holdings']:>7,.0f}  profit ${m['profit']:>7,.0f}  ROI {m['roi']:>6.1f}%  "
              f"avgBuy ${m['avg_buy']:>7,.2f}  B/D/T {m['n_base']}/{m['n_buys']}/{m['n_trims']}")
    print(f"\nBest by profit: {best['label']}")
    print(f"Dashboard: {out}")
    webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
