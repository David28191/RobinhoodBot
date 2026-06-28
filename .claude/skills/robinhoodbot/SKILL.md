---
name: robinhoodbot
description: Operating manual + control surface for the RobinhoodBot autonomous trading system (SPY accumulator + stat-arb pairs + QQQ swing) running on the Robinhood Agentic MCP via scheduled cloud routines. Use when adding/removing pairs, adjusting sizing or allocation, running backtests, managing the cloud routines (enable/disable/run), reading the scout or trade-review reports, or diagnosing the bot. This is the manual for HOW to change the bot safely — the trading itself runs in the cloud, not in this skill.
---

# RobinhoodBot — operating manual

A small, real-money autonomous trader on Robinhood's **Agentic** account `596618249`
(~$120, +$10/week deposits). Three strategies run unattended via **cloud routines**
that pull data from the Robinhood MCP, decide with `cloud_decide.py`, and place real
orders — your PC can be off. This skill is the manual for operating and improving it.

> **Repo:** https://github.com/David28191/RobinhoodBot (public; **no secrets/state committed**)
> **Account:** Agentic cash account `596618249` ONLY (the only `agentic_allowed` account).
> Other accounts (margin `5QZ75881`, Roth `475671004`) are **off-limits** to the agent.

## Golden rules (never violate)
1. **Trade only account `596618249`.** Never any other account.
2. **Never wire a strategy live without (a) a backtest and (b) a dry run.**
3. **Equities only, long-only** (cash account: no shorts, no options). Pairs trade the cheap leg only.
4. **Sizing is small and capped.** Per-run cap = 25% of account value, hard ceiling $150.
5. **The code repo is read-only to the routines.** Never let an unattended session commit/push.
6. **Robinhood is the source of truth** for positions/cash. Treat Google Drive state as a convenience, not gospel.
7. Changes to live behavior land by **editing configs + `git push`** — the live routine clones latest each run.

---

## The three strategies (exact sizing)

Bankroll = **real account value** (so weekly deposits + gains grow every sleeve). Split in
`spy_accumulate.json` → `_bot_allocation` (read via `allocation.py`):

| Sleeve | % | ~$ at $120 | What it does |
|---|---|---|---|
| SPY accumulator | 55% | ~$66 | buy-and-hold core + opportunistic dips |
| Pairs | 40% | ~$48 | market-neutral-ish stat-arb (long the cheap leg) |
| QQQ swing | 5% | ~$6 | round-trip fade of weekly dips |

**1. SPY accumulator** (`spy_accumulate.json`, logic in `live_spy.py`):
- Weekly **base buy $5** → protected core, never sold
- **Dip ladder**: −1.5σ→$15, −2.5σ→$25, −3.5σ→$35 (5-day cooldown)
- **Trim 5%** of the *dip-sleeve only* at +2.5σ (core untouched)

**2. Pairs** (`pairs.json`, logic in `live.py` / `pairbot.py`):
- Traded pairs: C/GS, JPM/WFC, XOM/CVX, V/MA, BSOL/IBIT
- **Open BUY $15** of the cheap leg when |z| ≥ 2.0; close on revert (≤0.5) / stop (3.5) / 90-day time-stop
- Max **5** open positions; z = rolling-`lookback`(120) ratio z-score

**3. QQQ swing** (`swing.json`, logic in `spy_wtd.py::swing_live_decide`):
- Fade: **BUY $6** (the swing sleeve $) when QQQ weekly z ≤ −1.0; SELL the whole position on
  revert (|z|≤0.5) / stop (3.5) / 40-day time-stop. **Anchor frozen at entry.**
- One round-trip at a time. Backtest (10y, $1k scale): +$550, 71% win, Sharpe 0.46
  (chosen over SPY-swing, which is redundant with the accumulator).

---

## Files (the map)

| File | Role |
|---|---|
| `cloud_decide.py` | **The brain.** Reads MCP prices + state, runs all 3 strategies, emits `data/intended_orders.json` + `data/updated_state.json`. Data-agnostic (no yfinance) so it runs in the cloud. |
| `live.py` / `live_spy.py` | Pure `decide()` for pairs / accumulator (reused by the brain; also paper-run locally). |
| `spy_wtd.py` | Swing engine: `weekly_frame`, `backtest`, `swing_live_decide`. |
| `spy_accumulate.py` | Accumulator engine + signals + `_bot_allocation`. |
| `pairbot.py` | Pair engine: `fetch_prices` (yfinance, **local only**), `compute_spread`, `backtest_pair`. |
| `allocation.py` | Single source of truth for the 55/40/5 split; `load_allocation(bankroll)` sizes off real account value. |
| `find_pairs.py` | Discovery universe (28 sectors / 156 tickers) + cointegration math (ADF, half-life). |
| `scout_pairs.py` | Weekly pair-discovery report (cloud-runnable); flags ADD-candidates + marks traded. |
| `review_trades.py` | Hindsight scorecard of real fills (return, MAE/MFE, entry timing, vs-SPY). |
| `screen_value.py` | S&P 500 buy-low value screener (separate research tool). |
| Configs | `pairs.json`, `spy_accumulate.json`, `swing.json`, `spy.json` |
| Local-only | `run.py`, `dashboard.html`, `optimize*.py` (dashboards/backtests on your PC) |

---

## The cloud routines (manage via `RemoteTrigger` / claude.ai/code/routines)

