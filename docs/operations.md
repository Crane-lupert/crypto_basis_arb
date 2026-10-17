# Paper-live operations runbook

Scope: `src/basis_arb/live/` (strategy_spec.md section 9). The daemon trades **paper fills on live
Binance quotes**, or sends orders to the **Binance testnet**. Nothing in this repository can send an
order to a real-money endpoint; promotion to real capital is a separate decision outside this codebase.
Paper results are presented as paper, testnet fills as testnet (strategy_spec.md section 10).

## 1. Guarantees

| Guarantee | How it is enforced |
|---|---|
| Research/live parity | The decision path is `basis_arb.model.signals.build_targets`, the same function the backtest uses, run on the cached 1h panel with one live row appended (`feed.append_live_row`). The daemon only reads the LAST row's `target`/`contract` per strategy. `tests/test_live_daemon.py` asserts `daemon.model_signals is basis_arb.model.signals` and that the signal ran on the panel + live row. |
| No mainnet trading host | `live/broker.py` contains only `https://testnet.binancefuture.com` (futures) and `https://testnet.binance.vision` (spot). The constructor takes no base URL and every signed request passes `_require_testnet()`. `tests/test_live_broker.py::test_broker_source_contains_no_mainnet_host` reads the source and asserts that neither `api.binance.com`, `fapi.binance.com` nor any `binance.com` host appears, and that the only URL hosts are the two testnets. Mainnet hosts appear only in `live/history.py` / `live/feed.py`, which issue keyless public GETs. |
| No secrets in files | Testnet keys are read from the environment only (`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`). They are never logged, never written to `state/`, and broker error messages and `repr()` never contain them. Do not create a `.env` in the repo. |
| One daemon per book | `state/{ROOT}/daemon.lock` holds an OS-level exclusive lock for the life of the process (released by the OS if the process dies). A second instance exits with code 3. |

## 2. Running

Run from the repository root (the default state directory `state/` is relative to the working
directory and gitignored), with the project venv:

```bash
# one loop, compact status, exit (smoke test)
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once      # Windows
.venv/bin/python        -m basis_arb.live.daemon --root BTCUSDT --mode paper --once      # Linux/macOS

# continuous paper loop, 60 s cadence (spec section 9)
python -m basis_arb.live.daemon --root BTCUSDT --mode paper --interval 60
python -m basis_arb.live.daemon --root ETHUSDT --mode paper --interval 60   # separate process, separate book

# testnet routing (keys in the environment of this process only)
export BINANCE_TESTNET_API_KEY=...  BINANCE_TESTNET_API_SECRET=...      # PowerShell: $env:BINANCE_TESTNET_API_KEY="..."
python -m basis_arb.live.daemon --root BTCUSDT --mode testnet --interval 60
```

Options: `--strategies funding_carry,cash_and_carry,calendar,random_maturity` (default: all four),
`--state-dir PATH`, `--max-loops N`, `--clear-halt`, `--log-level INFO`. Status goes to stdout (one
line per loop, or the multi-line status with `--once`); logs go to stderr.

Exit codes: `0` normal stop (`--once`, `--max-loops`, Ctrl+C), `1` uncaught crash (Python default; safe
to restart, the book is saved every loop), `2` testnet mode without both keys (refused before any state
is touched), `3` refused at start (halted book, root/mode/strategy mismatch with the existing book,
another daemon holds the lock, or the testnet credential check failed), `4` HALT (a kill switch fired;
never restart automatically).

Example `--once` output (2026-10-17 20:22 UTC, paper, all strategies flat):

```
basis_arb live | BTCUSDT | paper | 2026-10-17T20:22:14Z | loop 1 | OK
quote  spot 81619.79  perp 81579.45 (mark 81579.40, index 81616.10)  latency 781ms  age 2.4s
       funding last 0.0045% next 2026-10-18T00:00Z | cq BTCUSDT_261225 82470.15 dte 68.5 | nq BTCUSDT_270326 83516.05 dte 159.5
panel  1081 rows 2026-09-02T21:00Z .. 2026-10-17T21:00Z (last = live row) | r_usd 0.0410 (fred) | decision loop: yes
signal funding_carry    target  0 FLAT   excess -0.0046 hurdle 0.0762   action FLAT  pos flat
       ...
book   equity 55000.00 / equity_0 55000.00 (dd 0.00%) | fills this loop 0 | funding events 0 | safe: -
```

## 3. What one loop does

