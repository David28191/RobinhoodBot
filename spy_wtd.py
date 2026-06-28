"""
spy_wtd.py  --  SPY "weekly-to-date" mean-reversion / momentum backtester + dashboard
=====================================================================================
A single-instrument cousin of pairbot.py. The idea:

  * Each week has a START (last Friday's close = the "anchor").
  * "Weekly-to-date move" = how far SPY is from that anchor right now.
  * We size that move in standard deviations of a NORMAL week
    (z = weekly-to-date move / typical weekly move).
  * When |z| crosses entry_z (e.g. 1 or 2 standard deviations) we open a trade.

You can run it two ways via spy.json:
  * direction "fade"     = mean-reversion. Down 2σ -> BUY (bet on bounce); up 2σ -> sell/short.
  * direction "momentum" = trend-follow.   Up   2σ -> BUY (ride it);       down 2σ -> sell/short.

EXIT = revert to mean: we HOLD (even across weekends) until the move unwinds
(|z| back inside exit_z) or it blows out further (|z| >= stop_z), or a max_days
time-stop trips. While a trade is open the anchor is FROZEN at entry, so the
weekly reset on Monday can't fake a reversion.

You normally edit spy.json, not this file. Then run:  python spy_wtd.py
"""

import json
import os
import datetime as dt
import webbrowser

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# reuse the data downloader + dashboard colors from the pair engine
from pairbot import fetch_prices, GREEN, RED, BLUE, AMBER, MUTED

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 1) CONFIG
# ---------------------------------------------------------------------------
def load_config():
    with open(os.path.join(HERE, "spy.json")) as f:
        cfg = json.load(f)
    defaults = cfg.get("defaults", {})
    cfg["symbol"] = cfg.get("symbol", "SPY").upper().strip()
    cfg.setdefault("history_period", "10y")
    cfg.setdefault("vol_lookback_weeks", 52)

    merged = []
    for i, sc in enumerate(cfg.get("scenarios", [{}])):
        m = dict(defaults)
        m.update(sc)
        m.setdefault("label", f'{m.get("direction","fade")} · {m.get("entry_z",2.0)}σ')
        m["idx"] = i
        merged.append(m)
    cfg["scenarios"] = merged
    return cfg


# ---------------------------------------------------------------------------
# 2) STRATEGY  --  weekly anchor, typical-week sigma, weekly-to-date z-score
# ---------------------------------------------------------------------------
def weekly_frame(px, vol_lookback_weeks):
    """
    For each trading day, attach:
      anchor  = last close of the PRIOR week (this week's starting line)
      sigma   = rolling std of weekly log-returns (size of a 'normal' week)
      z_live  = ln(price / anchor) / sigma   (this week's move in σ, so far)
    Everything uses only completed prior weeks -> no look-ahead.
    """
    weeks = px.index.to_period("W-FRI")
    weekly_close = px.groupby(weeks).last()
    weekly_ret = np.log(weekly_close / weekly_close.shift(1))
    weekly_sigma = weekly_ret.rolling(vol_lookback_weeks).std()

    # prior week's last close / prior week's rolling sigma, mapped back to each day
    anchor_by_week = weekly_close.shift(1)
    sigma_by_week = weekly_sigma.shift(1)
    anchor = pd.Series(anchor_by_week.reindex(weeks).values, index=px.index)
    sigma = pd.Series(sigma_by_week.reindex(weeks).values, index=px.index)

    z_live = np.log(px / anchor) / sigma
    return pd.DataFrame({"price": px, "anchor": anchor, "sigma": sigma, "z": z_live})


def _entry_signal(direction, z, entry_z):
    """Returns +1 (open long), -1 (open short), or 0 (no signal)."""
    if direction == "fade":            # extreme move -> bet it reverses
        if z <= -entry_z:
            return +1                  # week is unusually DOWN -> buy the dip
        if z >= entry_z:
            return -1                  # week is unusually UP   -> sell/short
    else:                              # momentum -> bet the move continues
        if z >= entry_z:
            return +1                  # week breaking UP   -> buy
        if z <= -entry_z:
            return -1                  # week breaking DOWN -> sell/short
    return 0


