# Changelog — RobinhoodBot

Notable changes to the autonomous trading bot. Newest first.
(Account: Agentic cash `••••596618249`, ~$120, +$10/week deposits.)

## 2026-07-01 — Swing sleeve unwedged (Robinhood-as-source-of-truth guard)
### Fixed
- **QQQ swing never traded** — root cause: on the first live run (6/29) the routine placed the SPY + IBIT orders but never placed the intended QQQ buy, then persisted the *optimistic* state (which assumes fills) to Drive. Every run since read `swing.open=true` → "holding" → no action, and a phantom SELL was queued to fail on reversion.
- **`decide_swing` now reconciles state against REAL shares** (Robinhood is the source of truth): state says open but account holds no QQQ → reset to flat (logged) and trade normally; account holds QQQ the state doesn't track → block OPEN (no double-buy) until reconciled. The reset persists via `updated_state.json`.
### Known follow-ups
- Routine should persist state only **after confirming each `place_equity_order` succeeded** (the optimistic-state design is what let one missed order poison the ledger).
- Drive has **duplicate `robinhood_live_state.json` files** (connector can only create, not update) — routine must load the newest; stale older copies should be trashed manually.

## 2026-06-29 — Allocation tweak + dashboard scope
### Changed
- Allocation **50 / 40 / 10** (accumulator / pairs / QQQ swing) — swing 5%→10% (~$12), taken from the accumulator (55%→50%). Pushed; live brain picks it up next run.
### Added
- **Dashboard "Pair-finder scope" panel** (Pairs tab): universe size, candidate pairs, tickers with data, passed-correlation, cointegrated count, currently trading.
- **"Changes since last update"** on the dashboard — diffs vs the prior run: tickers that dropped out (no data), pairs that gained/lost cointegration, signals that came/went. Persisted via `data/scope_prev.json` (stored in Drive by the daily routine for day-over-day diffs).
- Fuller **ACTIONABLE-NOW** detail in the scout + dashboard: which leg to BUY, sector (3mo + vs-200d trend), cointegration ✓spring/weak + ADF, correlation, half-life, backtest win%.

## 2026-06-28 — Went LIVE; QQQ swing; dashboards; macro awareness
### Added — computer-off autonomy
- **`cloud_decide.py`** — the data-agnostic decision brain (runs all strategies from Robinhood-MCP prices, no yfinance) so it works in the cloud. Emits `data/intended_orders.json` + `data/updated_state.json`.
- **GitHub repo** `David28191/RobinhoodBot` (public, no secrets/state) — cloud routines clone it.
- **Cloud routines** (Anthropic-hosted, PC-off): LIVE trader (`trig_01Y4…`), Pair Scout (`trig_01RF…`, Mon), Trade Review (`trig_01T6…`, Fri), Dashboard Refresh (`trig_019h…`, daily), DRY RUN (`trig_01D4…`, disabled).
- **State persistence** via Google Drive `robinhood_live_state.json` (each run is a fresh cloud session); **SPY base-buy guard** (checks Robinhood order history) as belt-and-suspenders against stale state.
### Added — strategies & research
- **QQQ swing sleeve** (`swing.json`, `spy_wtd.swing_live_decide`) — fade weekly QQQ dips, round-trip, frozen anchor. Chosen over SPY-swing (which is redundant with the accumulator's buy-dip/trim). Backtest 10y: +$550, 71% win, Sharpe 0.46 (vs SPY 0.14).
- **3-way allocation + deposit-aware bankroll** — `allocation.py` sizes off *real account value* (so $10/wk deposits + gains grow every sleeve); per-run cap = **25% of account** (hard ceiling $150).
- **`scout_pairs.py`** — weekly pair discovery (cloud-runnable) with ADD-candidates + per-pair **sector macro trend** (recent 3mo + structural vs-200d), flags long-into-a-falling-sector. Universe widened to **28 sectors / 156 tickers / 388 pairs**.
- **`review_trades.py`** — hindsight trade scorecard (return, MAE/MFE, entry timing, vs-SPY).
- **`build_dashboard.py`** — unified tabbed `bot_dashboard.html` (Overview + 3 strategy tabs).
- **`screen_value.py`** — S&P 500 buy-low value screener.
- **`robinhoodbot` skill** (`.claude/skills/robinhoodbot/SKILL.md`) — operating manual.
### Changed
- **Went LIVE** — enabled the live trader (first real autonomous run Mon 2026-06-29 9:40am ET).
- SPY accumulator weekly base buy **$25 → $5** (accumulate cash for pairs); dip ladder scaled to the small account (15/25/35).
- Bankroll **$1,000 paper → real account value**; allocation 60/40 → 55/40/5 → 50/40/10.
### Fixed
- **Google Drive token expiry** — root cause of silent skipped uploads; refreshed by revoking at Google + reconnecting. Drive read+write confirmed.
- **Read-only repo** — `.gitignore` now excludes all routine outputs (`data/*.json,*.csv,*.txt,*.html`) so routines can't push code changes (one had pushed a report file).
- UTF-8 report write (Windows cp1252 choked on `⚠`); robust datetime index in `cloud_decide`.

## 2026-06-27 — Reality-check + sizing
### Changed
- Reconciled the real account; sized everything to the actual ~$75 cash (not the $1,000 paper bankroll). Confirmed Agentic account `596618249` is the only `agentic_allowed` account; equities-only, long-only (no shorts/options).
### Verified
- Robinhood MCP reachable from a scheduled cloud session (read-only `get_portfolio` probe); MCP `get_equity_historicals` can supply all needed price history (replacing the cloud-blocked yfinance).

---
_See `.claude/skills/robinhoodbot/SKILL.md` for the current operating manual (sizing, routines, safety rails, gotchas, workflows)._