| Routine | ID | Schedule | Does |
|---|---|---|---|
| **Cloud Brain (LIVE)** | `trig_01Y4CUVbxd9P3SkjXZYp5bQu` | Mon–Fri 9:40am ET | Places real trades. **DISABLED until state-persistence is robust.** |
| Cloud Brain (DRY RUN) | `trig_01D42gNNUFWG3Ykw1CH5qafs` | Mon–Fri 9:40am ET | Decides + notifies, places nothing. Disabled (use to verify before re-enabling LIVE). |
| Pair Scout | `trig_01RF4emscfykPKgba1Adcjaj` | Mon 8:05am ET | Top pair candidates → push + Drive. |
| Trade Review | `trig_01T669GazXXPoWNijRGeowGw` | Fri 5:08pm ET | Hindsight scorecard → push + Drive. |

All clone the repo, `pip install pandas numpy yfinance plotly`, pull data from the
Robinhood MCP, and have the **Robinhood-trading** + **Google-Drive** connectors attached.

**Kill switches:** disable the routine at claude.ai/code/routines/`<id>`, OR disconnect the
Robinhood agent in the Robinhood app, OR ask Claude to disable it.

---

## How it trades unattended (the data + state flow)
1. Cloud routine clones the repo (read-only) and installs deps.
2. Loads prior **state** from Google Drive file `robinhood_live_state.json`.
3. Pulls **prices** via Robinhood `get_equity_historicals` (NOT yfinance — sandbox blocks Yahoo)
   for SPY + QQQ + pair tickers → `data/mcp_prices.json`.
4. Reads live **cash + account_value + positions** via `get_portfolio` / `get_equity_positions`.
5. Runs `python cloud_decide.py` → `data/intended_orders.json` (+ `updated_state.json`).
6. For each intended order: `review_equity_order` → `place_equity_order` (fresh `ref_id`).
7. Saves updated state back to Drive; pushes a notification.

**Caps enforced:** `cloud_decide` drops buys exceeding `min(cash, 25%-of-account)`; the live
routine adds a $150 absolute backstop and a **SPY base-buy guard** (checks order history so a
stale state can't double-buy).

---

## Common workflows

**Add a pair (the expansion loop):** read the weekly Scout report's **ADD-CANDIDATE** list
(cointegrated + profitable + not yet traded). To add: append `{ "a": "X", "b": "Y" }` to
`pairs.json` → `pairs`, `git push`. The live routine trades it next run. (Also add the tickers
to a `find_pairs.UNIVERSE` group if not already there so the scout keeps watching them.)

**Adjust a sleeve size or %:** edit `spy_accumulate.json` → `_bot_allocation`
(`spy_accumulate_pct` / `pairs_pct` / `swing_pct`) and/or the per-strategy dollar knobs
(`base_buy_dollars`, `dip_ladder`, `capital_per_leg`, swing `capital`), then `git push`.
Bankroll auto-tracks real account value, so deposits grow everything — don't hardcode totals.

**Backtest before changing anything live:**
- Pairs: `python find_pairs.py` (discovery) or `pairbot.backtest_pair`.
- Accumulator: `python spy_accumulate.py` / `optimize_accumulate.py`.
- Swing: `python spy_wtd.py` (compares variants; tune in `swing.json`/`spy.json`).

**Enable / disable / run a routine:** `RemoteTrigger {action:"update", trigger_id, body:{enabled:true|false}}`
or `{action:"run", trigger_id}`. Always **verify with the DRY RUN** before enabling LIVE.

**Read the reports:** Scout = what to add. Trade Review = how trades did (entry timing,
vs-SPY). Improvement = (signal from reports) + (this skill's rules for changing safely).

---

## Known gotchas (hard-won — read before debugging)
- **Cloud sandbox blocks yfinance/Yahoo.** Cloud code must get prices from the Robinhood MCP
  (`get_equity_historicals`), never yfinance. `cloud_decide.py`/`scout_pairs.py` are built for this.
- **`get_equity_historicals` payloads are huge.** In a routine, save raw to a file and extract
  `{date, close}` with a script — never read the full payload into context.
- **OAuth connector tokens EXPIRE** (Google Drive seen expiring → silent skipped upload). **This is
  the real limit on "computer-off."** It applies to the **Robinhood** connector too — if it lapses,
  the live trader silently can't trade. Routines should detect connector failures and push-notify;
  reauthorize at claude.ai/customize/connectors.
- **The repo is read-only to routines.** Cloud sessions may try to `git push` working changes and
  fail — keep routines read-only (no commit/push); don't grant write to unattended sessions.
- **Don't rely on Drive for trade correctness.** Prefer reconstructing critical state (open
  positions, weekly base-buy done?) from Robinhood `get_equity_positions` + `get_equity_orders`.
- **Fractional/dollar orders are market + regular-hours only**, ≤6 decimals, no fractional shorts;
  placed after-hours they queue to the next open.
- **Account number is not committed** to the repo (read from `live_state.json` / `$ROBINHOOD_ACCOUNT`).

## Status / open items (update as we go)
- LIVE trader **paused** pending: Drive token reauth + read-only routine fix + Robinhood-as-source-of-truth state.
- Email delivery from routines unverified — use **push + Google Drive**; Gmail connector for real inbox email.
- Swing sleeve is intentionally tiny ($6); grows with the account.
