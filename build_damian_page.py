#!/usr/bin/env python3
"""Render data/damian_artifact_data.json into damian_artifact.html (the published page)."""
import json, io, html

d = json.load(open("data/damian_artifact_data.json"))
SC = ["Forward P/E value", "Earnings quality", "Macro trend + valuation"]


def sh(name):
    low = name.lower()
    if "forward" in low or "p/e value" in low:
        return 0
    if "earnings" in low:
        return 1
    return 2


def meter(scans):
    idx = {sh(s) for s in scans}
    return "".join(
        '<i class="b%s"%s></i>' % (" on" if i in idx else "",
                                   ' title="%s"' % html.escape(SC[i]) if i in idx else "")
        for i in range(3))


def money(v):
    if not v:
        return "-"
    return "$%.0fB" % (v / 1e9) if v >= 1e9 else "$%.0fM" % (v / 1e6)


def n(v, f="%.1f"):
    return "-" if v is None else f % v


trip = "".join("""<article class="card">
<header><span class="tk">{t}</span><span class="conf">{m}</span></header>
<p class="nm">{nm}</p>
<dl><div><dt>Fwd P/E</dt><dd>{fpe}</dd></div><div><dt>PEG</dt><dd>{peg}</dd></div>
<div><dt>Net margin</dt><dd>{nmg}</dd></div><div><dt>Cap</dt><dd>{mc}</dd></div></dl>
</article>""".format(t=x["t"], m=meter(x["scans"]), nm=html.escape(x["name"] or "&mdash;"),
                     fpe=n(x["fpe"]), peg=n(x["peg"], "%.2f"),
                     nmg=n((x["nm"] or 0) * 100, "%.0f%%"), mc=money(x["mc"]))
               for x in d["triple"])

rows = "".join("""<tr><td class="r">{i}</td><td><span class="tk">{t}</span><span class="sub">{nm}</span></td>
<td class="num sc">{s}</td><td class="cf"><span class="conf sm">{m}</span></td>
<td class="num">{fpe}<span class="src">{src}</span></td><td class="num">{peg}</td>
<td class="num">{v}</td><td class="num">{mc}</td><td class="sec">{sec}</td></tr>""".format(
    i=i, t=x["t"], nm=html.escape((x["name"] or "")[:34]), s="%.1f" % x["score"],
    m=meter([SC[j] for j in range(x["conf"])]),
    fpe=n(x["fpe"]), src=("&#8226;" if x["src"] == "scanner" else ""),
    peg=n(x["peg"], "%.2f"), v=n(x["val"], "%.0f"), mc=n(x["mac"], "%.0f"),
    sec=html.escape(x["sec"] or "&mdash;"))
    for i, x in enumerate(d["ranked"], 1))

tot = list(d["totals"].values())

CSS = """
:root{
  --paper:#F6F8FA; --raise:#FFFFFF; --ink:#101A24; --ink-2:#41525F; --ink-3:#75868F;
  --line:#DDE4E9; --line-2:#EDF1F4;
  --accent:#B06D14; --accent-soft:#F5E7CE;
  --s3:#0F6F6C; --s2:#B0821C; --off:#DEE5EA;
  --shadow:0 1px 2px rgba(16,26,36,.05),0 1px 8px rgba(16,26,36,.04);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0C141C; --raise:#131E28; --ink:#E6EDF2; --ink-2:#9FB1BD; --ink-3:#6D808D;
  --line:#22303C; --line-2:#1A2733;
  --accent:#E0A44B; --accent-soft:#3A2C13;
  --s3:#3FA9A2; --s2:#D3A643; --off:#202E39;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 1px 10px rgba(0,0,0,.28);
}}
:root[data-theme="dark"]{
  --paper:#0C141C; --raise:#131E28; --ink:#E6EDF2; --ink-2:#9FB1BD; --ink-3:#6D808D;
  --line:#22303C; --line-2:#1A2733;
  --accent:#E0A44B; --accent-soft:#3A2C13;
  --s3:#3FA9A2; --s2:#D3A643; --off:#202E39;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 1px 10px rgba(0,0,0,.28);
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;line-height:1.55;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:32px 20px 72px}
h1,h2,h3{font-family:Archivo,ui-sans-serif,system-ui,sans-serif;text-wrap:balance;margin:0}
.tk,.num,dd,.stat b{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,monospace;
  font-variant-numeric:tabular-nums}
.mast{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;justify-content:space-between;
  padding-bottom:18px;border-bottom:2px solid var(--ink)}
h1{font-size:clamp(28px,4.4vw,42px);font-weight:700;letter-spacing:-.022em}
.tag{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink-3)}
.ro{display:inline-block;margin-top:6px;padding:3px 9px;border:1px solid var(--accent);
  border-radius:3px;color:var(--accent);background:var(--accent-soft);
  font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase}
.lede{max-width:66ch;color:var(--ink-2);margin:18px 0 0;font-size:15.5px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);margin:26px 0 0;border-radius:5px;overflow:hidden}
.stat{background:var(--raise);padding:14px 16px}
.stat b{display:block;font-size:26px;font-weight:600;letter-spacing:-.02em}
.stat span{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3)}
section{margin-top:44px}
.hd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h2{font-size:20px;font-weight:650;letter-spacing:-.012em}
.note{color:var(--ink-2);font-size:14px;max-width:68ch;margin:0 0 18px}
.conf{display:inline-flex;gap:3px}
.conf i{width:13px;height:5px;border-radius:1px;background:var(--off);display:block}
.conf i.on{background:var(--s2)}
.conf i.on:first-child{background:var(--s3)}
.conf.sm i{width:9px;height:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.card{background:var(--raise);border:1px solid var(--line);border-radius:6px;padding:13px 14px;
  box-shadow:var(--shadow)}
.card header{display:flex;justify-content:space-between;align-items:center;gap:8px}
.card .tk{font-size:16px;font-weight:600;letter-spacing:.01em}
.card .nm{margin:2px 0 10px;font-size:12px;color:var(--ink-3);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card dl{display:grid;grid-template-columns:1fr 1fr;gap:7px 10px;margin:0;
  border-top:1px solid var(--line-2);padding-top:9px}
.card dt{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)}
.card dd{margin:0;font-size:14px;font-weight:500}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--raise)}
table{border-collapse:collapse;width:100%;min-width:760px;font-size:13.5px}
th{position:sticky;top:0;background:var(--raise);text-align:left;font-size:10px;font-weight:600;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);
  padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--line-2);vertical-align:middle}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--line-2)}
td.r{color:var(--ink-3);font-family:"IBM Plex Mono",monospace;font-size:11.5px;width:34px}
td .tk{font-weight:600;font-size:13.5px}
td .sub{display:block;font-size:11px;color:var(--ink-3);
  max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.num{text-align:right;white-space:nowrap}
td.sc{font-weight:600;font-size:14.5px;color:var(--accent)}
.src{color:var(--s3);margin-left:3px;font-size:10px}
.sec{font-size:11.5px;color:var(--ink-2);white-space:nowrap}
.cf{width:44px}
.meth{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.pill{background:var(--raise);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:5px;padding:13px 15px}
.pill h3{font-size:13px;font-weight:650;margin-bottom:5px}
.pill p{margin:0;font-size:13px;color:var(--ink-2)}
.caveat{margin-top:38px;padding:16px 18px;border:1px solid var(--line);
  border-radius:6px;background:var(--raise)}
.caveat p{margin:0;font-size:13.5px;color:var(--ink-2);max-width:76ch}
.caveat strong{color:var(--ink)}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
  font-size:12px;color:var(--ink-3);display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

HTML = """<title>Damian Market Scan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>%s</style>
<div class="wrap">
<div class="mast">
  <div><h1>Damian</h1><div class="ro">Research only &middot; places no orders</div></div>
  <div class="tag">Market scan &middot; %s</div>