| Step | What | Source |
|---|---|---|
| 1 | Rebuild the recent panel (45 days of complete 1h bars) on the first loop of each UTC hour; on failure keep the cached panel and retry every 5 min | `history.recent_panel` (spot/perp/quarterly klines, funding prints, exchangeInfo, FRED DTB3) |
| 2 | Quote snapshot: system status, spot + perp + CQ/NQ `bookTicker`, `premiumIndex`; exchangeInfo cached 1 h | `feed.LiveFeed.snapshot` |
| 3 | Funding: if `nextFundingTime` seen on the previous loop has passed, fetch the settled print(s) from `/fapi/v1/fundingRate` and book `-qty x rate x markPrice` to every perp leg held **before** the stamp (also legs closed after the stamp); retried each loop until the print is published; the print is written into the panel | `history.funding_prints` |
| 4 | Expiry: a held quarterly whose expiry has passed settles at the perp index price (spot mid if absent) and pays the 5 bp delivery fee | spec section 6 step 2 |
| 5 | Decision: append the live row (in-progress bar, `ts = ceil(now)`, close = high = low = mid), run `build_targets`, take the last row. A new target is **adopted once per bar**, on the first loop after the hourly bar closes, as the backtest decides at bar closes; intra-bar loops only monitor. A new book, or a restart in a later bar, decides on its first loop; a restart within an already-decided bar waits for the next bar | `model.signals` |
| 6 | Reconcile the book to the adopted target: close legs that are not wanted, open missing legs with equal coin quantity (`notional_per_leg / first-leg mid`; a roll keeps the kept leg's quantity). Exits are always allowed; entries and rolls only on a decision loop and only when not SAFE. A blocked roll or a failed leg flattens the whole strategy (no unhedged remainder) | `broker.place` |
| 7 | Mark to market at mids, drawdown check, `SNAPSHOT` + `HEARTBEAT` events, atomic `book.json` save, sleep to the next 60 s tick | `state.Book` |

## 4. Kill switches (fail-secure: block on uncertainty)

| Condition | Threshold | Action | Event | Clears |
|---|---|---|---|---|
| Quote stale (older of receipt time and exchange time) | > 5 min | SAFE: no decision this loop (positions kept, nothing traded on a stale price) | `SAFE` code `STALE_QUOTE` | next fresh quote |
| Feed error: non-200, missing/non-numeric field, crossed or non-positive book, perp not TRADING, `system/status != 0` (maintenance) | any | SAFE as above | `SAFE` code `FEED_ERROR` | next good snapshot |
| No fresh quote | > 30 min | flatten every leg (paper: at the last fresh quote, FILLs flagged `stale_quote: true`; testnet: market orders) and HALT | `KILL` switch `STALE_QUOTE_30MIN`, `FILL` reason `KILL`, `HALT` | manual (`--clear-halt` or reset) |
| Exception in the decision path (live row, `build_targets`, reconcile) | any | SAFE until restart: no new entries or rolls; exits still allowed; decisions keep running | `ERROR` + `SAFE` code `DECISION_EXCEPTION` | process restart |
| Order rejected / transport error on an order | any | unwind the strategy's legs, SAFE until restart | `ERROR` + `SAFE` code `BROKER_FAILURE` | process restart |
| History unavailable at start | no panel | SAFE (no decisions) | `SAFE` code `NO_PANEL` | panel loaded |
| Equity drawdown from `equity_0` | > 5 % | flatten every leg and HALT | `KILL` switch `DRAWDOWN`, `HALT` | manual reset (a cleared halt re-fires while equity stays below the limit) |

`equity_0` is the sum of per-strategy capital (spec section 6): `notional x (1 + 1/leverage)` for the
spot-leg strategies (15,000 USDT each at 10,000 notional and 2x) and `2 x notional / leverage` for the
calendar (10,000 USDT): 55,000 USDT for all four.

A HALTED book refuses to start (exit 3) until an operator either passes `--clear-halt` (writes a
`HEARTBEAT` with `phase: clear_halt` and the previous halt record) or resets the state.

## 5. State files (`state/{ROOT}/`)

| File | Content | Write pattern |
|---|---|---|
| `book.json` | `schema_version`, `root`, `mode`, `created_ts`, `equity_0`, `cash`, `last_equity`, `halted` (null or `{switch, detail, ts, unflattened, equity}`), funding bookkeeping (`funding_cursor_ms` = last accrued stamp, `next_funding_ms`, `funding_due_ms`, `closed_perp` legs still owed a pending stamp), `marks` (last mid per instrument), `last_decision_bar`, `last_loop_ts`, `counters` (loops, fills, funding_events, expiries, safe_loops, errors, kills) and per strategy: `capital`, `cash`, `target`/`contract` (actual legs), `positions` (leg, instrument, signed qty, entry price, entry ts, expiry), `funding` (+ received), `fees` (+ paid, incl. delivery), `slippage` (+ cost vs mid), counts, `signal_target`/`signal_contract`/`signal_reason` (last adopted decision) | rewritten every loop: temp file in the same directory, fsync, `os.replace` (atomic) |
| `events.jsonl` | append-only ledger, one strict JSON object per line (no NaN; timestamps UTC ISO 8601 `+00:00`); `ts` and `kind` first | appended and flushed per event |
| `daemon.lock` | PID of the process holding the lock (informational; the lock itself is an OS lock, a leftover file is harmless) | at start |

P&L identity per strategy: `equity - capital = basis + funding - fees - slippage`, where `basis` is the
mid-to-mid convergence of the legs. Equity uses the backtest's mark convention:
`cash + spot_qty x spot_mid + sum(fut_qty x (fut_mid - entry_price))`.

## 6. Reading `events.jsonl`

| `kind` | When | Main fields |
|---|---|---|
| `HEARTBEAT` | every loop; also `phase: start/stop/clear_halt` | `loop`, `status` OK/SAFE, `safe` codes, `decision_loop`, `fills`, `equity`, `quote_age_s`, `latency_ms` |
| `SNAPSHOT` | every loop | `quote` (bid/ask per leg, mark, index, funding, symbols, expiries), `equity`, `equity_0`, `drawdown`, per-strategy legs + P&L components, `r_usd_source` (`fred` / `fallback`) |
| `DECISION` | once per bar (decision loop) | `bar`, `live_row_ts`, `entries_allowed`, per strategy `target`, `contract`, `excess`, `hurdle`, `reason` (from model.signals) and `action` (ENTER, EXIT, ROLL, FLIP, HOLD, FLAT, BLOCKED_SAFE, UNSUPPORTED, `EXIT(<why>)`, UNWOUND, BROKER_ERROR, ERROR) |
| `FILL` | every executed order | `strategy`, `leg`, `instrument`, `side`, `qty`, `price`, `fee`, `reason` (ENTER/EXIT/ROLL/KILL), `fill_ts`, `mid`, `slippage_cost`, `realized`, `broker` (paper/testnet), `quote_ts`, `stale_quote` |
| `FUNDING_ACCRUAL` | per perp leg per settled stamp | `stamp`, `strategy`, `qty`, `rate`, `mark_price`, `amount` (+ received), `closed_leg` |
| `EXPIRY` | quarterly settled | `instrument`, `settle_price`, `price_source`, `realized`, `delivery_fee` |
| `SAFE` | when the set of SAFE codes changes | `active`, `codes`, `reasons`, `sticky` |
| `KILL` / `HALT` | kill switch | `switch`, `detail`; HALT adds `unflattened`, `equity` |
| `ERROR` | any caught exception | `where`, `error`, `traceback` |

```bash
jq -c 'select(.kind=="FILL" or .kind=="FUNDING_ACCRUAL" or .kind=="KILL" or .kind=="HALT")' state/BTCUSDT/events.jsonl
python -c "import pandas as pd; e = pd.read_json('state/BTCUSDT/events.jsonl', lines=True); print(e.kind.value_counts())"
```

```powershell
Get-Content state\BTCUSDT\events.jsonl | ConvertFrom-Json | Where-Object kind -eq 'FILL' |
  Format-Table ts, strategy, leg, side, qty, price, fee, reason
```

Size: about 1.7 KB per loop (SNAPSHOT + HEARTBEAT), roughly 2.5 MB per day per underlying. Rotate by
stopping the daemon and moving `events.jsonl` to an archive name; a new file is started on the next run.

## 7. Resetting state

1. Stop the daemon (Ctrl+C, `systemctl stop`, or end the scheduled task) so the lock is released.
2. Archive, do not delete, the old book: `mv state/BTCUSDT state/BTCUSDT.2026-10-18` (PowerShell:
   `Move-Item state\BTCUSDT state\BTCUSDT.2026-10-18`).
3. Start again: a new `book.json` is created with fresh `equity_0`, cash and an empty ledger.

Use a reset (not `--clear-halt`) after a drawdown HALT, after changing `--strategies`, or when switching
between paper and testnet for the same root (a book records its mode and refuses the other one; a separate
`--state-dir` per mode also works).

## 8. Testnet mode: can and cannot

* Can: MARKET orders on the spot testnet (`BTCUSDT` verified TRADING on 2026-10-18) and the USD-M futures
  testnet (perpetuals; quarterlies only while the testnet lists them as `TRADING`). On 2026-10-18 the
  futures testnet listed `BTCUSDT_261225` / `ETHUSDT_261225` as TRADING and `*_270326` as
  `PENDING_TRADING`. Fills are parsed from the responses (`avgPrice`/`executedQty` for futures,
  `cummulativeQuoteQty / executedQty` for spot); quantities are floored to the testnet step size and both
  legs use the coarser step. At start the daemon checks the credentials with `GET /fapi/v2/balance` and
  `GET /api/v3/account` and refuses to start (exit 3) if either fails. Timestamps are synced to the testnet
  server clock; a `-1021` (outside `recvWindow=5000`) is retried once after a resync.
* Cannot: trade a contract the testnet does not list as TRADING (`UnsupportedOnTestnet`: the entry is
  skipped with action `UNSUPPORTED`; a held position that would need it, e.g. a roll into the next quarter,
  is closed instead). In practice S1/S3 can only use the current quarter on testnet, and rolls into the
  next quarter are refused while it is `PENDING_TRADING`.
* Decisions still use **mainnet** public quotes (the real market); MTM marks are mainnet mids, so the
  testnet-vs-mainnet price difference shows up as basis P&L noise. Futures commissions are modeled at the
  configured taker rate (order responses carry no commission); spot commissions are taken from the
  response when they are in the base or quote asset.
* The testnet account nets positions across strategies (one-way mode, no `reduceOnly`); the book is the
  per-strategy record and is not reconciled against testnet account positions.
* Orders are never retried after a transport error (the order state is unknown): the strategy is unwound
  where possible and the daemon goes SAFE until restart. Check the testnet UI for the client order id
  (`cba-<ms>-<n>`) in that case.

## 9. Running as a scheduled service

Windows Task Scheduler (current user, at logon; the lock makes a second start a no-op with exit 3;
Windows may require an elevated prompt to create an `ONLOGON` task):

```bat
schtasks /Create /TN "basis_arb_live_BTCUSDT" /SC ONLOGON /RL LIMITED /F /TR "cmd /c cd /d D:\vscode\crypto_basis_arb && .venv\Scripts\python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --interval 60 >> state\daemon_BTCUSDT.log 2>&1"
```

For a self-healing watchdog, add a second trigger with `/SC MINUTE /MO 5` and the same `/TR`: if the
daemon is alive the new instance is refused by the lock (exit 3) without touching the state; if it died
it is restarted. A HALTED book also exits 3, so a watchdog never silently clears a kill switch. (In
testnet mode each attempt first calls the two testnet balance endpoints, before the lock is checked.)

systemd (Linux), `/etc/systemd/system/basis-arb-live@.service`, started as
`systemctl enable --now basis-arb-live@BTCUSDT`:

```ini
[Unit]
Description=crypto_basis_arb paper-live daemon (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=basisarb
WorkingDirectory=/opt/crypto_basis_arb
ExecStart=/opt/crypto_basis_arb/.venv/bin/python -m basis_arb.live.daemon --root %i --mode paper --interval 60
Restart=on-failure
RestartSec=30
# 2 = missing testnet keys, 3 = refused at start, 4 = HALT (kill switch): never auto-restart through these;
# 1 (uncaught crash) is restarted
RestartPreventExitStatus=2 3 4
# testnet mode only: keys from a root-owned 0600 file outside the repository
# EnvironmentFile=/etc/crypto_basis_arb/testnet.env

[Install]
WantedBy=multi-user.target
```

## 10. Known limitations (live layer)

* Decisions are adopted once per hourly bar; between bars the daemon only monitors, accrues funding,
  settles expiries and applies kill switches. The live row is the in-progress bar (its close is the mid a
  few seconds after the previous bar closed), so live decisions lag the backtest's bar-close decision by
  that delay, and dte on the live row is measured to the bar end.
* The state machines run on a 45-day window and start flat at its first bar; S2/S3 need a full 30-day
  trailing-funding window (no decision before that), so the live state can differ from a full-sample
  backtest until the first exit after the window start.
* While SAFE because of a stale quote or feed error, no decision is taken at all (no paper exit on a
  stale price); positions are held until quotes return or the 30-minute kill flattens them.
* Expiry settlement uses the index at the first loop after delivery (Binance settles on the average index
  of the final 30 minutes). No margin or liquidation model runs live (paper legs cannot be liquidated).
* `live/history.py` duplicates part of `src/basis_arb/data/` (klines, funding, quarterlies, FRED, panel
  assembly); it should delegate to `data/` once that layer is final, with a parity test on an overlapping
  window.
