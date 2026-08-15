# Changelog — RobinhoodBot

Notable changes to the autonomous trading bot. Newest first.
(Account: Agentic cash `••••596618249`, ~$120, +$10/week deposits.)

## 2026-08-15 — TANK accumulator sleeve REPLACES the retired SPY weekly-DCA
### Added — TANK sleeve (active: buy the tank, sell the bounce, melt-ice-cube backstop)
- New `tank.py` + `tank.json`. Holds a **core** (SPY/QQQ, never sold); strikes with dry powder when a
  **watch** name has fallen a lot — drawdown-from-20d-high ladder (−5%→1u, −8%→2u, −12%→3u, −18%→4u)
  plus a deep **RSI(2)<10 → +1u** kicker — laddering bigger the harder it falls (`unit_dollars` $3.50).
  **Trims** the opportunistic tranche on the bounce (RSI2>70 or close>5DMA); the core is never trimmed.
  **Melt-ice-cube backstop:** if nothing tanks for `backstop_days` (15), deploy `backstop_dollars`
  into the core so cash never rots. Long-only, dollar-sized, fractional, `max_names` (4) concentration cap.
- Wired into `cloud_decide.py`: `decide_spy` **removed** (SPY weekly-DCA retired); `tank.decide` runs
  in its place, sized off `allocation.tank_budget`. `to_broker_orders` emits tank buys (dollar) / trims
  (share qty, **capped to REAL shares**). Tank ledger reconciled vs real shares each run + persisted.
### Changed — allocation → 40/35/25 (TANK / pairs / swing)
- `_bot_allocation`: `spy_accumulate_pct` retired → `tank_pct: 40`, `pairs_pct: 35`, `swing_pct: 25`.
  `allocation.py` returns `tank_budget` (folds a legacy `spy_accumulate_pct` into tank for back-compat).
- **Why:** the SPY weekly-DCA added ~zero value vs a plain recurring buy (10y backtest: dip-timing
  lagged DCA), and "buy every Monday" is not an active-trader strategy. That 40% now trades actively.
### Verified
- Full-brain local dry-run (real snapshot: $224 value / $114 cash / held SPY,MA,QQQ,C) → 4 sane tank
  buys (COIN 3u, AVGO 3u @ RSI2=5, META/GOOGL 2u; AMZN skipped at max_names), $35, within the $56 cap.
### TODO before full activation
- Add the tank `watch` list to the LIVE routines' `get_equity_historicals` fetch (they pull
  SPY+QQQ+pair tickers only today) so the whole watchlist gets prices. Until then tank is active on
  **SPY/QQQ only**. Per Golden Rule #2, run the **DRY-RUN routine** once to confirm in-cloud before LIVE.

## 2026-07-16 — Pairs: base + z-ladder ROTATION (long-only pairs are now a real pair trade)
### Changed — long_only pairs no longer exit to cash; they rotate + ladder
- **Root cause of the "sold IBIT, never bought" report:** `long_only` pairs ran an
  open-at-±entry / **close-to-cash** mean-reversion (`live.decide`), so a CLOSE was a lone
  SELL. That isn't a pair trade. Replaced with a **continuous rotation**: always hold the
  relatively-cheap leg, and at the OPPOSITE extreme **BUY the other leg then SELL the one we
  hold** (buy-first, so a failed/underfunded buy can never leave a naked half-close).
- **Base + z-ladder sizing around the held leg** (mirrors the QQQ swing sleeve): hold a
  **base** (`capital_per_leg`), **ADD** fixed `step_dollars` steps as |z| diverges past
  `entry_z + step_offsets` (up to `n_steps`), **TRIM** back as it converges (partial sell of
  the trade tranche, never the base), rotate the whole book at the flip. `cooldown_days` gates
  add/trim. **FIXED dollar steps** (not scaled to the account); the 40%-of-account
  `pairs_budget` cap still gates OPEN/ADD and grows with deposits. New `pairs.json` keys:
  `n_steps, step_dollars, step_offsets, cooldown_days`.
- New logic: `pairbot.ladder_target_steps` + `_backtest_pair_ladder` (dashboard/backtest now
  model the ladder); `live.decide` emits OPEN/ADD/TRIM/SWAP with a `steps` ledger;
  `cloud_decide` sizes TRIM as a fraction of REAL shares, keeps SWAP legs atomic via a shared
  `group` id in `cash_guard`, and **reconciles the pairs ledger against real shares** (drops a
  phantom leg so it re-enters instead of wedging). BSOL/IBIT keeps `entry_z: 2.5` (wider band).