def swing_live_decide(cfg, wf, state, capital):
    """LIVE decision for the swing sleeve -- one round-trip position at a time,
    consistent with backtest() above: fade entry on a dip, exit on reversion /
    stop / time, long_only (no shorts). Uses the LAST row of weekly_frame `wf`.
    `state` is the open-position dict (or empty/None when flat). `capital` is the
    dollars to deploy on an open. Returns a list with at most one order dict."""
    import datetime as _dt
    direction = cfg.get("direction", "fade")
    mode = cfg.get("mode", "long_only")
    entry_z, exit_z = cfg["entry_z"], cfg["exit_z"]
    stop_z, max_days = cfg.get("stop_z"), cfg.get("max_days")

    price = float(wf["price"].iloc[-1])
    z_live = float(wf["z"].iloc[-1])
    anchor = float(wf["anchor"].iloc[-1])
    sigma = float(wf["sigma"].iloc[-1])
    today = _dt.date.today()
    open_pos = state if state and state.get("open") else None

    if open_pos is None:                                  # flat -> maybe OPEN
        if np.isnan(z_live):
            return []
        sig = _entry_signal(direction, z_live, entry_z)
        if sig == -1 and mode == "long_only":
            sig = 0                                       # cash account can't short
        if sig == +1 and not (np.isnan(anchor) or np.isnan(sigma)):
            return [{"action": "OPEN", "side": "BUY", "dollars": round(capital, 2),
                     "z": round(z_live, 2), "price": price,
                     "frozen_anchor": anchor, "frozen_sigma": sigma,
                     "reason": f"swing fade entry z={z_live:+.2f}"}]
        return []

    # holding -> measure reversion vs the FROZEN anchor from entry
    fa, fs = open_pos.get("frozen_anchor"), open_pos.get("frozen_sigma")
    zf = np.log(price / fa) / fs if (fa and fs) else np.nan
    try:
        held = (today - _dt.date.fromisoformat(open_pos["entry_date"])).days
    except Exception:
        held = 0
    hit_revert = (not np.isnan(zf)) and abs(zf) <= exit_z
    hit_stop = stop_z is not None and (not np.isnan(zf)) and abs(zf) >= stop_z
    hit_time = max_days is not None and held >= max_days
    if hit_revert or hit_stop or hit_time:
        reason = "reverted" if hit_revert else ("stop-loss" if hit_stop else "time-stop")
        return [{"action": "CLOSE", "side": "SELL", "shares": float(open_pos.get("shares", 0)),
                 "sell_full_position": True, "price": price,
                 "z": round(float(zf), 2) if not np.isnan(zf) else None,
                 "reason": f"swing exit ({reason})"}]
    return []


