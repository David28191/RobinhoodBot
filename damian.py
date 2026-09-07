#!/usr/bin/env python3
"""Damian - research-only stock scanner (forward P/E + earnings + macro).

RESEARCH ONLY. Damian never emits an order and is never imported by cloud_decide.py.
It ranks the discovery universe and writes a report; a human decides what to act on.

Three pillars (weights in damian.json):
  1. VALUATION - forward P/E, taken from the Robinhood scanner's native Forward P/E
                 when the name came from a scan, else CONSTRUCTED as price /
                 (last 3 reported quarters + next quarter's estimate). Scored on
                 level, discount vs sector median, and P/E compression
                 (trailing P/E > forward P/E = earnings growing into the multiple).
  2. EARNINGS  - beat rate, average surprise, YoY EPS growth, net-margin trend.
  3. MACRO     - sector trend (median 3mo return of its sector peers), relative
                 strength vs the benchmark, and position in the 52-week range.

DISCOVERY is market-wide: the routine runs saved Robinhood screeners (ids in
damian.json -> scans) and saves them to data/damian_scan_results.json. There is no
hardcoded universe; find_pairs.UNIVERSE is only a fallback.

Data is supplied by the cloud routine as JSON dumps (same pattern as cloud_decide.py -
no yfinance; the sandbox blocks Yahoo):
    data/damian_scan_results.json   {title: run_scan payload}     (market-wide discovery)
    data/mcp_prices.json            {sym: [{date, close}, ...]}   (extract_mcp_prices.py)
    data/damian_fundamentals.json   raw get_equity_fundamentals payload(s)
    data/damian_earnings.json       raw get_earnings_results payload(s)
    data/damian_financials.json     raw get_financials payload(s)  (optional)

Local dry-run:  python damian.py
"""
import json
import os
import statistics
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "damian.json")


# ---------------------------------------------------------------- loading

def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return json.load(f)


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _results(payload):
    """MCP payloads arrive as {data:{results:[...]}}, a bare list, or a list of payloads."""
    out = []
    if payload is None:
        return out
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            out.extend(payload["data"].get("results") or [])
        elif "results" in payload:
            out.extend(payload.get("results") or [])
        else:
            # {sym: [...]} mapping
            for sym, rows in payload.items():
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict):
                            r.setdefault("symbol", sym)
                            out.append(r)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and ("data" in item or "results" in item):
                out.extend(_results(item))
            elif isinstance(item, dict):
                out.append(item)
    return out


def load_inputs(data_dir=None):
    d = data_dir or os.path.join(HERE, "data")
    prices = _read_json(os.path.join(d, "mcp_prices.json")) or {}

    fundamentals = {}
    for r in _results(_read_json(os.path.join(d, "damian_fundamentals.json"))):
        if r.get("symbol"):
            fundamentals[r["symbol"].upper()] = r

    earnings = {}
    for r in _results(_read_json(os.path.join(d, "damian_earnings.json"))):
        sym = (r.get("symbol") or "").upper()
        if sym:
            earnings.setdefault(sym, []).append(r)
    for sym in earnings:
        earnings[sym].sort(key=lambda r: (r.get("year") or 0, r.get("quarter") or 0))

    financials = {}
    for r in _results(_read_json(os.path.join(d, "damian_financials.json"))):
        sym = (r.get("symbol") or "").upper()
        if sym:
            financials.setdefault(sym, []).append(r)

    scan_hits = load_scan_results(os.path.join(d, "damian_scan_results.json"))

    return {"prices": prices, "fundamentals": fundamentals,
            "earnings": earnings, "financials": financials, "scan_hits": scan_hits}


def load_scan_results(path):
    """Fold saved run_scan payloads into {SYM: {scans:[titles], cols:{...}}}.

    Accepts either a {title: <run_scan payload>} mapping or a bare list of
    payloads. Scanner columns carry a NATIVE Forward P/E and PEG - better than
    anything we can construct from quarterly EPS, so they win in build_rows.
    """
    raw = _read_json(path)
    if not raw:
        return {}

    payloads = []
    if isinstance(raw, dict):
        if "data" in raw or "results" in raw:
            payloads = [raw]
        else:
            for title, p in raw.items():
                if isinstance(p, dict):
                    p.setdefault("_title", title)
                    payloads.append(p)
    elif isinstance(raw, list):
        payloads = [p for p in raw if isinstance(p, dict)]

    hits = {}
    for p in payloads:
        body = p.get("data", {}).get("result", p.get("result", p))
        title = body.get("scan_title") or p.get("_title") or "scan"
        for row in body.get("results", []) or []:
            sym = (row.get("ticker") or "").upper()
            if not sym:
                continue
            rec = hits.setdefault(sym, {"scans": [], "cols": {}})
            if title not in rec["scans"]:
                rec["scans"].append(title)
            for k, v in (row.get("columns") or {}).items():
                rec["cols"].setdefault(k, v)
    return hits


