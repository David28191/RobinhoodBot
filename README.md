# RobinhoodBot — Pair-Trading Backtester & Dashboard

A local tool that backtests **pair-trading** strategies on stock pairs *you* choose,
and builds an `.html` dashboard showing completed trades and a live watchlist.

**Right now this is Phase 1/2: research only. It does NOT place real trades.**
(Real trading via Robinhood's Agentic Trading MCP is Phase 3 — see Desktop/Robinhood.md.)

---

## How to use it

1. Open a terminal **in this folder** (`Desktop\RobinhoodBot`).
2. Run:
   ```
   python run.py
   ```
3. Your browser opens `dashboard.html`. That's it. Run it again any time to refresh
   with the latest market data.

---

## Editing what it trades — just edit `pairs.json`

```json
{
  "defaults": {
    "lookback": 60,          // days used to compute the "normal" spread
    "entry_z": 2.0,          // open a trade when z-score crosses this (±)
    "exit_z": 0.5,           // close when z-score reverts inside this (±)
    "stop_z": 3.5,           // bail out (loss) if z-score blows past this (±)
    "capital_per_leg": 1000  // dollars per side of each trade
  },
  "history_period": "2y",    // how much history to test ("1y","2y","5y","max")

  "pairs":     [ {"a":"KO","b":"PEP"}, {"a":"V","b":"MA"} ],   // backtested + charted
  "watchlist": [ {"a":"KO","b":"PEP"}, {"a":"HD","b":"LOW"} ]  // current signal only
}
```

- **`pairs`** = fully backtested, charted, and shown in the trades table.
- **`watchlist`** = just shows the *current* z-score + signal (no backtest needed).
- You can override any default on a single pair, e.g. `{"a":"KO","b":"PEP","entry_z":2.5}`.
- Pick stocks that genuinely move together (same industry): KO/PEP, V/MA, XOM/CVX, HD/LOW.

---

## What the dashboard shows

- **Summary** — realized P&L, open P&L, win rate, average trade, best/worst.
- **Watchlist** — for each watched pair: current price ratio, z-score, and the signal
  ("SHORT KO / LONG PEP", "no signal (wait)", etc.).
- **Equity curve** — cumulative P&L of all backtested pairs over time.
- **Per-pair detail** — normalized prices + z-score chart with entry/exit bands and trade markers.
- **Completed trades** — every simulated trade: entry/exit dates, z-scores, P&L, result.

A copy of the trade log is also saved to `data/trades.csv` (open it in Excel if you like).

---

## How the strategy works (plain English)

For a pair A/B, we track the **ratio** A÷B. The **z-score** says how far today's ratio is
from its recent average. When the ratio stretches unusually far (|z| ≥ `entry_z`), we bet it
snaps back: short whichever side is rich, long the cheap side. When it reverts (|z| ≤ `exit_z`)
we close and bank the difference. `stop_z` caps the loss if it keeps diverging instead.

---

## Files

| File | What it is |
|------|------------|
| `pairs.json`     | **You edit this.** Your pairs, watchlist, thresholds. |
| `run.py`         | **You run this.** Builds + opens the dashboard. |
| `pairbot.py`     | The engine. You don't need to touch it. |
| `dashboard.html` | The generated dashboard (overwritten each run). |
| `data/trades.csv`| Saved trade log. |

## Requirements
Python 3 with: `pandas numpy plotly yfinance` (already installed). Needs internet
(for Yahoo Finance data + the chart library).