# ---------------------------------------------------------------------------
# 3) BACKTEST
# ---------------------------------------------------------------------------
def backtest(wf, sc):
    """
    Walk day by day. Decide on today's close, FILL at next session's open-ish
    (next close, to match pairbot's no-look-ahead rule). While open, the anchor
    is FROZEN so 'revert to mean' measures against the original week's start.
    Returns (trades, daily cumulative-P&L Series).
    """
    px = wf["price"]
    z_live = wf["z"]
    anchor = wf["anchor"]
    sigma = wf["sigma"]
    idx = px.index
    n = len(px)

    direction = sc["direction"]
    mode = sc.get("mode", "long_short")
    entry_z = sc["entry_z"]
    exit_z = sc["exit_z"]
    stop_z = sc.get("stop_z")
    max_days = sc.get("max_days")
    capital = sc["capital"]
    cost_bps = sc.get("cost_bps", 0)
    round_trip = capital * (cost_bps / 10000.0) * 2     # spread/slippage, both sides

    position = 0          # 0 flat, +1 long, -1 short
    e_date = e_px = e_z = None
    f_anchor = f_sigma = None
    realized = 0.0
    trades, daily = [], []

    for i in range(n):
        date = idx[i]
        price = px.iat[i]
        zl = z_live.iat[i]

        has_next = i + 1 < n
        fpx = px.iat[i + 1] if has_next else np.nan
        fdate = idx[i + 1] if has_next else date
        can_fill = has_next and not np.isnan(fpx)

        if position == 0:
            if not np.isnan(zl) and can_fill:
                sig = _entry_signal(direction, zl, entry_z)
                if sig == -1 and mode == "long_only":
                    sig = 0                              # cash account can't short
                if sig != 0:
                    position, e_date, e_px, e_z = sig, fdate, fpx, zl
                    f_anchor = anchor.iat[i]
                    f_sigma = sigma.iat[i]
            daily.append((date, realized))
            continue

        # ---- holding a position: measure reversion vs the FROZEN anchor ----
        zf = np.log(price / f_anchor) / f_sigma if f_sigma and f_anchor else np.nan
        held = (date - e_date).days
        hit_revert = not np.isnan(zf) and abs(zf) <= exit_z
        hit_stop = stop_z is not None and not np.isnan(zf) and abs(zf) >= stop_z
        hit_time = max_days is not None and held >= max_days

        if (hit_revert or hit_stop or hit_time) and can_fill:
            ret = fpx / e_px - 1.0
            pnl = position * capital * ret - round_trip
            realized += pnl
            reason = ("stop-loss" if (hit_stop and not hit_revert)
                      else "reverted" if hit_revert else "time-stop")
            trades.append({
                "direction": "LONG" if position == +1 else "SHORT",
                "entry_date": e_date.date().isoformat(),
                "exit_date": fdate.date().isoformat(),
                "days_held": (fdate - e_date).days,
                "entry_z": round(float(e_z), 2),
                "exit_z": round(float(zf), 2) if not np.isnan(zf) else None,
                "pnl": round(float(pnl), 2),
                "return_pct": round(float(pnl / capital * 100), 2),
                "result": "WIN" if pnl > 0 else "LOSS",
                "exit_reason": reason,
                "status": "CLOSED",
            })
            position = 0

        unreal = 0.0
        if position != 0:
            unreal = position * capital * (price / e_px - 1.0) - round_trip / 2
        daily.append((date, realized + unreal))

    if position != 0:                                    # still-open trade at the end
        last_px = px.iloc[-1]
        zf = np.log(last_px / f_anchor) / f_sigma if f_sigma and f_anchor else np.nan
        pnl = position * capital * (last_px / e_px - 1.0) - round_trip / 2
        trades.append({
            "direction": "LONG" if position == +1 else "SHORT",
            "entry_date": e_date.date().isoformat(),
            "exit_date": "(still open)",
            "days_held": (idx[-1] - e_date).days,
            "entry_z": round(float(e_z), 2),
            "exit_z": round(float(zf), 2) if not np.isnan(zf) else None,
            "pnl": round(float(pnl), 2),
            "return_pct": round(float(pnl / capital * 100), 2),
            "result": "OPEN",
            "exit_reason": "open",
            "status": "OPEN",
        })

    daily_series = pd.Series({d: v for d, v in daily}, name=sc["label"]).astype(float)
    return trades, daily_series


def metrics(trades, daily):
    closed = [t for t in trades if t["status"] == "CLOSED"]
    wins = [t for t in closed if t["pnl"] > 0]
    realized = sum(t["pnl"] for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    if len(daily) > 1:
        max_dd = float((daily.cummax() - daily).max())
        chg = daily.diff().dropna()
        sd = chg.std()
        sharpe = float(chg.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0
    else:
        max_dd, sharpe = 0.0, 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in closed if t["pnl"] < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "realized": realized, "trades": len(closed), "win_rate": win_rate,
        "sharpe": sharpe, "max_dd": max_dd, "profit_factor": pf,
        "avg": (realized / len(closed)) if closed else 0.0,
    }


# ---------------------------------------------------------------------------
# 4) CURRENT SIGNAL (what each variant says right now)
# ---------------------------------------------------------------------------
def current_readout(wf, cfg):
    row = wf.dropna(subset=["z"]).iloc[-1]
    price, anchor, z = row["price"], row["anchor"], row["z"]
    wtd_pct = (price / anchor - 1.0) * 100
    out = []
    for sc in cfg["scenarios"]:
        sig = _entry_signal(sc["direction"], z, sc["entry_z"])
        if sig == -1 and sc.get("mode") == "long_only":
            txt, lvl = "no trade (can't short in cash acct)", "muted"
        elif sig == +1:
            txt, lvl = "BUY SPY", "long"
        elif sig == -1:
            txt, lvl = "SELL / SHORT SPY", "short"
        elif abs(z) >= sc["entry_z"] * 0.75:
            txt, lvl = "approaching trigger — watch", "warn"
        else:
            txt, lvl = "no signal (wait)", "muted"
        out.append({"label": sc["label"], "signal": txt, "level": lvl})
    return {"date": wf.index[-1].date().isoformat(), "price": float(price),
            "anchor": float(anchor), "wtd_pct": float(wtd_pct), "z": float(z),
            "rows": out}


# ---------------------------------------------------------------------------
# 5) DASHBOARD
# ---------------------------------------------------------------------------
def _equity_overlay(daily_map):
    fig = go.Figure()
    palette = [BLUE, AMBER, GREEN, "#c678dd", RED, "#56b6c2"]
    full_idx = sorted(set().union(*[d.index for d in daily_map.values()])) if daily_map else []
    for k, (label, d) in enumerate(daily_map.items()):
        s = d.reindex(full_idx).ffill().fillna(0.0)
        fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                                 name=label, line=dict(color=palette[k % len(palette)], width=2)))
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dot"))
    fig.update_layout(template="plotly_dark", height=380,
                      margin=dict(l=50, r=20, t=10, b=30),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.12), yaxis_title="Cumulative P&L ($)")
    return fig