def build_universe(cfg, inputs=None):
    tickers = []
    src = cfg.get("universe_source")

    if src == "scans" and inputs and inputs.get("scan_hits"):
        # Most-confirmed names first: appearing in several independent screens
        # is the strongest single signal Damian has.
        tickers = sorted(inputs["scan_hits"],
                         key=lambda s: (-len(inputs["scan_hits"][s]["scans"]), s))
    elif src == "find_pairs" or not tickers:
        try:
            import find_pairs
            tickers = sorted({t for group in find_pairs.UNIVERSE.values() for t in group})
        except Exception as exc:  # universe is advisory - never fatal
            print("  WARN: could not load find_pairs.UNIVERSE (%s)" % exc)

    tickers += [t for t in cfg.get("extra_tickers", []) if t not in tickers]
    return tickers


# ---------------------------------------------------------------- helpers

def _f(val):
    """Robinhood returns numbers as strings; None-safe float."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _scale(x, lo, hi):
    """Map x from [lo,hi] onto [0,1], clamped."""
    if x is None or hi == lo:
        return None
    return _clamp((x - lo) / (hi - lo))


def closes(prices, sym):
    rows = prices.get(sym) or []
    return [r["close"] for r in rows if r.get("close") is not None]


def pct_change(series, days):
    if not series or len(series) <= days:
        return None
    past = series[-(days + 1)]
    if not past:
        return None
    return series[-1] / past - 1.0


# ---------------------------------------------------------- forward EPS

def blended_forward_eps(rows):
    """Last 3 REPORTED quarters + the next quarter's ESTIMATE.

    Better than 4x the next estimate for seasonal names. Returns
    (eps, next_estimate, next_report_date, n_actuals_used).
    """
    if not rows:
        return None, None, None, 0
    actuals = [(_f(r.get("eps", {}).get("actual")), r) for r in rows]
    actuals = [(v, r) for v, r in actuals if v is not None]
    upcoming = [r for r in rows if _f(r.get("eps", {}).get("actual")) is None
                and _f(r.get("eps", {}).get("estimate")) is not None]

    est = _f(upcoming[0]["eps"]["estimate"]) if upcoming else None
    rpt = (upcoming[0].get("report") or {}).get("date") if upcoming else None

    if est is not None and len(actuals) >= 3:
        return sum(v for v, _ in actuals[-3:]) + est, est, rpt, 3
    if len(actuals) >= 4:                      # no estimate published - use trailing 4
        return sum(v for v, _ in actuals[-4:]), est, rpt, 4
    if est is not None:                        # thin history - annualize the estimate
        return est * 4.0, est, rpt, 0
    return None, est, rpt, 0


def earnings_stats(rows, cfg):
    """Beat rate, mean surprise, YoY EPS growth from the earnings history."""
    ec = cfg["earnings"]
    pairs = [(_f(r.get("eps", {}).get("estimate")), _f(r.get("eps", {}).get("actual")))
             for r in rows]
    pairs = [(e, a) for e, a in pairs if e is not None and a is not None and e != 0]
    if len(pairs) < ec["min_quarters"]:
        return None

    beats = sum(1 for e, a in pairs if a > e)
    beat_rate = beats / len(pairs)
    cap = ec["surprise_cap_pct"]
    surprises = [_clamp((a - e) / abs(e), -cap, cap) for e, a in pairs]
    mean_surprise = statistics.fmean(surprises)

    actuals = [a for _, a in pairs]
    eps_growth = None
    if len(actuals) >= 5 and actuals[-5] not in (None, 0):
        eps_growth = actuals[-1] / abs(actuals[-5]) - 1.0

    return {"n": len(pairs), "beat_rate": beat_rate, "mean_surprise": mean_surprise,
            "eps_growth_yoy": eps_growth,
            "ttm_eps": sum(actuals[-4:]) if len(actuals) >= 4 else None}


def margin_trend(rows):
    """Net-margin change, newest 2 periods vs the 2 before. Positive = expanding."""
    if not rows or len(rows) < 4:
        return None

    def margin(r):
        nm = _f(r.get("net_margin"))
        if nm is not None:
            return nm if abs(nm) <= 1.5 else nm / 100.0
        rev, ni = _f(r.get("revenue")), _f(r.get("net_income"))
        return ni / rev if rev and ni is not None else None

    ms = [m for m in (margin(r) for r in rows) if m is not None]
    if len(ms) < 4:
        return None
    return statistics.fmean(ms[-2:]) - statistics.fmean(ms[-4:-2])


# ---------------------------------------------------------------- scoring

def score_valuation(row, sector_median_fwd_pe, cfg):
    vc = cfg["valuation"]
    fwd_pe = row.get("fwd_pe")
    if fwd_pe is None:
        return None, []
    notes = []

    lo, hi, ideal = vc["fwd_pe_floor"], vc["fwd_pe_cap"], vc["ideal_fwd_pe"]
    # Cheap is good, but a sub-floor P/E is usually a value trap, not a bargain.
    if fwd_pe <= lo:
        abs_score = 0.45
        notes.append("fwd P/E %.1f below floor - possible value trap" % fwd_pe)
    elif fwd_pe <= ideal:
        abs_score = 1.0 - 0.25 * (ideal - fwd_pe) / max(ideal - lo, 1e-9)
    else:
        abs_score = _clamp(1.0 - (fwd_pe - ideal) / max(hi - ideal, 1e-9)) * 0.9

    if sector_median_fwd_pe:
        disc = 1.0 - fwd_pe / sector_median_fwd_pe
        disc_score = _scale(disc, -0.35, 0.45) or 0.5
        if disc > 0.15:
            notes.append("%.0f%% below sector median fwd P/E (%.1f)" % (disc * 100, sector_median_fwd_pe))
        elif disc < -0.20:
            notes.append("%.0f%% ABOVE sector median fwd P/E" % (-disc * 100))
    else:
        disc_score = 0.5

    trail = row.get("trailing_pe")
    if trail and fwd_pe:
        mc = vc["max_compression_credit"]
        compression = _clamp(trail / fwd_pe - 1.0, -mc, mc)
        comp_score = _scale(compression, -mc, mc) or 0.5
        if compression > 0.12:
            notes.append("P/E compressing %.0f%% (trailing %.1f -> fwd %.1f)"
                         % (compression * 100, trail, fwd_pe))
    else:
        comp_score = 0.5

    score = (vc["abs_level_weight"] * abs_score
             + vc["sector_discount_weight"] * disc_score
             + vc["compression_weight"] * comp_score)
    return _clamp(score) * 100, notes


def score_earnings(row, cfg):
    ec = cfg["earnings"]
    st = row.get("earn_stats")
    if not st:
        return None, []
    notes = []

    beat_score = _scale(st["beat_rate"], 0.25, 1.0) or 0.5
    if st["beat_rate"] >= 0.75:
        notes.append("beat %.0f%% of last %d quarters" % (st["beat_rate"] * 100, st["n"]))
    elif st["beat_rate"] <= 0.4:
        notes.append("missed often (%.0f%% beat rate)" % (st["beat_rate"] * 100))

    cap = ec["surprise_cap_pct"]
    surp_score = _scale(st["mean_surprise"], -cap, cap) or 0.5
    if st["mean_surprise"] > 0.05:
        notes.append("avg EPS surprise +%.1f%%" % (st["mean_surprise"] * 100))

    g = st.get("eps_growth_yoy")
    gcap = ec["eps_growth_cap_pct"]
    growth_score = _scale(_clamp(g, -gcap, gcap), -gcap, gcap) if g is not None else 0.5
    if g is not None and g > 0.15:
        notes.append("EPS +%.0f%% YoY" % (g * 100))
    elif g is not None and g < -0.15:
        notes.append("EPS %.0f%% YoY (shrinking)" % (g * 100))

    mt = row.get("margin_trend")
    if mt is not None:
        margin_score = _scale(mt, -0.04, 0.04) or 0.5
        if mt > 0.005:
            notes.append("net margin expanding +%.1fpp" % (mt * 100))
        elif mt < -0.005:
            notes.append("net margin compressing %.1fpp" % (mt * 100))
    else:
        margin_score = 0.5

    score = (ec["beat_rate_weight"] * beat_score
             + ec["surprise_weight"] * surp_score
             + ec["eps_growth_weight"] * growth_score
             + ec["margin_trend_weight"] * margin_score)
    return _clamp(score) * 100, notes


def score_macro(row, sector_trend, cfg):
    mc = cfg["macro"]
    notes = []

    if sector_trend is not None:
        sect_score = _scale(sector_trend, -0.15, 0.20) or 0.5
        if sector_trend > 0.05:
            notes.append("sector +%.1f%% 3mo (tailwind)" % (sector_trend * 100))
        elif sector_trend < -0.05:
            notes.append("sector %.1f%% 3mo (headwind)" % (sector_trend * 100))
    else:
        sect_score = 0.5

    rs = row.get("rel_strength")
    bench = row.get("benchmark", "SPY")
    if rs is not None:
        rs_score = _scale(rs, -0.20, 0.25) or 0.5
        if rs > 0.05:
            notes.append("outperforming %s by %.0f%% 3mo" % (bench, rs * 100))
        elif rs < -0.10:
            notes.append("lagging %s by %.0f%% 3mo" % (bench, -rs * 100))
    else:
        rs_score = 0.5

    # Reward the middle of the 52-week range: not an extended top, not a falling knife.
    pos = row.get("range_position")
    if pos is not None:
        ideal = mc["ideal_range_position"]
        rng_score = _clamp(1.0 - abs(pos - ideal) / 0.55)
        if pos < 0.20:
            notes.append("near 52w low (%.0f%% of range) - confirm it is not still falling" % (pos * 100))
        elif pos > 0.90:
            notes.append("near 52w high (%.0f%% of range) - extended" % (pos * 100))
    else:
        rng_score = 0.5

    score = (mc["sector_trend_weight"] * sect_score
             + mc["rel_strength_weight"] * rs_score
             + mc["range_position_weight"] * rng_score)
    return _clamp(score) * 100, notes


# ---------------------------------------------------------------- pipeline

def _days_until(datestr):
    if not datestr:
        return None
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - date.today()).days


def build_rows(cfg, inputs, universe):
    """Assemble one raw row per ticker with every derived metric (pre-scoring)."""
    prices, fundamentals = inputs["prices"], inputs["fundamentals"]
    earnings, financials = inputs["earnings"], inputs["financials"]
    scan_hits = inputs.get("scan_hits") or {}
    fl, mc = cfg["filters"], cfg["macro"]
    bench = cfg.get("benchmark", "SPY")

    bench_ret = pct_change(closes(prices, bench), mc["trend_lookback_days"])

    rows, skipped = [], []
    for sym in universe:
        fund = fundamentals.get(sym) or {}
        hit = scan_hits.get(sym) or {}
        cols = hit.get("cols") or {}
        if not fund and not cols:
            skipped.append((sym, "no fundamentals"))
            continue

        cap = _f(fund.get("market_cap")) or _f(cols.get("Market cap"))
        vol = (_f(fund.get("average_volume_30_days")) or _f(fund.get("average_volume"))
               or _f(cols.get("Average volume")))
        if cap is not None and cap < fl["min_market_cap"]:
            skipped.append((sym, "market cap below floor"))
            continue
        if vol is not None and vol < fl["min_avg_volume"]:
            skipped.append((sym, "illiquid"))
            continue

        series = closes(prices, sym)
        px = (series[-1] if series else None) or _f(fund.get("last_trade_price")) or _f(cols.get("Last"))
        if not px:
            skipped.append((sym, "no price"))
            continue

        erows = earnings.get(sym, [])
        fwd_eps, next_est, next_date, n_act = blended_forward_eps(erows)

        # The scanner publishes a real forward P/E; prefer it over our
        # constructed one and fall back only when the scan did not cover it.
        native_fwd_pe = _f(cols.get("Forward P/E"))
        derived_fwd_pe = px / fwd_eps if (fwd_eps and fwd_eps > 0) else None
        fwd_pe = native_fwd_pe or derived_fwd_pe
        fwd_pe_source = ("scanner" if native_fwd_pe else
                         "derived" if derived_fwd_pe else "none")

        if fwd_pe is None and fl["require_positive_fwd_eps"] and fwd_eps is not None and fwd_eps <= 0:
            skipped.append((sym, "negative forward EPS"))
            continue

        hi52, lo52 = _f(fund.get("high_52_weeks")), _f(fund.get("low_52_weeks"))
        range_pos = None
        if hi52 and lo52 and hi52 > lo52:
            range_pos = _clamp((px - lo52) / (hi52 - lo52))

        ret_3mo = pct_change(series, mc["trend_lookback_days"])
        rel = (ret_3mo - bench_ret) if (ret_3mo is not None and bench_ret is not None) else None

        basis = ("Robinhood scanner" if fwd_pe_source == "scanner" else
                 "3 actual + 1 est" if n_act == 3 else
                 "trailing 4 actual" if n_act == 4 else
                 "next est x4" if fwd_eps else "n/a")

        rows.append({
            "symbol": sym,
            "name": cols.get("Name") or "",
            "price": px,
            "sector": fund.get("sector") or "Unknown",
            "industry": fund.get("industry") or "",
            "market_cap": cap,
            "trailing_pe": _f(fund.get("pe_ratio")),
            "pb_ratio": _f(fund.get("pb_ratio")),
            "dividend_yield": _f(fund.get("dividend_yield")),
            "peg": _f(cols.get("PEG")),
            "roe": _f(cols.get("Return on equity")),
            "net_margin": _f(cols.get("Net profit margin")),
            "operating_margin": _f(cols.get("Operating margin")),
            "scans": hit.get("scans", []),
            "confluence": len(hit.get("scans", [])),
            "fwd_eps": fwd_eps,
            "fwd_pe": fwd_pe,
            "fwd_pe_source": fwd_pe_source,
            "fwd_eps_basis": basis,
            "next_earnings_date": next_date,
            "days_to_earnings": _days_until(next_date),
            "next_eps_estimate": next_est,
            "earn_stats": earnings_stats(erows, cfg),
            "margin_trend": margin_trend(financials.get(sym, [])),
            "ret_3mo": ret_3mo,
            "rel_strength": rel,
            "benchmark": bench,
            "range_position": range_pos,
        })

    return rows, skipped


def sector_aggregates(rows):
    """Median forward P/E and median 3mo return per sector (the macro backdrop)."""
    by_sector = {}
    for r in rows:
        by_sector.setdefault(r["sector"], []).append(r)

    med_pe, med_trend = {}, {}
    for sector, group in by_sector.items():
        pes = [r["fwd_pe"] for r in group if r.get("fwd_pe")]
        if len(pes) >= 2:
            med_pe[sector] = statistics.median(pes)
        rets = [r["ret_3mo"] for r in group if r.get("ret_3mo") is not None]
        if rets:
            med_trend[sector] = statistics.median(rets)
    return med_pe, med_trend


def score_rows(cfg, rows):
    med_pe, med_trend = sector_aggregates(rows)
    w = cfg["weights"]
    fl = cfg["filters"]

    scored = []
    for r in rows:
        val, vnotes = score_valuation(r, med_pe.get(r["sector"]), cfg)
        earn, enotes = score_earnings(r, cfg)
        mac, mnotes = score_macro(r, med_trend.get(r["sector"]), cfg)
        if val is None and earn is None:
            continue  # not enough fundamental data to have an opinion

        # Renormalise across whichever pillars have data, so a missing pillar
        # neither silently helps nor penalises the name.
        parts = [(val, w["valuation"]), (earn, w["earnings"]), (mac, w["macro"])]
        avail = [(s, wt) for s, wt in parts if s is not None]
        total_w = sum(wt for _, wt in avail)
        composite = sum(s * wt for s, wt in avail) / total_w if total_w else 0.0

        # Confluence: independent screens agreeing is the strongest signal here.
        conf_cfg = cfg.get("confluence") or {}
        extra = max(0, r.get("confluence", 0) - 1)
        conf_bonus = min(extra * conf_cfg.get("bonus_per_extra_scan", 0.0),
                         conf_cfg.get("max_bonus", 0.0))
        composite = _clamp(composite + conf_bonus, 0.0, 100.0)

        flags = []
        dte = r.get("days_to_earnings")
        if dte is not None and 0 <= dte <= fl["max_days_to_earnings_flag"]:
            flags.append("reports in %dd (%s)" % (dte, r["next_earnings_date"]))
        if r.get("fwd_pe") is None:
            flags.append("no forward P/E - valuation pillar skipped")
        if r.get("earn_stats") is None:
            flags.append("thin earnings history")

        out = dict(r)
        out.update({
            "score_valuation": val, "score_earnings": earn, "score_macro": mac,
            "score": composite,
            "confluence_bonus": conf_bonus,
            "sector_median_fwd_pe": med_pe.get(r["sector"]),
            "sector_trend_3mo": med_trend.get(r["sector"]),
            "notes": vnotes + enotes + mnotes,
            "flags": flags,
        })
        scored.append(out)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


# ------------------------------------------------------- stage 1: prescreen

def prescreen(cfg, inputs, universe):
    """Stage 1 - rank the whole universe on CHEAP data only.

    get_earnings_results costs one MCP call per symbol, so scanning 156 names
    that way would blow the routine's context. Fundamentals come 10-per-call and
    prices are already pulled, so we rank on trailing valuation + macro/trend
    first and spend the per-symbol earnings calls only on the survivors.
    """
    pc = cfg["prescreen"]
    rows, skipped = build_rows(cfg, inputs, universe)
    _, med_trend = sector_aggregates(rows)

    # Stage 1 usually runs BEFORE fundamentals are fetched, so trailing P/E is
    # not available yet - the scanner's forward P/E is. Prefer whichever exists,
    # forward first, or this pass skips the whole universe.
    def pe_of(r):
        return r.get("fwd_pe") or r.get("trailing_pe")

    by_sector = {}
    for r in rows:
        v = pe_of(r)
        if v and v > 0:
            by_sector.setdefault(r["sector"], []).append(v)
    med_trail = {s: statistics.median(v) for s, v in by_sector.items() if len(v) >= 2}

    out = []
    for r in rows:
        tpe = pe_of(r)
        if tpe is None or tpe <= 0 or tpe > pc["trailing_pe_cap"]:
            skipped.append((r["symbol"], "no usable P/E, or above cap"))
            continue

        ideal = pc["ideal_trailing_pe"]
        level = (1.0 - 0.25 * (ideal - tpe) / ideal if tpe <= ideal
                 else _clamp(1.0 - (tpe - ideal) / max(pc["trailing_pe_cap"] - ideal, 1e-9)))
        med = med_trail.get(r["sector"])
        disc = _scale(1.0 - tpe / med, -0.35, 0.45) if med else 0.5
        value_score = 0.5 * _clamp(level) + 0.5 * (disc if disc is not None else 0.5)

        macro_score, mnotes = score_macro(r, med_trend.get(r["sector"]), cfg)
        macro_score = (macro_score or 50.0) / 100.0

        composite = (pc["weights"]["value"] * value_score
                     + pc["weights"]["macro"] * macro_score) * 100

        row = dict(r)
        row.update({"prescreen_score": composite,
                    "sector_median_trailing_pe": med,
                    "sector_trend_3mo": med_trend.get(r["sector"]),
                    "notes": mnotes})
        out.append(row)

    out.sort(key=lambda r: r["prescreen_score"], reverse=True)
    return out, skipped


def write_shortlist(cfg, ranked, skipped, base=None):
    pc = cfg["prescreen"]
    root = base or HERE
    shortlist = ranked[:pc["shortlist_n"]]
    path = os.path.join(root, pc["shortlist_path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "asof": date.today().isoformat(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "universe_scored": len(ranked),
        "skipped": len(skipped),
        "symbols": [r["symbol"] for r in shortlist],
        "detail": [{"symbol": r["symbol"], "prescreen_score": round(r["prescreen_score"], 2),
                    "trailing_pe": r.get("trailing_pe"), "sector": r["sector"],
                    "sector_trend_3mo": r.get("sector_trend_3mo"),
                    "rel_strength": r.get("rel_strength"),
                    "range_position": r.get("range_position")} for r in shortlist],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path, payload


# ---------------------------------------------------------------- output

def apply_sector_cap(scored, max_per_sector):
    """Re-order so no sector fills the top of the list.

    A forward-P/E screen concentrates hard: deep cyclicals (memory, energy,
    shipping) go cheap together, so the raw ranking can return one trade
    several times over and read as diversified. Names past the cap keep their
    score but drop below the uncapped ones. Sector "Unknown" is never capped -
    it means fundamentals have not been fetched yet (stage-1 pass), not that
    the names share a business.
    """
    if not max_per_sector or max_per_sector < 1:
        return list(scored), []

    kept, overflow, seen = [], [], {}
    for r in scored:
        sector = r.get("sector") or "Unknown"
        if sector == "Unknown":
            kept.append(r)
            continue
        seen[sector] = seen.get(sector, 0) + 1
        if seen[sector] <= max_per_sector:
            kept.append(r)
        else:
            r = dict(r)
            r.setdefault("flags", []).append(
                "below sector cap - %d higher-scoring %s name(s) already listed"
                % (max_per_sector, sector))
            overflow.append(r)
    return kept + overflow, overflow


def _fmt(v, spec="%.1f", dash="-"):
    return dash if v is None else spec % v


def render_report(cfg, scored, skipped, asof):
    o = cfg["output"]
    capped, demoted = apply_sector_cap(scored, o.get("max_per_sector"))
    top = capped[:o["top_n"]]
    watch = capped[o["top_n"]:o["top_n"] + o["watch_n"]]

    n_native = sum(1 for r in scored if r.get("fwd_pe_source") == "scanner")

    L = []
    L.append("# Damian - stock scan %s" % asof)
    L.append("")
    L.append("Research only. No orders are placed by this scan.")
    L.append("")
    L.append("Scored %d of %d names on valuation (forward P/E), earnings quality, and "
             "macro/trend. Forward P/E is Robinhood's own field for %d of them; for the rest it "
             "is constructed as price / (last 3 reported quarters + next quarter's estimate)."
             % (len(scored), len(scored) + len(skipped), n_native))
    L.append("")

    if demoted:
        L.append("> **Sector cap applied.** At most %d names per sector appear in the ranked "
                 "lists, so one crowded trade cannot fill the report. %d name(s) were pushed "
                 "down despite scoring well: %s. A screen that returns six versions of the same "
                 "bet has found one idea, not six." % (
                     o["max_per_sector"], len(demoted),
                     ", ".join("%s (%s)" % (r["symbol"], r["sector"]) for r in demoted[:8])))
        L.append("")

    L.append("## Top candidates")
    L.append("")
    L.append("| # | Ticker | Score | Screens | Fwd P/E | PEG | Val | Earn | Macro | Sector |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(top, 1):
        L.append("| %d | **%s** | **%.1f** | %d/3 | %s | %s | %s | %s | %s | %s |" % (
            i, r["symbol"], r["score"], r.get("confluence", 0), _fmt(r.get("fwd_pe")),
            _fmt(r.get("peg"), "%.2f"), _fmt(r.get("score_valuation"), "%.0f"),
            _fmt(r.get("score_earnings"), "%.0f"), _fmt(r.get("score_macro"), "%.0f"),
            r["sector"]))
    L.append("")

    L.append("## Why each one scored")
    L.append("")
    for r in top:
        L.append("### %s%s - %.1f  (%s)" % (
            r["symbol"], " - " + r["name"] if r.get("name") else "", r["score"], r["sector"]))
        if r.get("scans"):
            L.append("Surfaced by: %s" % "; ".join(r["scans"]))
            L.append("")
        bits = []
        if r.get("fwd_pe"):
            bits.append("fwd P/E %.1f (%s)" % (r["fwd_pe"], r["fwd_eps_basis"]))
        if r.get("market_cap"):
            bits.append("$%.0fB cap" % (r["market_cap"] / 1e9))
        if r.get("dividend_yield"):
            bits.append("%.2f%% yield" % r["dividend_yield"])
        if bits:
            L.append("*%s*" % " | ".join(bits))
            L.append("")
        for n in r["notes"]:
            L.append("- %s" % n)
        for f in r["flags"]:
            L.append("- **FLAG:** %s" % f)
        L.append("")

    if watch:
        L.append("## Next up (just missed the cut)")
        L.append("")
        L.append("| Ticker | Score | Fwd P/E | Sector | Lead reason |")
        L.append("|---|---|---|---|---|")
        for r in watch:
            L.append("| %s | %.1f | %s | %s | %s |" % (
                r["symbol"], r["score"], _fmt(r.get("fwd_pe")), r["sector"],
                r["notes"][0] if r["notes"] else "-"))
        L.append("")

    _, med_trend = sector_aggregates(scored)
    if med_trend:
        L.append("## Macro backdrop (3mo median return by sector)")
        L.append("")
        ranked = sorted(med_trend.items(), key=lambda kv: kv[1], reverse=True)
        L.append("| Sector | 3mo | |")
        L.append("|---|---|---|")
        for sector, t in ranked:
            L.append("| %s | %+.1f%% | %s |" % (sector, t * 100,
                     "tailwind" if t > 0.05 else "headwind" if t < -0.05 else "neutral"))
        L.append("")

    if skipped:
        L.append("## Skipped (%d)" % len(skipped))
        L.append("")
        reasons = {}
        for sym, why in skipped:
            reasons.setdefault(why, []).append(sym)
        for why, syms in sorted(reasons.items()):
            L.append("- **%s** (%d): %s" % (why, len(syms), ", ".join(sorted(syms)[:20])))
        L.append("")

    L.append("---")
    L.append("")
    L.append("Damian is a screen, not a recommendation. Scores rank relative attractiveness "
             "inside this universe on this date; they say nothing about absolute value or "
             "downside. Check the news and the filings before acting on any name.")
    return "\n".join(L)


def write_outputs(cfg, scored, skipped, asof, base=None):
    o = cfg["output"]
    root = base or HERE
    paths = {}

    report_path = os.path.join(root, o["report_path"])
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render_report(cfg, scored, skipped, asof))
    paths["report"] = report_path

    # Same order the report shows, so picks.json and the report never disagree.
    capped, _ = apply_sector_cap(scored, o.get("max_per_sector"))

    keep = ("symbol", "name", "confluence", "scans", "peg", "roe", "fwd_pe_source", "score", "score_valuation", "score_earnings", "score_macro",
            "price", "sector", "industry", "fwd_pe", "fwd_eps", "fwd_eps_basis",
            "trailing_pe", "sector_median_fwd_pe", "sector_trend_3mo", "market_cap",
            "next_earnings_date", "days_to_earnings", "rel_strength", "range_position",
            "notes", "flags")
    picks = [{k: r.get(k) for k in keep} for r in capped[:o["top_n"] + o["watch_n"]]]
    picks_path = os.path.join(root, o["picks_path"])
    with open(picks_path, "w", encoding="utf-8") as f:
        json.dump({"asof": asof, "generated": datetime.now().isoformat(timespec="seconds"),
                   "scored": len(scored), "skipped": len(skipped), "picks": picks}, f, indent=2)
    paths["picks"] = picks_path

    # Append-only history, same convention as data/bot_journal.jsonl.
    journal_path = os.path.join(root, o["journal_path"])
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "asof": asof,
            "scored": len(scored),
            "top": [{"symbol": r["symbol"], "score": round(r["score"], 2),
                     "fwd_pe": r.get("fwd_pe"), "sector": r.get("sector")}
                    for r in capped[:o["top_n"]]],
        }) + "\n")
    paths["journal"] = journal_path

    return paths


def run(cfg=None, data_dir=None, base=None):
    cfg = cfg or load_config()
    inputs = load_inputs(data_dir)
    universe = build_universe(cfg, inputs)

    # Stage 1: rank on cheap data and publish the shortlist the routine uses to
    # decide which names are worth the per-symbol earnings calls in stage 2.
    if not inputs.get("fundamentals"):
        ranked, pre_skipped = prescreen(cfg, inputs, universe)
        write_shortlist(cfg, ranked, pre_skipped, base=base)

    rows, skipped = build_rows(cfg, inputs, universe)
    scored = score_rows(cfg, rows)
    asof = date.today().isoformat()
    paths = write_outputs(cfg, scored, skipped, asof, base=base)
    return scored, skipped, paths


if __name__ == "__main__":
    cfg = load_config()
    inputs = load_inputs()
    universe = build_universe(cfg, inputs)
    print("Damian - research-only scan")
    print("  universe: %d tickers" % len(universe))
    print("  prices:       %d tickers" % len(inputs["prices"]))
    print("  fundamentals: %d tickers" % len(inputs["fundamentals"]))
    print("  earnings:     %d tickers" % len(inputs["earnings"]))
    print("  financials:   %d tickers" % len(inputs["financials"]))

    print("  scan hits:    %d tickers" % len(inputs["scan_hits"]))

    if not inputs["fundamentals"] and not inputs["scan_hits"]:
        print("\n  No data found. Damian needs the routine to write at least:")
        print("    data/damian_scan_results.json  (run_scan - market-wide discovery)")
        print("  and, for the deep-dive pass:")
        print("    data/damian_fundamentals.json  (get_equity_fundamentals)")
        print("    data/damian_earnings.json      (get_earnings_results)")
        print("    data/damian_financials.json    (get_financials, optional)")
        raise SystemExit(0)

    if not inputs["fundamentals"]:
        print("\n  Scan-only pass: valuation uses the scanner's native Forward P/E;")
        print("  the earnings pillar stays dark until stage 2 fetches per-name data.")

    scored, skipped, paths = run(cfg)
    print("\n  scored %d, skipped %d" % (len(scored), len(skipped)))
    print("\n  %-7s %6s %8s %6s %6s %6s  %s" % ("TICKER", "SCORE", "FWD_PE", "VAL", "EARN", "MACRO", "SECTOR"))
    for r in scored[:cfg["output"]["top_n"]]:
        print("  %-7s %6.1f %8s %6s %6s %6s  %s" % (
            r["symbol"], r["score"], _fmt(r.get("fwd_pe")),
            _fmt(r.get("score_valuation"), "%.0f"), _fmt(r.get("score_earnings"), "%.0f"),
            _fmt(r.get("score_macro"), "%.0f"), r["sector"]))
    print("\n  wrote %s" % paths["report"])
