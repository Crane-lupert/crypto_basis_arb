# Paper-live operations runbook

Scope: `src/basis_arb/live/` (strategy_spec.md section 9, v1.0.2). The daemon trades **paper fills on live
Binance quotes**, or sends orders to the **Binance testnet**. Nothing in this repository can send an
order to a real-money endpoint; promotion to real capital is a separate decision outside this codebase.
Paper results are presented as paper, testnet fills as testnet (strategy_spec.md section 10).

## 1. Guarantees

| Guarantee | How it is enforced |
|---|---|
| Research/live parity | Decisions come from `daemon.decide_closed()`, which runs `basis_arb.model.signals.build_targets` (the backtest's function) on **closed 1h bars only** and reads the last row. A new decision is taken only when a new hour has closed, so bar counters (min hold, funding window) mean hours exactly as in the backtest. `tests/test_live_daemon.py::test_replay_of_closed_bars_matches_build_targets` feeds 48 hourly bars to the daemon one closed bar per loop and asserts that the adopted `(target, contract)` sequence per strategy equals `build_targets` on the same bars. |
| No decision on a provisional price | The current hour is one provisional row built from the live quote (`feed.append_live_row`), replaced on every poll and used only for marking, fills and kill switches; it never enters the decision input. |
| No mainnet trading host | `live/broker.py` contains only `https://testnet.binancefuture.com` (futures) and `https://testnet.binance.vision` (spot). The constructor takes no base URL and every signed request passes `_require_testnet()`. `tests/test_live_broker.py::test_broker_source_contains_no_mainnet_host` reads the source and asserts that neither `api.binance.com`, `fapi.binance.com` nor any `binance.com` host appears, and that the only URL hosts are the two testnets. Mainnet hosts appear only in `live/history.py` / `live/feed.py`, which issue keyless public GETs. |
| Paper and testnet never mix | State is per mode: `state/{ROOT}/paper/` and `state/{ROOT}/testnet/` each hold their own `book.json`, `events.jsonl` and lock, so testnet fills never enter the paper P&L or ledger. |
| No secrets in files | Testnet keys are read from the environment only (`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`). They are never logged, never written to `state/`, and broker error messages and `repr()` never contain them. Do not create a `.env` in the repo. |
| One daemon per book | `state/{ROOT}/{mode}/daemon.lock` holds an OS-level exclusive lock for the life of the process (released by the OS if the process dies). A second instance on the same book exits with code 3. |

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
`--state-dir PATH`, `--max-loops N`, `--clear-halt`, `--log-level INFO`. Status goes to stdout (one line
per loop, or the multi-line status with `--once`); logs go to stderr.

| Exit code | Meaning | Supervisor action |
|---|---|---|
| 0 | normal stop (`--once`, `--max-loops`, Ctrl+C) | none |
| 1 | uncaught crash (Python default); the book is saved every loop | restart |
| 2 | testnet mode without both keys (refused before any state is touched) | fix the environment |
| 3 | refused at start: halted book, root/mode/strategy mismatch with the existing book, another daemon holds the lock, or the testnet credential check failed | operator |
| 4 | HALT: no fresh quote for 30 min (positions kept) | operator, never automatic |

## 3. What one loop does

| Step | What | Source |
|---|---|---|
| 1 | Rebuild the closed-bar panel (45 days of 1h bars ending at the last closed hour) as soon as a new hour has closed; retried every 60 s while the newest bar is missing | `history.recent_panel` (spot/perp/quarterly klines, funding prints, exchangeInfo, FRED DTB3) |
| 2 | Quote snapshot: system status, spot + perp + CQ/NQ `bookTicker`, `premiumIndex`; exchangeInfo cached 1 h | `feed.LiveFeed.snapshot` |
| 3 | Funding: if `nextFundingTime` seen on the previous loop has passed, fetch the settled print(s) from `/fapi/v1/fundingRate` and book `-qty x rate x markPrice` to every perp leg held **before** the stamp (also legs closed after the stamp); retried each loop until published; the print is written into the closed panel | `history.funding_prints` |
| 4 | No fresh quote for 30 min: HALT without flattening | kill switch |
| 5 | Expiry: a held quarterly whose expiry has passed settles at the perp index price (spot mid if absent) and pays the 5 bp delivery fee | spec section 6 step 2 |
| 6 | Decision, once per newly closed bar: `decide_closed(closed panel)`, adopted as the target until the next bar closes. If a crossed funding stamp's settled print is still missing from the panel, the decision waits up to 10 min for it (then decides and logs an `ERROR`). A new book, or a restart after a new bar closed, decides on its first loop; a restart within an already-decided bar waits for the next bar | `model.signals` |
| 7 | Provisional row for the current hour (`ts = ceil(now)`, close = high = low = mid; skipped while history lags); mark to market at mids; margin monitor; drawdown check | `feed.append_live_row`, `state.Book` |
| 8 | Reconcile the book to the adopted target: close legs that are not wanted, open missing legs with equal coin quantity (`notional_per_leg / first-leg mid`; a roll keeps the kept leg's quantity). **Exits** are allowed on every loop, also while SAFE, at the latest quote (fills on a quote older than 5 min carry `stale_quote: true`). **Entries and rolls** happen only on the decision loop, only with a fresh quote and only when nothing is SAFE; a blocked entry waits for the next closed bar. A blocked roll, an unsupported contract or a failed leg flattens the whole strategy (no unhedged remainder) | `broker.place` |
| 9 | `SNAPSHOT` + `HEARTBEAT` events, atomic `book.json` save, sleep to the next 60 s tick | |

## 4. Kill switches (fail-secure: block on uncertainty, never trade blind)

| Condition | Threshold | Action | Event | Clears |
|---|---|---|---|---|
| Quote stale (older of receipt time and exchange time) | > 5 min | SAFE: no new entries; exits allowed (flagged `stale_quote`) | `SAFE` code `STALE_QUOTE` | next fresh quote |
| Feed error: non-200, missing/non-numeric field, crossed or non-positive book, perp not TRADING, `system/status != 0` (maintenance), any other feed exception, a live row that fails the panel contract | any | SAFE as above | `SAFE` code `FEED_ERROR` | next good snapshot |
| No fresh quote | > 30 min | **HALT without flattening**: positions stay, nothing is traded on stale prices, the loop exits | `HALT` switch `STALE_QUOTE_30MIN`, `flattened: false`, `positions_kept` | manual (`--clear-halt` or reset) |
| Equity drawdown from `equity_0` | > 5 % | SAFE until restart (no flattening; re-evaluated at every loop, so a restart with equity still below the limit is SAFE again) | `SAFE` code `DRAWDOWN` | restart with equity above the limit |
| Margin-utilization breach on an isolated futures leg of a spot-leg strategy (S1, S2, S4): adverse move since entry on the mark (perp mark price, quarterly mid) > `1/leverage - maintenance` (49.6 % at 2x) | per leg | flatten **that strategy**: the breached leg (`LIQUIDATION`, paper books the 50 bp liquidation penalty) then its hedge (`KILL`); SAFE until restart. S3 (calendar) uses cross-collateral semantics: utilization is tracked (`peak_adverse_move`), it never breaches | `KILL` switch `MARGIN_BREACH`, `SAFE` code `MARGIN_BREACH` | restart |
| Exception in the decision path (`decide_closed`, reconcile) or anywhere in the loop | any | SAFE until restart: no new entries or rolls; decisions keep running, exits allowed | `ERROR` + `SAFE` code `DECISION_EXCEPTION` | restart |
| Order rejected / transport error on an order | any | unwind the strategy's legs, SAFE until restart | `ERROR` + `SAFE` code `BROKER_FAILURE` | restart |
| Funding endpoint failure | any | SAFE for that loop, retried next loop | `ERROR` + `SAFE` code `FUNDING_ERROR` | next good fetch |
| History unavailable / newest closed bar missing | no panel / lag | SAFE; decisions paused (positions held, no provisional row across a hole) | `SAFE` code `NO_PANEL` / `HISTORY_LAG` | panel current again |

`equity_0` is the sum of per-strategy capital (spec section 6): `notional x (1 + 1/leverage)` for the
spot-leg strategies (15,000 USDT each at 10,000 notional and 2x) and `2 x notional / leverage` for the
calendar (10,000 USDT): 55,000 USDT for all four. HALT and SAFE events are the operator's cue (spec
section 9). A HALTED book refuses to start (exit 3) until an operator either passes `--clear-halt` (writes
a `HEARTBEAT` with `phase: clear_halt` and the previous halt record) or resets the state.

## 5. State files (`state/{ROOT}/{mode}/`, mode = `paper` or `testnet`)

| File | Content | Write pattern |
|---|---|---|
| `book.json` | `schema_version`, `root`, `mode`, `created_ts`, `equity_0`, `cash`, `last_equity`, `halted` (null or `{switch, detail, ts, flattened, positions_kept, equity}`), funding bookkeeping (`funding_cursor_ms` = last accrued stamp, `next_funding_ms`, `funding_due_ms`, `closed_perp` legs still owed a pending stamp), `marks` (last mid per instrument), `last_decision_bar` (the closed bar of the last decision), `last_loop_ts`, `counters` (loops, fills, funding_events, expiries, safe_loops, errors, kills) and per strategy: `capital`, `cash`, `target`/`contract` (actual legs), `positions` (leg, instrument, signed qty, entry price, entry ts, expiry, `peak_adverse_move`), `funding` (+ received), `fees` (+ paid, incl. delivery), `slippage` (+ cost vs mid), `penalties` (+ paid), counts, `signal_target`/`signal_contract`/`signal_reason` (the adopted decision) | rewritten every loop: temp file in the same directory, fsync, `os.replace` (atomic) |
| `events.jsonl` | append-only ledger, one strict JSON object per line (no NaN; timestamps UTC ISO 8601 `+00:00`); `ts` and `kind` first | appended and flushed per event |
| `daemon.lock` | PID of the process holding the lock (informational; the lock itself is an OS lock, a leftover file is harmless) | at start |

P&L identity per strategy: `equity - capital = basis + funding - fees - slippage - penalties`, where
`basis` is the mid-to-mid convergence of the legs. Equity uses the backtest's mark convention:
`cash + spot_qty x spot_mid + sum(fut_qty x (fut_mid - entry_price))`.

## 6. Reading `events.jsonl`

| `kind` | When | Main fields |
|---|---|---|
| `HEARTBEAT` | every loop; also `phase: start/stop/clear_halt` | `loop`, `status` OK/SAFE, `safe` codes, `decision_loop`, `fills`, `equity`, `quote_age_s`, `latency_ms` |
| `SNAPSHOT` | every loop | `quote` (bid/ask per leg, mark, index, funding, symbols, expiries), `quote_fresh`, `equity`, `equity_0`, `drawdown`, per-strategy legs + P&L components, `closed_bar`, `r_usd_source` (`fred` / `fallback`) |
| `DECISION` | once per closed bar | `bar`, `entries_allowed`, `quote_fresh`, `safe`, `notes` (e.g. unsupported instruments), per strategy `target`, `contract`, `excess`, `hurdle`, `reason` (from model.signals) and `action` (ENTER, EXIT, ROLL, FLIP, HOLD, FLAT, BLOCKED_SAFE, WAIT_BAR, UNSUPPORTED, `EXIT(<why>)`, UNWOUND, NO_QUOTE, BROKER_ERROR, ERROR) |
| `FILL` | every executed order | `strategy`, `leg`, `instrument`, `side`, `qty`, `price`, `fee`, `reason` (ENTER/EXIT/ROLL/KILL/LIQUIDATION), `fill_ts`, `mid`, `slippage_cost`, `realized`, `broker`, `quote_ts`, `quote_age_s`, `stale_quote` |
| `FUNDING_ACCRUAL` | per perp leg per settled stamp | `stamp`, `strategy`, `qty`, `rate`, `mark_price`, `amount` (+ received), `closed_leg` |
| `EXPIRY` | quarterly settled | `instrument`, `settle_price`, `price_source`, `realized`, `delivery_fee` |
| `SAFE` | when the set of SAFE codes changes | `active`, `codes`, `reasons`, `sticky` |
| `KILL` | margin breach flatten | `switch: MARGIN_BREACH`, `strategy`, `instrument`, `mark`, `entry_price`, `adverse_move`, `limit` |
| `HALT` | 30 min without a fresh quote | `switch`, `detail`, `flattened: false`, `positions_kept`, `equity` |
| `ERROR` | any caught exception | `where`, `error`, `traceback` |

```bash
jq -c 'select(.kind=="FILL" or .kind=="FUNDING_ACCRUAL" or .kind=="KILL" or .kind=="HALT")' state/BTCUSDT/paper/events.jsonl
python -c "import pandas as pd; e = pd.read_json('state/BTCUSDT/paper/events.jsonl', lines=True); print(e.kind.value_counts())"
```

```powershell
Get-Content state\BTCUSDT\paper\events.jsonl | ConvertFrom-Json | Where-Object kind -eq 'FILL' |
  Format-Table ts, strategy, leg, side, qty, price, fee, reason
```

Size: about 1.7 KB per loop (SNAPSHOT + HEARTBEAT), roughly 2.5 MB per day per book. Rotate by stopping
the daemon and moving `events.jsonl` to an archive name; a new file is started on the next run.

## 7. Resetting state

1. Stop the daemon (Ctrl+C, `systemctl stop`, or end the scheduled task) so the lock is released.
2. Archive, do not delete, the old book of that mode: `mv state/BTCUSDT/paper state/BTCUSDT/paper.2026-10-18`
   (PowerShell: `Move-Item state\BTCUSDT\paper state\BTCUSDT\paper.2026-10-18`).
3. Start again: a new `book.json` is created with fresh `equity_0`, cash and an empty ledger.

Use a reset (not `--clear-halt`) after changing `--strategies`, or when equity is below the drawdown limit
and a fresh `equity_0` is intended. Paper and testnet books are independent; resetting one does not touch
the other.

## 8. Testnet mode: can and cannot

* Can: MARKET orders on the spot testnet and the USD-M futures testnet (perpetuals). Fills are parsed
  from the responses (`avgPrice`/`executedQty` for futures, `cummulativeQuoteQty / executedQty` for spot);
  quantities are floored to the testnet step size and both legs use the coarser step. At start the
  daemon checks the credentials with `GET /fapi/v2/balance` and `GET /api/v3/account` and refuses to
  start (exit 3) if either fails. Timestamps are synced to the testnet server clock; a `-1021` (outside
  `recvWindow=5000`) is retried once after a resync. Fills and P&L live in `state/{ROOT}/testnet/`, never
  in the paper ledger.
* Cannot: trade a delivery contract that the futures testnet does not list as `TRADING`. The spec treats
  quarterlies as unsupported on the testnet (S1/S3 unsupported there); the daemon checks the testnet
  listing on every entry: the strategy's entry is skipped with action `UNSUPPORTED` in the `DECISION`
  event (with `notes.<strategy>.unsupported`), no SAFE, and the other strategies trade normally. A held
  position that would need an unsupported contract (e.g. a roll) is closed instead, and an
  `UnsupportedOnTestnet` raised while placing an order unwinds the strategy's partial legs the same way.
  (On 2026-10-18 a probe of the futures testnet exchangeInfo listed `BTCUSDT_261225` as `TRADING` and
  `*_270326` as `PENDING_TRADING`; whichever way the listing goes, the check is live, not hard-coded.)
* Decisions use closed **mainnet** klines; MTM marks are mainnet mids, so the testnet-vs-mainnet price
  difference shows up as basis P&L noise. Futures commissions are modeled at the configured taker rate
  (order responses carry no commission); spot commissions come from the response when they are in the base
  or quote asset.
* The testnet account nets positions across strategies (one-way mode, no `reduceOnly`); the book is the
  per-strategy record and is not reconciled against testnet account positions. After a margin-breach
  flatten the testnet exchange may already have liquidated the leg; check the account.
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
# 2 = missing testnet keys, 3 = refused at start, 4 = HALT: never auto-restart through these;
# 1 (uncaught crash) is restarted
RestartPreventExitStatus=2 3 4
# testnet mode only: keys from a root-owned 0600 file outside the repository
# EnvironmentFile=/etc/crypto_basis_arb/testnet.env

[Install]
WantedBy=multi-user.target
```

## 10. Known limitations (live layer)

* Decisions are taken at the first poll after an hour closes (up to one poll interval plus the kline
  fetch after the close) and filled at the live quote then, not at the bar's close price.
* The state machines run on a 45-day window and start flat at its first bar; S2/S3 need a full 30-day
  trailing-funding window (no decision before that), so the live state can differ from a full-sample
  backtest until the first exit after the window start.
* Exits while SAFE because of a stale quote are paper-filled at the latest quote (at most 30 min old,
  flagged `stale_quote`); beyond 30 min the daemon HALTs and trades nothing.
* Expiry settlement uses the index at the first loop after delivery (Binance settles on the average index
  of the final 30 minutes). The margin monitor uses the perp mark price and the quarterly mid at each poll,
  not intrabar extremes.
* The notional resize rule of the backtest engine (spec section 6 step 6) is not applied live.
* `live/history.py` duplicates part of `src/basis_arb/data/` (klines, funding, quarterlies, FRED, outage
  forward-fill, panel assembly); it should delegate to `data/` once that layer is final, with a parity test
  on an overlapping window. Unlike `data/`, it trims a window that contains an outage longer than 5 h
  instead of raising, so the daemon keeps running.