def _detail_figure(wf, sc, trades):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        row_heights=[0.5, 0.5],
                        subplot_titles=(f'{sc["symbol_name"]} price + weekly anchor',
                                        "Weekly-to-date z-score"))
    fig.add_trace(go.Scatter(x=wf.index, y=wf["price"], name="SPY", line=dict(color=BLUE)), 1, 1)
    fig.add_trace(go.Scatter(x=wf.index, y=wf["anchor"], name="week anchor",
                             line=dict(color=MUTED, width=1, dash="dot")), 1, 1)

    fig.add_trace(go.Scatter(x=wf.index, y=wf["z"], name="z (WTD)",
                             line=dict(color="#c9d1d9")), 2, 1)
    for lev, col in [(sc["entry_z"], RED), (-sc["entry_z"], GREEN),
                     (sc["exit_z"], MUTED), (-sc["exit_z"], MUTED),
                     (sc.get("stop_z", 99), "#6e3b3b"), (-sc.get("stop_z", 99), "#6e3b3b")]:
        fig.add_hline(y=lev, line=dict(color=col, width=1, dash="dash"), row=2, col=1)
    fig.add_hline(y=0, line=dict(color=MUTED, width=1), row=2, col=1)

    zser = wf["z"]
    for t in trades:
        ed = pd.Timestamp(t["entry_date"])
        if ed in zser.index:
            fig.add_trace(go.Scatter(x=[ed], y=[zser.loc[ed]], mode="markers",
                          marker=dict(symbol="triangle-up", size=11, color="#ffffff"),
                          showlegend=False, hovertext=f'ENTRY {t["direction"]}'), 2, 1)
        if t["status"] == "CLOSED":
            xd = pd.Timestamp(t["exit_date"])
            if xd in zser.index:
                fig.add_trace(go.Scatter(x=[xd], y=[zser.loc[xd]], mode="markers",
                              marker=dict(symbol="x", size=10,
                                          color=GREEN if t["pnl"] > 0 else RED),
                              showlegend=False, hovertext="EXIT"), 2, 1)
    fig.update_layout(template="plotly_dark", height=520,
                      margin=dict(l=50, r=20, t=30, b=30),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", y=1.1))
    return fig


def compute(cfg, wf):
    """Backtest every scenario and build the shared figures. No file I/O except
    saving the best variant's trade log."""
    results = []
    daily_map = {}
    for sc in cfg["scenarios"]:
        sc["symbol_name"] = cfg["symbol"]
        trades, daily = backtest(wf, sc)
        m = metrics(trades, daily)
        results.append({"sc": sc, "trades": trades, "metrics": m})
        daily_map[sc["label"]] = daily

    # pick the "best" scenario to chart in detail = highest Sharpe, tie-break P&L
    best = max(results, key=lambda r: (r["metrics"]["sharpe"], r["metrics"]["realized"]))
    detail_fig = _detail_figure(wf, best["sc"], best["trades"])
    equity_fig = _equity_overlay(daily_map)
    readout = current_readout(wf, cfg)

    if best["trades"]:
        pd.DataFrame(best["trades"]).to_csv(
            os.path.join(DATA_DIR, "spy_trades.csv"), index=False)
    return results, best, equity_fig, detail_fig, readout


def build_dashboard(cfg, wf):
    """Standalone spy_dashboard.html (its own full page + styles)."""
    results, best, equity_fig, detail_fig, readout = compute(cfg, wf)
    body = _body_sections(cfg, results, best, equity_fig, detail_fig, readout, embed=False)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>SPY Weekly-to-Date Strategy</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
  h1{{margin:0 0 4px}} h2{{margin:28px 0 12px;font-size:18px;border-left:3px solid {BLUE};padding-left:10px}}
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
  <h1>SPY — Weekly-to-Date Strategy</h1>
  <div class="sub">Generated {now} &nbsp;·&nbsp; {cfg["history_period"]} history &nbsp;·&nbsp; σ from {cfg["vol_lookback_weeks"]}-week vol &nbsp;·&nbsp; backtest only, no real money</div>
  {body}
</body></html>"""
    out = os.path.join(HERE, "spy_dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out, results, best


def section_html(px=None):
    """Return an embeddable HTML fragment (no <html>/<style> wrapper) for the
    main pair dashboard. Reuses the pair page's CSS classes + already-loaded
    Plotly. Downloads SPY itself unless `px` (a Series) is passed in."""
    cfg = load_config()
    if px is None:
        close = fetch_prices([cfg["symbol"]], period=cfg["history_period"])
        px = close[cfg["symbol"]].dropna()
    wf = weekly_frame(px, cfg["vol_lookback_weeks"])
    results, best, equity_fig, detail_fig, readout = compute(cfg, wf)
    body = _body_sections(cfg, results, best, equity_fig, detail_fig, readout, embed=True)
    return (f'<h2 style="border-left:3px solid {AMBER};font-size:20px;margin-top:40px">'
            f'SPY — Weekly-to-Date Strategy</h2>'
            f'<div class="sub">Single-instrument cousin of the pairs above. '
            f'{cfg["history_period"]} history · σ from {cfg["vol_lookback_weeks"]}-week vol · '
            f'edit <span class="mono">spy.json</span> to tune.</div>{body}')


def _body_sections(cfg, results, best, equity_fig, detail_fig, readout, embed):
    """The inner sections (Right now / comparison / equity / detail / trades),
    shared by the standalone page and the embedded section. `embed` controls
    Plotly loading and div-id prefixes so figures don't collide with the pairs."""
    lvl_color = {"short": RED, "long": GREEN, "warn": AMBER, "muted": MUTED}

    # comparison table
    cmp_rows = ""
    for r in results:
        m = r["metrics"]
        is_best = r is best
        pf = "∞" if m["profit_factor"] == float("inf") else f'{m["profit_factor"]:.2f}'
        pnl_c = GREEN if m["realized"] >= 0 else RED
        sh_c = GREEN if m["sharpe"] >= 1 else AMBER if m["sharpe"] >= 0 else RED
        star = ' <span style="color:%s">★</span>' % AMBER if is_best else ""
        cmp_rows += (
            f'<tr{" style=background:#19222e" if is_best else ""}>'
            f'<td class="mono">{r["sc"]["label"]}{star}</td>'
            f'<td class="mono">{r["sc"]["mode"]}</td>'
            f'<td class="mono" style="color:{pnl_c}">${m["realized"]:,.0f}</td>'
            f'<td class="mono">{m["trades"]}</td>'
            f'<td class="mono">{m["win_rate"]:.0f}%</td>'
            f'<td class="mono" style="color:{sh_c}">{m["sharpe"]:.2f}</td>'
            f'<td class="mono" style="color:{RED}">-${m["max_dd"]:,.0f}</td>'
            f'<td class="mono">{pf}</td>'
            f'<td class="mono">${m["avg"]:,.0f}</td></tr>')

    # current signal cards
    sig_rows = ""
    for row in readout["rows"]:
        c = lvl_color.get(row["level"], MUTED)
        sig_rows += (f'<tr><td class="mono">{row["label"]}</td>'
                     f'<td style="color:{c};font-weight:600">{row["signal"]}</td></tr>')

    z = readout["z"]
    z_c = RED if z >= 1 else GREEN if z <= -1 else MUTED
    wtd_c = GREEN if readout["wtd_pct"] >= 0 else RED

    # trades table for the best scenario
    tr_rows = ""
    for t in sorted(best["trades"], key=lambda x: x["entry_date"], reverse=True):
        col = {"WIN": GREEN, "LOSS": RED, "OPEN": AMBER}[t["result"]]
        xz = "—" if t["exit_z"] is None else f'{t["exit_z"]:+.2f}'
        tr_rows += (
            f'<tr><td>{t["direction"]}</td>'
            f'<td class="mono">{t["entry_date"]}</td><td class="mono">{t["exit_date"]}</td>'
            f'<td class="mono">{t["days_held"]}d</td>'
            f'<td class="mono">{t["entry_z"]:+.2f} → {xz}</td>'
            f'<td class="mono">{t["exit_reason"]}</td>'
            f'<td class="mono" style="color:{col}">${t["pnl"]:,.0f}</td>'
            f'<td class="mono" style="color:{col}">{t["return_pct"]:+.1f}%</td>'
            f'<td style="color:{col};font-weight:600">{t["result"]}</td></tr>')
    if not tr_rows:
        tr_rows = '<tr><td colspan="9" style="color:#8a8f98">No trades generated.</td></tr>'

    prefix = "spy_" if embed else ""
    eq_html = equity_fig.to_html(full_html=False, include_plotlyjs=False, div_id=prefix + "equity")
    det_html = detail_fig.to_html(full_html=False,
                                  include_plotlyjs=(False if embed else "cdn"),
                                  div_id=prefix + "detail")

    return f"""
  <h2>Right now</h2>
  <div class="cards">
    <div class="card"><div class="label">As of</div><div class="value">{readout["date"]}</div></div>
    <div class="card"><div class="label">SPY price</div><div class="value">${readout["price"]:,.2f}</div></div>
    <div class="card"><div class="label">Week anchor</div><div class="value" style="color:{MUTED}">${readout["anchor"]:,.2f}</div></div>
    <div class="card"><div class="label">Weekly-to-date</div><div class="value" style="color:{wtd_c}">{readout["wtd_pct"]:+.2f}%</div></div>
    <div class="card"><div class="label">z-score</div><div class="value" style="color:{z_c}">{z:+.2f}σ</div></div>
  </div>
  <div class="panel"><table>
    <tr><th>Variant</th><th>Signal today</th></tr>
    {sig_rows}
  </table></div>

  <h2>Variant comparison ({cfg["history_period"]})</h2>
  <div class="sub">★ = best by Sharpe. P&amp;L is on ${cfg["defaults"]["capital"]:,.0f} deployed per trade.</div>
  <div class="panel"><table>
    <tr><th>Variant</th><th>Mode</th><th>Net P&amp;L</th><th>Trades</th><th>Win %</th>
        <th>Sharpe</th><th>Max DD</th><th>Profit factor</th><th>Avg/trade</th></tr>
    {cmp_rows}
  </table></div>

  <h2>Equity curves</h2>
  <div class="panel">{eq_html}</div>

  <h2>Best variant detail — {best["sc"]["label"]} ({best["sc"]["mode"]})</h2>
  <div class="panel">{det_html}</div>

  <h2>Trades — {best["sc"]["label"]}</h2>
  <div class="panel"><table>
    <tr><th>Side</th><th>Entry</th><th>Exit</th><th>Held</th><th>z entry→exit</th>
        <th>Why closed</th><th>P&amp;L</th><th>Return</th><th>Result</th></tr>
    {tr_rows}
  </table></div>

  <div class="note">Research/backtest only. "long_short" assumes you can short SPY; your live
  Robinhood Agentic cash account is long-only, so use the <span class="mono">long_only</span>
  rows to judge what's actually tradable there. Simulated results are not a promise of real
  returns. Nothing here is financial advice.</div>
"""


def main():
    print("Loading spy.json ...")
    cfg = load_config()
    print(f"Downloading {cfg['symbol']} ({cfg['history_period']}) ...")
    close = fetch_prices([cfg["symbol"]], period=cfg["history_period"])
    px = close[cfg["symbol"]].dropna()
    print(f"Got {len(px)} trading days.")

    wf = weekly_frame(px, cfg["vol_lookback_weeks"])
    out, results, best = build_dashboard(cfg, wf)

    print("\nVariant comparison:")
    for r in results:
        m = r["metrics"]
        print(f"  {r['sc']['label']:<16} {r['sc']['mode']:<11} "
              f"P&L ${m['realized']:>8,.0f}  trades {m['trades']:>3}  "
              f"win {m['win_rate']:>3.0f}%  Sharpe {m['sharpe']:>5.2f}  "
              f"maxDD -${m['max_dd']:,.0f}")
    print(f"\nBest by Sharpe: {best['sc']['label']} ({best['sc']['mode']})")
    print(f"Dashboard: {out}")
    webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