</div>
<p class="lede">A market-wide screen for stocks that look mispriced on <strong>forward earnings</strong>,
hold up on <strong>earnings quality</strong>, and are not fighting their sector. Three independent
screens run against the whole US market; the names below are where they agree.</p>

<div class="stats">
  <div class="stat"><b>%d</b><span>Tickers surfaced</span></div>
  <div class="stat"><b>%d</b><span>Scored</span></div>
  <div class="stat"><b>%d</b><span>In all three screens</span></div>
  <div class="stat"><b>%d</b><span>Fwd P/E matches</span></div>
</div>

<section>
  <div class="hd"><h2>Triple-confirmed</h2><span class="tag">value &middot; quality &middot; trend</span></div>
  <p class="note">These %d names cleared <em>all three</em> screens at once. Independent screens
  agreeing is the strongest single signal here &mdash; it means a name is cheap on forward earnings,
  profitable, <em>and</em> trending, rather than cheap for a reason. Sorted by PEG.</p>
  <div class="grid">%s</div>
</section>

<section>
  <div class="hd"><h2>Ranked</h2><span class="tag">top 20 by composite score</span></div>
  <p class="note">Score blends valuation, earnings and macro, renormalised over whatever data a name
  actually has, plus a bonus for appearing in more than one screen. A <span class="src">&#8226;</span>
  marks a forward P/E taken from Robinhood&rsquo;s native field rather than derived from quarterly EPS.</p>
  <div class="scroll"><table>
  <thead><tr><th></th><th>Ticker</th><th class="num">Score</th><th>Screens</th>
  <th class="num">Fwd P/E</th><th class="num">PEG</th><th class="num">Val</th>
  <th class="num">Macro</th><th>Sector</th></tr></thead>
  <tbody>%s</tbody></table></div>
</section>

<section>
  <div class="hd"><h2>How a name earns its score</h2></div>
  <div class="meth">
    <div class="pill"><h3>Valuation</h3><p>Forward P/E level, its discount to the sector median, and
    <em>compression</em> &mdash; a trailing P/E well above the forward one means earnings are growing
    into the multiple. A P/E below the floor scores <em>down</em>, not up: that is usually a value trap.</p></div>
    <div class="pill"><h3>Earnings</h3><p>Beat rate and average surprise across up to eight quarters,
    year-over-year EPS growth, and the net-margin trend. Surprise and growth are capped so one
    freak quarter cannot carry a name.</p></div>
    <div class="pill"><h3>Macro</h3><p>The sector&rsquo;s own 3-month trend, relative strength against
    SPY, and where the price sits in its 52-week range &mdash; rewarding the middle, penalising both
    extended tops and falling knives.</p></div>
  </div>
</section>

<div class="caveat">
  <p><strong>Read this as a shortlist, not advice.</strong> Scores rank names against each other
  inside this scan on this date; they say nothing about absolute value or downside. The screens are
  mechanical &mdash; they cannot see a lawsuit, a guidance cut, or a business in secular decline.
  A low multiple is a question, not an answer. Check the news and the filings before acting on any
  name, and treat an upcoming earnings date as a coin-flip, not a catalyst.</p>
</div>

<footer><span>Damian &middot; RobinhoodBot</span><span>Generated %s &middot; scans run live against the full US market</span></footer>
</div>""" % (CSS, d["asof"], d["universe"], d["scored"], len(d["triple"]),
             tot[0]["total"], len(d["triple"]), trip, rows, d["asof"])

io.open("damian_artifact.html", "w", encoding="utf-8").write(HTML)
print("wrote damian_artifact.html  %.1f KB" % (len(HTML) / 1024))
