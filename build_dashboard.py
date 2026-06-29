"""
build_dashboard.py  --  unified RobinhoodBot dashboard (single self-contained HTML)
===================================================================================
Pulls together everything in ONE intuitive page:
  * account header (value / cash / deployed / open P&L)
  * a TAB TOGGLE across the strategies:  Overview | SPY Accumulator | Pairs | QQQ Swing
  * Overview = allocation, recent trades, and the latest pair-scout RECOMMENDATIONS
  * each strategy tab = its rules, budget, live positions + P&L

Data sources (all local; refreshed by the cloud routines or a manual fetch):
  data/account_snapshot.json   account + positions + trades + quotes (real, from MCP)
  data/pair_scout.json         latest scout output (recommendations + sector trend)
  pairs.json / spy_accumulate.json / swing.json   the strategy configs

  python build_dashboard.py            # writes bot_dashboard.html and opens it
"""

import json
import os
import webbrowser

import allocation

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "bot_dashboard.html")


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def money(x):
    return f"${x:,.2f}"


def pnl_span(pnl, pct=None):
    cls = "pos" if pnl >= 0 else "neg"
    extra = f" ({pct:+.2f}%)" if pct is not None else ""
    return f'<span class="{cls}">{pnl:+.2f}{extra}</span>'


def strat_of(symbol):
    return {"SPY": "Accumulator", "QQQ": "Swing"}.get(symbol, "Pairs")