- Backtest (2y, $15 base/$10 step, 6 pairs): ladder **$47.95** realized+open (66% win) vs flat
  rotation $43.11 vs old exit-to-cash $26.64. Classic long/short pairs are unchanged.

## 2026-07-09 — QQQ swing redesign + rolling anchor (swing & accumulator) + allocation + journal
### Changed — QQQ swing = "base + trade around it"
- Replaced the flat↔fully-in round-trip fade with an **always-net-long base+ladder**. Establishes a
  permanent **base long = `base_pct` (25%) of the sleeve, NEVER sold** (a protected floor), then
  trades the rest AROUND it via a **z-banded exposure ladder** so step size scales with the move:
  `add_ladder −1σ→1 / −2σ→2 steps`, `trim_ladder +1σ→1 / +2σ→0`, with a **±1σ dead-band** (small
  moves do nothing, a big move jumps straight to full/base). `n_steps=2`, `cooldown_days=3`. New
  logic: `spy_wtd.swing_ladder_decide` / `apply_swing_ladder` / `backtest_swing_ladder` /
  `_ladder_target`; `swing.json` reshaped. `cloud_decide.decide_swing` rewritten for the new ledger
  `{base_shares, trade_shares, trade_basis, steps, base_established}` + reconciliation (adopts
  untracked real QQQ as the base; resets a stale base). **TRIM sells a specific share quantity, never
  `sell_full_position`** — a trim can't dump the protected base.
### Changed — rolling-mean anchor (swing AND accumulator)
- New **`spy_wtd.rolling_frame` / `swing_frame`**: anchor = trailing **10-day** mean (continuous),
  `z = ln(price/mean)/rolling_std`, instead of the weekly-reset anchor (prior-Friday close). A price
  that keeps falling stays below its mean, so the ladder keeps buying a **multi-week decline** instead
  of re-baselining every Monday. Swing uses it via `anchor: "rolling_weekly_mean"` in `swing.json`.
- **SPY accumulator switched to the same rolling signal** (`spy_accumulate.py::rolling_signal`,
  `strategies[0].signal="rolling"`, `rolling_ma_days=10`). Backtest (2y SPY): the old `weekly` signal
  fired only **1 dip buy / 0 trims** in 2 years (nearly inert); rolling → **4 buys / 2 trims**, similar
  ROI (~18%). Recalibrated because rolling z is a different scale (`|z|>1` ~25% of days vs ~12%).
### Changed — allocation 50/40/10 → 40/40/20
- QQQ swing **10%→20%** (from the accumulator 50%→40%), so trades are ~$10 not ~$2 (base ~$8, steps
  ~$12 at the real ~$158 account). Pairs unchanged at 40%. Existing positions untouched; only future
  sizing changes. Swing backtest at $24: **+$3.97, max DD $2.10, 3 adds/2 trims** (still trails
  buy-hold by design — small base).
### Added — recording
- **Append-only run journal** `data/bot_journal.jsonl` — `cloud_decide` appends one JSON line per run
  (timestamp, as-of, bankroll, cash before/after, decision notes, intended orders w/ `ref_id`),
  never overwritten. **Drive round-trip WIRED same day:** both LIVE routines (`trig_01Y4…` 9:40am,
  `trig_013D…` 3:45pm) now download `robinhood_bot_journal.jsonl` from Drive → `data/bot_journal.jsonl`
  (step 2b) and upload it back on the no-orders (step 6) and success (step 8) paths — so the history
  accumulates across cloud runs. First Drive copy appears after the next LIVE run. Optional next:
  append CONFIRMED `place_equity_order` fills (order id), not just intent.

## 2026-07-01 — Swing sleeve unwedged (Robinhood-as-source-of-truth guard)
### Added
- **Second daily LIVE run at 3:45pm ET** (`trig_013DVtkwfVswcdxdQ59QWEqC`, Mon–Fri) — same brain, same rails, notifications prefixed `RH LIVE PM`. Lets the swing/pairs react intra-day before the close instead of only at 9:40am. (Cron is UTC: 19:45Z = 3:45pm EDT; in winter EST it drifts to 2:45pm — still inside regular hours, fine for fractional market orders.)
- Both LIVE routine prompts now say: if Drive has **duplicate `robinhood_live_state.json`** files, load the **most recently modified** (the Drive connector can only create, not update, so duplicates accumulate).
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