def build():
    snap = load(os.path.join(DATA, "account_snapshot.json"))
    scout = load(os.path.join(DATA, "pair_scout.json"))
    acc_cfg = load(os.path.join(HERE, "spy_accumulate.json"))
    swing_cfg = load(os.path.join(HERE, "swing.json"))
    pairs_cfg = load(os.path.join(HERE, "pairs.json"))

    acct = snap.get("account", {"total_value": 0, "cash": 0, "equity": 0})
    quotes = snap.get("quotes", {})
    positions = snap.get("positions", [])
    trades = snap.get("trades", [])

    total = float(acct.get("total_value", 0))
    al = allocation.load_allocation(total if total else None)

    # ---- positions w/ P&L + strategy tag ----
    pos_rows, open_pnl, deployed = [], 0.0, {"Accumulator": 0.0, "Pairs": 0.0, "Swing": 0.0}
    for p in positions:
        sym, qty, avg = p["symbol"], float(p["qty"]), float(p["avg"])
        last = float(quotes.get(sym, avg))
        cost, val = qty * avg, qty * last
        pnl = val - cost
        pct = (pnl / cost * 100) if cost else 0
        open_pnl += pnl
        strat = strat_of(sym)
        deployed[strat] = deployed.get(strat, 0) + cost
        pos_rows.append((strat, sym, qty, avg, last, cost, val, pnl, pct))

    # ---- recommendations from the scout ----
    allrecs = scout.get("all", scout.get("top", []))
    def is_add(r):
        return (r.get("coint") == "yes" and r.get("net$", 0) > 0 and r.get("traded") != "yes"
                and 5 <= r.get("half_life", 999) <= 60)
    adds = [r for r in allrecs if is_add(r)]
    actionable = [r for r in allrecs if r.get("signal") == "YES"]

    # =====================================================================
    # HTML pieces
    # =====================================================================
    def stat(label, value, sub=""):
        return (f'<div class="stat"><div class="lbl">{label}</div>'
                f'<div class="val">{value}</div><div class="sub">{sub}</div></div>')

    header = f"""
    <div class="head">
      <div class="brand">🤖 RobinhoodBot <span class="tag">Agentic ••8249</span></div>
      <div class="asof">as of {snap.get('as_of','—')}</div>
    </div>
    <div class="stats">
      {stat("Account value", money(total))}
      {stat("Cash", money(acct.get('cash',0)))}
      {stat("Deployed", money(sum(deployed.values())))}
      {stat("Open P&amp;L", pnl_span(open_pnl))}
    </div>"""

    # allocation bars
    def bar(name, pct, dollars, used):
        usedpct = (used / dollars * 100) if dollars else 0
        return f"""
        <div class="alloc">
          <div class="alloc-top"><b>{name}</b><span>{pct:.0f}% &middot; {money(dollars)} budget &middot; {money(used)} used</span></div>
          <div class="track"><div class="fill" style="width:{min(usedpct,100):.0f}%"></div></div>
        </div>"""
    alloc_html = (bar("SPY Accumulator", al["spy_pct"], al["spy_budget"], deployed["Accumulator"])
                  + bar("Pairs", al["pairs_pct"], al["pairs_budget"], deployed["Pairs"])
                  + bar("QQQ Swing", al["swing_pct"], al["swing_budget"], deployed["Swing"]))

    # trades table
    trows = ""
    for t in trades:
        sym = t["symbol"]; cur = float(quotes.get(sym, t["price"]))
        ret = (cur / float(t["price"]) - 1) * 100
        trows += (f"<tr><td>{t['date']}</td><td><span class='chip'>{strat_of(sym)}</span></td>"
                  f"<td>{t['side'].upper()} {money(t['dollars'])} {sym}</td>"
                  f"<td>${float(t['price']):,.2f}</td><td>{pnl_span(ret/100*float(t['dollars']), ret)}</td></tr>")
    trades_html = f"""
      <table><thead><tr><th>Date</th><th>Strategy</th><th>Order</th><th>Fill</th><th>P&amp;L since</th></tr></thead>
      <tbody>{trows or '<tr><td colspan=5>No trades yet.</td></tr>'}</tbody></table>"""

    # recommendations
    def rec_card(r):
        down = r.get("sector_trend") == "DOWN"
        warn = '<div class="warn">⚠ sector recently falling — you\'d be long a sinking sector</div>' if down else ""
        sect = f"{r.get('sector','?')} ({r.get('sector_3mo',0):+d}% 3mo)"
        return f"""
        <div class="rec {'down' if down else ''}">
          <div class="rec-h"><b>{r['pair']}</b><span class="sect">{sect}</span></div>
          <div class="rec-m">coint <b>{r.get('coint')}</b> (ADF {r.get('adf')}) &middot; half-life {r.get('half_life')}d
            &middot; z_now <b>{r.get('z_now'):+.2f}</b> &middot; backtest {r.get('win%')}% win</div>
          {warn}
        </div>"""
    add_html = "".join(rec_card(r) for r in adds[:6]) or "<div class='muted'>No add-candidates this week.</div>"

    def act_table(items):
        if not items:
            return "<div class='muted'>None right now.</div>"
        rows = ""
        for r in items[:15]:
            a, b = r["pair"].split("/")
            buy = a if r["z_now"] < 0 else b
            down = r.get("sector_trend") == "DOWN"
            spring = "✓" if r.get("coint") == "yes" else "—"
            sect = (f"{r.get('sector','?')} <span class='muted'>"
                    f"({r.get('sector_3mo',0):+d}% / {r.get('sector_200',0):+d}%)</span>")
            warn = " <span class='neg'>⚠</span>" if down else ""
            tr = "●" if r.get("traded") == "yes" else ""
            rows += (f"<tr class='{'down' if down else ''}'><td><b>{r['pair']}</b></td>"
                     f"<td><b>{r['z_now']:+.2f}</b></td><td><span class='chip'>BUY {buy}</span></td>"
                     f"<td>{sect}{warn}</td><td>{spring} <span class='muted'>{r.get('adf')}</span></td>"
                     f"<td>{r.get('corr')}</td><td>{r.get('half_life')}d</td>"
                     f"<td>{r.get('win%')}%</td><td>{tr}</td></tr>")
        return ("<table><thead><tr><th>Pair</th><th>z</th><th>Action</th><th>Sector (3mo/200d)</th>"
                "<th>Coint (ADF)</th><th>Corr</th><th>Half-life</th><th>Win%</th><th>Trading</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")
    act_html = act_table(actionable)

    overview = f"""
      <h2>Allocation</h2>{alloc_html}
      <h2>Recent trades</h2>{trades_html}
      <h2>Recommendations to ADD <span class="muted">(cointegrated + profitable, not yet traded)</span></h2>
      <div class="recs">{add_html}</div>
      <h2>Actionable now <span class="muted">(|z| past entry — would trigger a trade)</span></h2>
      {act_html}
      <p class="muted small">BUY = the cheap leg it would buy &middot; Coint ✓ = trustworthy spring (ADF&lt;-2.86)
        &middot; Sector (3mo/200d) = recent vs structural trend, ⚠ = recently falling &middot; ● = already trading</p>"""

    # ---- strategy tabs ----
    def pos_table(filter_strat):
        rows = ""
        for (strat, sym, qty, avg, last, cost, val, pnl, pct) in pos_rows:
            if strat != filter_strat:
                continue
            rows += (f"<tr><td>{sym}</td><td>{qty:.6f}</td><td>${avg:,.2f}</td><td>${last:,.2f}</td>"
                     f"<td>{money(val)}</td><td>{pnl_span(pnl, pct)}</td></tr>")
        if not rows:
            rows = "<tr><td colspan=6 class='muted'>No open position.</td></tr>"
        return ("<table><thead><tr><th>Symbol</th><th>Shares</th><th>Avg</th><th>Last</th>"
                f"<th>Value</th><th>P&amp;L</th></tr></thead><tbody>{rows}</tbody></table>")

    d = acc_cfg.get("defaults", {})
    ladder = " &middot; ".join(f"{t['z']}σ→${t['dollars']}" for t in d.get("dip_ladder", []))
    spy_tab = f"""
      <h2>SPY Accumulator <span class="muted">— buy &amp; hold core + buy dips</span></h2>
      <p>Weekly base buy <b>${d.get('base_buy_dollars')}</b> (never sold) &middot; dip ladder: {ladder}
         &middot; trim {int(d.get('trim_pct',0)*100)}% of the dip-sleeve at +{d.get('sell_z')}σ.</p>
      <p class="muted">Budget {money(al['spy_budget'])} ({al['spy_pct']:.0f}%) &middot; deployed {money(deployed['Accumulator'])}</p>
      {pos_table('Accumulator')}"""

    plist = ", ".join(f"{p['a']}/{p['b']}" for p in pairs_cfg.get("pairs", []))
    pd_ = pairs_cfg.get("defaults", {})
    pairs_tab = f"""
      <h2>Pairs <span class="muted">— buy the cheap leg when the spread stretches (long-only)</span></h2>
      <p>Traded: <b>{plist}</b></p>
      <p>Open |z|≥{pd_.get('entry_z')} → buy {money(pd_.get('capital_per_leg',0))} of the cheap leg
         &middot; close on revert/stop/time &middot; max {pairs_cfg.get('max_positions')} positions.</p>
      <p class="muted">Budget {money(al['pairs_budget'])} ({al['pairs_pct']:.0f}%) &middot; deployed {money(deployed['Pairs'])}</p>
      {pos_table('Pairs')}
      <p class="muted small">⚠ long-only = opening a pair is an outright LONG of the cheap leg's sector — see the sector tags in Recommendations.</p>"""

    swing_open = any(s == "Swing" for (s, *_rest) in pos_rows)
    swing_tab = f"""
      <h2>QQQ Swing <span class="muted">— fade weekly dips, round-trip</span></h2>
      <p>Buy <b>{money(al['swing_budget'])}</b> of QQQ when the week is ≤ −{swing_cfg.get('entry_z')}σ;
         sell the whole position on revert (≤{swing_cfg.get('exit_z')}σ) / stop ({swing_cfg.get('stop_z')}σ) / {swing_cfg.get('max_days')}d.</p>
      <p class="muted">Budget {money(al['swing_budget'])} ({al['swing_pct']:.0f}%) &middot; status:
         <b>{'HOLDING' if swing_open else 'flat (waiting for a dip)'}</b> &middot; QQQ ${quotes.get('QQQ','—')}</p>
      {pos_table('Swing')}"""

    # =====================================================================
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RobinhoodBot Dashboard</title>
<style>
  :root {{ --bg:#0e1117; --card:#161b22; --line:#222a35; --txt:#e6edf3; --mut:#8b949e;
           --accent:#2f81f7; --pos:#3fb950; --neg:#f85149; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--txt);
          font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:980px; margin:0 auto; padding:24px 18px 60px; }}
  .head {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .brand {{ font-size:22px; font-weight:700; }}
  .tag {{ font-size:12px; color:var(--mut); border:1px solid var(--line); border-radius:20px; padding:2px 8px; margin-left:6px;}}
  .asof {{ color:var(--mut); font-size:13px; }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:18px 0 8px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }}
  .stat .lbl {{ color:var(--mut); font-size:12px; }}
  .stat .val {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .stat .sub {{ color:var(--mut); font-size:12px; }}
  .tabs {{ display:flex; gap:6px; margin:22px 0 14px; flex-wrap:wrap; }}
  .tabs button {{ background:var(--card); color:var(--txt); border:1px solid var(--line);
                  border-radius:10px; padding:9px 14px; cursor:pointer; font-size:14px; }}
  .tabs button.active {{ background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }}
  .panel {{ display:none; }} .panel.active {{ display:block; }}
  h2 {{ font-size:16px; margin:22px 0 10px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
  th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); font-size:14px; }}
  th {{ color:var(--mut); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
  tr:last-child td {{ border-bottom:none; }}
  tr.down {{ background:#1c1517; }}
  .pos {{ color:var(--pos); font-weight:600; }} .neg {{ color:var(--neg); font-weight:600; }}
  .muted {{ color:var(--mut); }} .small {{ font-size:13px; }}
  .chip {{ font-size:12px; background:#1f2630; border:1px solid var(--line); border-radius:6px; padding:1px 7px; }}
  .alloc {{ margin:10px 0; }}
  .alloc-top {{ display:flex; justify-content:space-between; font-size:13px; margin-bottom:5px; }}
  .alloc-top span {{ color:var(--mut); }}
  .track {{ height:9px; background:#0b0f15; border:1px solid var(--line); border-radius:20px; overflow:hidden; }}
  .fill {{ height:100%; background:linear-gradient(90deg,var(--accent),#56a2ff); }}
  .recs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; }}
  .rec {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:13px; }}
  .rec.down {{ border-color:#5c2a2a; }}
  .rec-h {{ display:flex; justify-content:space-between; align-items:center; }}
  .rec-h b {{ font-size:16px; }} .sect {{ color:var(--mut); font-size:12px; }}
  .rec-m {{ color:var(--mut); font-size:13px; margin-top:6px; }}
  .warn {{ color:#f0a020; font-size:12.5px; margin-top:8px; }}
  p {{ margin:8px 0; }}
</style></head><body><div class="wrap">
  {header}
  <div class="tabs">
    <button class="tab active" data-t="overview">📊 Overview</button>
    <button class="tab" data-t="spy">📈 SPY Accumulator</button>
    <button class="tab" data-t="pairs">🔀 Pairs</button>
    <button class="tab" data-t="swing">⚡ QQQ Swing</button>
  </div>
  <div id="overview" class="panel active">{overview}</div>
  <div id="spy" class="panel">{spy_tab}</div>
  <div id="pairs" class="panel">{pairs_tab}</div>
  <div id="swing" class="panel">{swing_tab}</div>
  <p class="muted small" style="margin-top:30px">Research view — the live trader runs in the cloud.
     Regenerate with <code>python build_dashboard.py</code> after refreshing data/account_snapshot.json.</p>
</div>
<script>
  const tabs = document.querySelectorAll('.tab');
  tabs.forEach(b => b.onclick = () => {{
    tabs.forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    b.classList.add('active');
    document.getElementById(b.dataset.t).classList.add('active');
  }});
</script></body></html>"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUT}")
    try:
        webbrowser.open("file://" + OUT.replace(os.sep, "/"))
    except Exception:
        pass


if __name__ == "__main__":
    build()
