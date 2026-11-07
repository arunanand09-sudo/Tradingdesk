# PRD: Intraday Trading Research and Execution System

**Owner:** Arun
**Status:** Draft v1.0
**Date:** 2026-11-01
**Intended consumer:** Claude Code (this document is the implementation brief)

---

## 0. How to use this document

This PRD is written to be handed to Claude Code as the authoritative spec. Requirements are
numbered (`R-<AREA>-<NN>`) so they can be referenced in commits, tests and issues. Every
functional requirement has an acceptance criterion. Where a decision is still open it is listed
in Section 16 rather than guessed at inside the body.

Build order is Section 13. Do not build later phases first. The risk engine (Section 10) is
phase one, not a later hardening task.

---

## 1. Assumptions (read this first)

The scope of this document was set without the market, instrument, capital base or trading style
being specified. The following assumptions were made. **If any is wrong, Sections 6, 10 and 13
change materially and the document must be revised before coding starts.**

| # | Assumption | Impact if wrong |
|---|---|---|
| A1 | Operator is a UK-resident individual trading own capital, not a regulated firm | Changes tax, broker entity, leverage caps, reporting |
| A2 | Starting capital is five figures GBP, not six or seven | Changes instrument choice and whether PDT binds |
| A3 | Holding periods are minutes to hours, flat overnight by default | Changes data granularity and funding cost model |
| A4 | Operator writes code and will run this himself on his own infrastructure | Changes hosting, UX and ops requirements |
| A5 | No existing proven strategy. The system must help find one | If a strategy already exists, skip to Phase 3 and cut Phase 1 scope |
| A6 | Goal is a durable personal system, not a product to sell to others | A sellable product adds FCA authorisation questions. This document does not cover those |

---

## 2. Problem statement

Discretionary intraday trading fails for retail participants at a rate that is well documented and
stable across markets. The failure is rarely caused by a lack of charting software. It is caused
by: trading without a measurable edge, position sizing that cannot survive a normal losing run,
no record of what was actually done, and no feedback loop that distinguishes skill from variance.

Evidence base, used here as a design constraint rather than discouragement:

- Chague, De-Losso and Giovannetti (2020) tracked 19,646 individuals who began day trading
  mini-Ibovespa futures between 2013 and 2015. Of the 1,551 who persisted beyond 300 days, 97%
  lost money and 0.5% earned more than a bank teller's starting salary. The share who were
  profitable **fell** with persistence: 15.5% of those trading 2 to 50 days, down to 3.0% of
  those trading more than 300 days. The authors found no evidence of learning.
- Barber, Lee, Liu and Odean (Taiwan, 1992 to 2006) found over 80% of day traders lose money
  after transaction costs, with a small persistent minority earning roughly double the gross
  spread of the broad population.

The design conclusion: **the system's primary job is to measure, constrain and falsify, not to
generate signals.** A system that makes it easy to trade a lot is actively harmful. A system that
makes it impossible to exceed risk limits, and that tells the operator honestly whether 100 trades
of evidence support continuing, is the useful artefact.

---

## 3. Goals

| ID | Goal | Measure of success |
|----|------|--------------------|
| G1 | Make risk limits mechanically unbreakable | No order can reach a broker that violates a configured limit. Proven by test suite |
| G2 | Make every hypothesis falsifiable before capital is committed | Every live strategy has a backtest, a walk-forward result and a paper-trade record on file |
| G3 | Make live results comparable to backtest results | Live vs backtest slippage, fill rate and PnL per trade reported automatically |
| G4 | Make the operator's own history queryable | Any question about past trades answerable by SQL, not memory |
| G5 | Reduce discretionary decisions under time pressure to a checklist | Pre-market plan produced before the session, not during it |
| G6 | Fail safe by default | Any component failure results in no new positions, not unmanaged positions |

---

## 4. Non-goals

Explicitly out of scope. Do not build these, and do not let scope creep reintroduce them.

- **N1. High-frequency or latency-sensitive trading.** Competing on speed against colocated firms
  is not viable from a retail stack. Strategies must be robust to 100ms to 1s of decision latency.
- **N2. Machine learning price prediction as a first deliverable.** Permitted only after Phase 4,
  and only inside the same validation gates as any other strategy. Deep learning on raw OHLCV with
  a few years of data overfits reliably.
- **N3. Fully autonomous unattended trading.** Phase 5 at the earliest, and only behind a
  supervisor process with a hard kill switch.
- **N4. Multi-user, multi-tenant, or sellable SaaS.** Single operator.
- **N5. Social or copy trading, signal subscriptions, sentiment-from-Twitter features.**
- **N6. Tax filing.** The system produces records sufficient for an accountant. It does not compute
  a tax liability.
- **N7. Options strategies.** Greeks, vol surfaces and multi-leg execution are a separate system.

---

## 5. Users and primary flows

Single user, three modes.

**Mode 1: Pre-session (20 minutes before open)**
Operator runs a command. System produces a dated session plan: account state, risk budget for the
day, scanner output ranked by the active strategy's criteria, each candidate with the levels and
invalidation point, overnight news on candidates, and any positions carried. Plan is immutable once
generated and is the only tradeable universe for that session.

**Mode 2: In-session**
Operator submits intended trades through the system, never directly through the broker platform.
System validates against the risk engine, sizes the position from the stop distance, places the
bracket (entry, stop, target) as a single atomic order group, and logs the full decision context.
System monitors the daily loss limit and halts on breach.

**Mode 3: Post-session and weekly review**
System reconciles broker fills against intended orders, computes per-trade R-multiples, updates the
journal, and reports rolling statistics with confidence intervals. Weekly it answers one question:
is there statistical evidence of an edge yet, and what is the probability the current run is noise.

---

## 6. Instrument and venue selection (a binding constraint, not a preference)

This is the decision with the largest downstream effect on the build, and the one most often made
by accident. Three viable UK retail routes, with the real constraints:

### Route A: US equities via Interactive Brokers
- **Blocker:** The FINRA Pattern Day Trader rule applies. A trader executing four or more day
  trades in five business days in a margin account must hold USD 25,000 minimum equity. This applies
  to IBKR UK clients, because US securities sit in the IB securities account governed by SEC/FINRA
  rules. Users on IBKR UK report being flagged. Verify with IBKR directly before building for this
  route.
- Below USD 25,000 the system must enforce a maximum of three day trades per rolling five business
  days, which is a hard design requirement, not a warning (`R-RISK-09`).
- Tax: Capital Gains Tax. 2026/27 annual exempt amount GBP 3,000, then 18% or 24% by band.
- API: TWS API (socket, full feature set, needs TWS or IB Gateway running) or Client Portal Web API
  (REST/OAuth, simpler, but the gateway re-authenticates roughly daily and lacks some order types).
  For an automated system, TWS API via `ib_async` is the more reliable choice.

### Route B: CME micro futures (MES, MNQ, MGC) via Interactive Brokers
- No PDT rule: it does not apply to futures.
- Standardised contracts, deep liquidity, central order book, visible depth, no payment-for-order-flow
  question, overnight sessions available.
- Leverage is high and intrinsic. One MES tick is USD 1.25; a 10 point move is USD 50 per contract.
  Position sizing discipline is the entire game.
- Tax: CGT as above. Losses are relievable against other gains.
- Best fit for a systematic intraday system with a small account.

### Route C: UK spread betting or CFDs via an FCA broker with an API (IG, CMC, Capital.com)
- No PDT rule. IG exposes a retail REST and streaming API (IG Labs) with Python wrappers available,
  rate limited to roughly 40 trade requests per minute.
- Spread betting profits are outside both Income Tax and CGT for most UK-resident individuals
  (TCGA 1992 s.51 treats betting winnings as outside chargeable gains). No stamp duty.
- **The symmetric cost: spread betting losses carry no tax relief at all.** For a strategy with an
  unproven edge, this is the wrong way round. Losses are the expected case in year one, and under
  Route C they are worth nothing back. CFDs are taxable but loss-relievable.
- You are trading against a dealer's quote, not an exchange book. Spread, slippage and the broker's
  own risk management are part of the strategy's cost, and they are not reproducible in a public
  historical dataset. Backtest-to-live divergence is structurally worse on this route.
- FCA retail leverage caps apply: 30:1 major FX down to 5:1 single equities and 2:1 crypto, with
  negative balance protection. Crypto derivatives remain banned for UK retail (CFDs, futures and
  options); crypto ETNs became available to retail on recognised exchanges from 8 October 2025.

### Requirement
**R-VENUE-01.** The system must not hardcode a venue. All broker interaction goes through a single
`BrokerAdapter` interface (Section 9.5). Ship with a paper adapter plus one live adapter. Adding a
second venue must require no change outside `adapters/`.
*Acceptance:* the full test suite passes against the paper adapter with no live credentials present.

**Recommendation:** build against Route B (micro futures via IBKR) as the first live adapter.
Reasons: no PDT constraint, exchange-traded price data that a backtest can actually reproduce,
loss relief preserved, and position sizing arithmetic that is explicit rather than hidden in a
dealer spread. Route C's tax treatment is attractive only once an edge is proven, which is exactly
when you would migrate.

---

## 7. Architecture

Modular monolith. Python. One process per role, communicating through the database and a message
queue, not shared memory. Do not start with microservices.

```
                  +-------------------+
                  |  CLI / TUI (ops)  |
                  +---------+---------+
                            |
+-------------+     +--------v---------+     +------------------+
| Data Layer  |---->|  Core Services   |---->| Broker Adapters  |
+-------------+     +--------+---------+     +------------------+
| ingest      |              |               | paper (default)  |
| normalise   |     +--------v---------+      | ibkr             |
| store       |     |   RISK ENGINE    |      | ig               |
| replay      |     | (mandatory gate) |      +------------------+
+-------------+     +--------+---------+
                             |
     +-----------------------+-----------------------+
     |            |              |                  |
+----v----+  +----v-----+  +-----v------+   +--------v-------+
| Research |  | Scanner  |  | Execution  |   | Journal /      |
| Backtest |  | Session  |  | Order Mgmt |   | Analytics      |
| Walk-fwd |  | Planner  |  | Reconcile  |   | Reporting      |
+----------+  +----------+  +------------+   +----------------+
```

**Hard rule R-ARCH-01:** there is exactly one code path from intent to broker, and it passes
through the risk engine. No module may call a broker adapter directly.
*Acceptance:* a static check (import-linter or equivalent) fails the build if any module other than
`execution.gateway` imports from `adapters/`.

**R-ARCH-02:** identical strategy code runs in backtest, paper and live. No separate live
implementation.
*Acceptance:* one strategy class, instantiated by all three runners, in the test suite.

---

## 8. Data layer

**R-DATA-01.** Store immutable raw vendor payloads separately from normalised bars. Never overwrite
history in place. Corrections arrive as new rows with a `revision` and `ingested_at`.
*Acceptance:* re-running an ingest for a past date produces new rows, not mutations, and a backtest
pinned to an `as_of` timestamp returns identical results on re-run.

**R-DATA-02.** Point-in-time correctness. A backtest must never see data that did not exist at the
simulated decision time. This includes corporate actions, index membership and any fundamental or
news field.
*Acceptance:* a deliberate look-ahead test (a strategy that peeks one bar forward) must fail a
guard assertion rather than silently producing good results.

**R-DATA-03.** Survivorship bias control. Any instrument universe used in a backtest must include
delisted and expired instruments for the period tested.

**R-DATA-04.** Record the full cost model per instrument: commission schedule, exchange and
regulatory fees, expected spread by time of day, financing or funding rate, and a configurable
slippage model. Backtests run with costs on by default; costs-off runs must be labelled as such in
every output.

**R-DATA-05.** Bar construction must be explicit about timestamp convention (bar open vs close),
timezone (store UTC, display Europe/London), session boundaries, and treatment of pre/post market.

### Vendor notes (verify current pricing before committing)
- Massive (formerly Polygon.io, rebranded October 2025): individual US stock plans at Basic free,
  Starter about USD 29/mo, Developer about USD 79 to 99/mo, Advanced about USD 199/mo. Full tape on
  paid tiers. Good ergonomics, reasonable first choice for US equities.
- Databento: usage-based, institutional grade, the right answer if L2 depth, microsecond timestamps
  or CME futures microstructure is needed. Most expensive per unit of data.
- Tiingo, FMP, Alpha Vantage: cheaper, better for EOD, fundamentals and news than for intraday ticks.
- IBKR's own market data is adequate for execution but has non-cacheable licensing terms and is not
  a research dataset.

**R-DATA-06.** Data access goes through a `DataSource` interface with the same adapter discipline
as brokers. Switching vendor must not touch strategy code.

### Core schema (minimum viable, Postgres with TimescaleDB or DuckDB for research)

```sql
instrument(id, symbol, venue, asset_class, currency, tick_size, tick_value,
           contract_size, first_seen, last_seen, expiry, is_active)

bar(instrument_id, ts_utc, timeframe, open, high, low, close, volume, vwap,
    trades, source, revision, ingested_at)   -- PK (instrument_id, ts_utc, timeframe, revision)

session_plan(id, trade_date, generated_at, strategy_version, risk_budget_gbp,
             account_equity, notes, frozen_at)

candidate(id, session_plan_id, instrument_id, rank, setup_type, entry_trigger,
          stop_level, target_level, thesis, invalidation, max_size)

intent(id, session_plan_id, candidate_id, created_at, side, qty, order_type,
       limit_price, stop_price, target_price, risk_gbp, reason,
       risk_check_result, rejected_reason)

order(id, intent_id, broker_order_id, parent_id, status, submitted_at,
      acknowledged_at, order_type, qty, price, tif)

fill(id, order_id, broker_exec_id, ts_utc, qty, price, commission, fees, liquidity_flag)

position(id, instrument_id, opened_at, closed_at, side, qty_open, avg_entry,
         avg_exit, realised_pnl, fees_total, max_adverse_excursion,
         max_favourable_excursion)

trade(id, position_id, r_multiple, planned_r, slippage_entry, slippage_exit,
      holding_seconds, setup_type, outcome, journal_id)

journal(id, trade_id, pre_trade_note, post_trade_note, rule_followed_bool,
        deviation_reason, screenshot_path, emotional_state_tag)

risk_event(id, ts_utc, kind, severity, detail, action_taken)

account_snapshot(ts_utc, equity, cash, margin_used, buying_power,
                 day_trade_count_5d, open_risk_gbp)
```

**R-DATA-07.** `journal.rule_followed_bool` and `journal.deviation_reason` are mandatory before a
trade can be marked closed. Analytics must be able to split performance into rule-following and
rule-breaking trades. This single split is usually the most informative output the system produces.

---

## 9. Functional requirements by module

### 9.1 Research and backtest

**R-RES-01.** Event-driven backtester with per-bar or per-tick resolution, modelling order queue
position at least crudely (a limit order at the bid does not fill until the bid trades through).
**R-RES-02.** Vectorised sweep mode for parameter exploration, separate from the event-driven engine
and explicitly labelled as indicative only. No strategy may go live on vectorised results alone.
**R-RES-03.** Walk-forward validation is mandatory: rolling in-sample fit, out-of-sample test,
report both. In-sample-only results must be refused by the promotion gate.
**R-RES-04.** Record every backtest run with a content hash of code, data `as_of`, parameters and
cost model, so any result is reproducible from the hash alone.
**R-RES-05.** Report the multiple-testing count. If 400 parameter combinations were tried, the
report states 400 and applies a deflated Sharpe or equivalent haircut. Silent parameter mining is
the main way systems like this lie to their owners.
**R-RES-06.** Minimum reported metrics: trade count, expectancy in R, win rate, average win/loss in
R, profit factor, max drawdown in R and in currency, longest losing streak, Sharpe with confidence
interval, exposure time, and performance by time of day and day of week.
*Acceptance:* a known-bad strategy (random entries, fixed stops, costs on) must produce a report
whose conclusion is "no evidence of edge", and the promotion gate must reject it.

**Build-vs-adopt:** use `nautilus_trader` as the engine. It is event-driven, Rust-cored, has an
Interactive Brokers adapter, and its explicit design goal is identical strategy code across
backtest and live, which is `R-ARCH-02` for free. Use `vectorbt` for parameter sweeps only.
Do not start a new project on `backtrader` (effectively unmaintained). Writing a backtester from
scratch is a 6-month detour that produces a worse one.

### 9.2 Scanner and session planner

**R-SCAN-01.** Scanner runs on a schedule before the session and writes a `session_plan` with
ranked candidates. Each candidate carries entry trigger, stop, target and written invalidation.
**R-SCAN-02.** The plan is frozen at `frozen_at`. Instruments not in the frozen plan cannot be
traded that session without an explicit logged override, and overrides are reported separately in
analytics.
*Acceptance:* attempting an intent on an off-plan instrument returns a rejection with code
`OFF_PLAN` unless `--override` is passed with a reason string.
**R-SCAN-03.** Candidate count is capped (default 5). Ranking must be deterministic and reproducible.

### 9.3 Risk engine

See Section 10. This is the core of the system.

### 9.4 Execution and order management

**R-EXE-01.** All entries are submitted as atomic bracket groups (entry plus stop plus target). A
naked entry with no stop must be impossible to construct.
*Acceptance:* the type system or constructor refuses an `Intent` without a `stop_price`.
**R-EXE-02.** Idempotency. Every order carries a client-generated unique ID. Resubmission after a
timeout must not create a duplicate position.
*Acceptance:* a test that submits the same intent twice during a simulated broker timeout results in
one position.
**R-EXE-03.** Reconciliation on every startup and on a timer. Broker positions are truth. Any
divergence between internal state and broker state raises a `risk_event` at severity `critical` and
puts the system in `HALT_NEW_ENTRIES`.
**R-EXE-04.** Disconnection policy is configured explicitly, per strategy, before go-live: either
"flatten all on disconnect" or "stops are resting at the broker, do nothing". There is no third
option and no default. Stops must be resting server-side at the broker, never simulated locally.
*Acceptance:* a test that kills the process with an open position confirms a live stop order exists
at the broker (paper adapter asserts the equivalent).
**R-EXE-05.** Kill switch: a single command, and a physical or filesystem flag, that cancels all
working orders and optionally flattens. Must work without the main process being healthy.
**R-EXE-06.** Clock discipline: NTP-synced host, and all decisions timestamped from a monotonic
source. Log broker-reported timestamps alongside local ones and alarm on drift beyond a threshold.

### 9.5 Broker adapter interface

```python
class BrokerAdapter(Protocol):
    def connect(self) -> None: ...
    def account(self) -> AccountSnapshot: ...
    def positions(self) -> list[Position]: ...
    def working_orders(self) -> list[Order]: ...
    def place_bracket(self, group: BracketOrder) -> BrokerOrderIds: ...
    def modify(self, broker_order_id: str, changes: OrderChanges) -> None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def cancel_all(self) -> None: ...
    def flatten_all(self) -> None: ...
    def subscribe(self, instruments: list[Instrument]) -> Iterator[MarketEvent]: ...
    def day_trade_count_5d(self) -> int | None: ...   # None where not applicable
```

**R-ADP-01.** The paper adapter is a first-class citizen with a realistic fill model (spread, queue,
partial fills, rejects, latency). It is not a stub.
**R-ADP-02.** Every live adapter must implement broker-side rejection and error-code mapping into a
single internal error taxonomy. Unknown broker errors default to `HALT_NEW_ENTRIES`, never to retry.

### 9.6 Journal and analytics

**R-JRN-01.** Pre-trade note is captured at intent time, not after the outcome is known. The system
must make post-hoc editing of pre-trade notes impossible (append-only with timestamps).
This is the single most important anti-self-deception feature in the system.
**R-JRN-02.** Weekly report answers, in this order: how many trades of evidence exist; expectancy
with a confidence interval; whether the result is distinguishable from zero at the current sample
size; performance split by rule-followed vs deviated; the worst five trades and why; and whether any
kill criterion (Section 15) has triggered.
**R-JRN-03.** Live-vs-backtest divergence report: realised slippage vs modelled, fill rate, and
realised expectancy vs backtest expectancy, with an alarm when live expectancy falls outside the
backtest's confidence interval.
**R-JRN-04.** No metric is reported without a sample size next to it.

---

## 10. Risk engine (build this first)

The risk engine is a pure function: given account state, open risk, the day's history and a proposed
intent, it returns `Approved(qty)` or `Rejected(reason)`. It has no network calls, no I/O, and 100%
branch coverage. It is the only component with that standard.

| ID | Requirement | Acceptance criterion |
|----|-------------|----------------------|
| R-RISK-01 | Per-trade risk is a fixed fraction of equity, default 0.5%, hard max 1.0%. Position size is **derived** from stop distance, never chosen by the operator | Property test: for any equity and stop distance, computed qty risks no more than the configured fraction, rounded down to tick/contract granularity |
| R-RISK-02 | Daily loss limit, default 2% of equity (or 3R, whichever is smaller). On breach: cancel working orders, block new entries for the rest of the session. Existing positions keep their stops | Simulated session hitting the limit produces zero subsequent approvals |
| R-RISK-03 | Weekly loss limit, default 5%. On breach: block entries until a written review is logged | Entry attempt after breach without a review record is rejected with `WEEKLY_HALT` |
| R-RISK-04 | Max concurrent open risk across all positions, default 2% of equity | Approval refused when the proposed trade would push aggregate open risk over the cap |
| R-RISK-05 | Correlation cap: positions with correlation above a threshold count as one unit of risk. Two long index futures are one bet, not two | Test with two highly correlated instruments shows combined risk is capped, not doubled |
| R-RISK-06 | Max trades per session, default 5. Overtrading is the most common failure mode and is mechanically preventable | Sixth intent rejected with `TRADE_COUNT_CAP` |
| R-RISK-07 | No new entries in the first N minutes after the open or the last N before the close unless the strategy explicitly declares those windows tradeable and has backtested them separately | Rejection code `TIME_WINDOW` |
| R-RISK-08 | Leverage ceiling independent of the broker's. The system's cap binds first, always | Order exceeding internal cap rejected before any adapter call |
| R-RISK-09 | PDT guard: where the venue is FINRA-regulated and equity is below USD 25,000, enforce a maximum of 3 day trades per rolling 5 business days, with the fourth rejected and a warning at the third | Rolling-window test across business-day boundaries including holidays |
| R-RISK-10 | Scheduled-event blackout: no new entries inside a configurable window around known high-impact releases for the traded instrument, unless the strategy declares itself an event strategy | Rejection code `EVENT_BLACKOUT`, driven by an economic calendar table |
| R-RISK-11 | Kill-switch state is persisted. A restart does not clear a halt. Only an explicit operator command with a logged reason clears it | Process restart during halt remains halted |
| R-RISK-12 | Every rejection is written to `risk_event` with full context. Rejections are a first-class analytics input, not log noise | Weekly report includes rejection counts by reason |
| R-RISK-13 | Limits are loaded from a version-controlled config file. A limit change is a commit, with the previous value retained in history. No runtime loosening of a limit while positions are open | Attempt to widen a limit intraday is refused with `LIMIT_CHANGE_LOCKED` |

**R-RISK-14.** Risk config must express limits in R (risk units) as well as currency, and all
reporting is primarily in R. Currency amounts move with account size; R does not.

---

## 11. Research-to-live promotion gates

No strategy reaches live capital without passing every gate. Gates are enforced in code, not by
judgement. A strategy's stage is a database field, and the live runner refuses to load a strategy
not at stage `LIVE_APPROVED`.

| Gate | Requirement to pass |
|------|---------------------|
| G0 Hypothesis | Written, falsifiable statement of why this works: what inefficiency, whose behaviour creates it, why it persists. One paragraph, stored with the strategy. "The indicator crossed" is not a hypothesis |
| G1 Backtest | Positive expectancy with costs on, minimum 200 trades, across at least two distinct market regimes. Parameter count declared; multiple-testing haircut applied |
| G2 Robustness | Performance degrades smoothly, not cliff-like, under parameter perturbation of plus or minus 20%. Survives a plus 50% slippage and cost stress test. Survives removal of the best 5% of trades |
| G3 Walk-forward | Out-of-sample expectancy at least 50% of in-sample, same sign, on rolling windows |
| G4 Paper | Minimum 30 trading days or 50 trades live-paper, with realised slippage within 1.5x of modelled |
| G5 Micro-live | Minimum 20 trades at 0.1% risk per trade. Live expectancy inside the backtest confidence interval |
| G6 Scale-up | Risk increases in steps (0.1%, 0.25%, 0.5%) with a minimum 20 trades and no limit breach at each step before the next |

**R-GATE-01.** The promotion gate is a single CLI command that runs all checks and either advances
the stage or prints exactly which gate failed and by how much.
*Acceptance:* a strategy that fails G2 cannot be advanced by any means short of a code change with
a commit.

---

## 12. Technology stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12+, `uv` for env and deps | Ecosystem, and the engine below is Python-native |
| Engine | `nautilus_trader` | Event-driven, backtest/live parity, IB adapter, actively developed |
| Sweeps | `vectorbt` | Fast parameter exploration, research only |
| Broker (live) | `ib_async` via nautilus IB adapter, or IG REST/streaming via `trading-ig` | Per Section 6 |
| Research store | DuckDB plus Parquet | Fast local analytics, no server to run |
| Operational store | Postgres (TimescaleDB optional) | Transactions for order state |
| Queue | Redis Streams | Simple, adequate, observable |
| Validation | `pydantic` v2 for every boundary object | Intents, orders and fills are validated data, not dicts |
| Tests | `pytest`, `hypothesis` for the risk engine | Property-based testing is the right tool for sizing maths |
| Lint/type | `ruff`, `mypy --strict` on core and risk packages | The risk engine must be type-sound |
| Deploy | Single VPS near the venue, or local machine with a UPS. Docker Compose | Latency is not the edge; reliability is |
| Observability | Structured JSON logs, Prometheus metrics, alerts to phone | An unmonitored system holding positions is the failure mode that costs the most |
| Secrets | Never in the repo. `.env` excluded, or a secrets manager | Credentials grant the ability to lose money |

**R-TECH-01.** Decimal or integer minor units for all prices and money. Floats for PnL are a defect.
*Acceptance:* a lint rule or test rejects `float` in money-typed fields.
**R-TECH-02.** All times stored UTC, displayed Europe/London, with explicit DST-safe session
boundary handling for venue calendars.

---

## 13. Build phases

Each phase ends with a demo and a decision point. Do not start the next phase before the current
one's definition of done is met.

**Phase 0: Skeleton and risk engine (1 to 2 weeks)**
Repo, env, CI, schema, pydantic domain objects, risk engine with full test coverage, paper adapter
with a realistic fill model, CLI.
*Done when:* the risk engine has 100% branch coverage, property tests pass, and an end-to-end
paper trade can be placed and journalled with zero live credentials in the system.

**Phase 1: Data and research (2 to 3 weeks)**
Ingest, point-in-time store, cost model, nautilus integration, backtest runner, reproducible run
records, metrics report with confidence intervals and multiple-testing haircut.
*Done when:* a deliberately random strategy is correctly reported as having no edge, and a
look-ahead bug is caught by the guard test.

**Phase 2: Scanner, planner and journal (1 to 2 weeks)**
Session plan generation and freezing, candidate ranking, append-only journal, weekly report.
*Done when:* a full week of paper sessions is planned, executed, journalled and reported without
manual database edits.

**Phase 3: Live adapter and reconciliation (2 to 3 weeks)**
One live adapter, atomic brackets, server-side stops, idempotency, reconciliation, kill switch,
monitoring and alerting.
*Done when:* the process can be killed at any point in the order lifecycle without producing an
unprotected position or a duplicate order, demonstrated by a chaos test.

**Phase 4: Promotion gates and micro-live (ongoing)**
Gate automation, then the first strategy through G0 to G5 at 0.1% risk.
*Done when:* 20 live micro trades are complete and the live-vs-backtest divergence report is green.

**Phase 5: Optional automation (only if Phase 4 produced evidence of an edge)**
Supervisor process, scheduled autonomous execution within risk limits, with a heartbeat that
flattens on supervisor loss.

---

## 14. Non-functional requirements

- **R-NFR-01.** Mean time to detect a failed component: under 60 seconds, via heartbeat alert.
- **R-NFR-02.** Recovery: full state reconstructed from the database and broker on restart. No
  in-memory-only state that matters.
- **R-NFR-03.** Every order decision traceable end to end from journal note to broker execution ID.
- **R-NFR-04.** Audit log append-only, with no code path that deletes a `fill`, `trade` or
  `risk_event` row.
- **R-NFR-05.** Backups: nightly off-machine, restore tested quarterly.
- **R-NFR-06.** Backtest of one year of 1-minute bars on one instrument completes in under 60
  seconds, so that research iteration is not the bottleneck.

---

## 15. Kill criteria

Written now, while it is cheap, because these are the decisions that are impossible to make honestly
mid-drawdown. The system evaluates and reports these automatically.

- **K1.** Cumulative live loss reaches a pre-committed absolute figure: stop entirely, review from
  scratch. The figure goes in config before the first live trade.
- **K2.** 100 live trades with expectancy not distinguishable from zero after costs: the strategy has
  no demonstrated edge. Retire it. Do not add parameters to it.
- **K3.** Three consecutive weekly loss-limit breaches: halt until the process, not the strategy, is
  reviewed.
- **K4.** Rule-following rate below 80% over 50 trades: the problem is not the system. Stop trading
  live and go back to paper.
- **K5.** Live slippage persistently above 2x modelled: the strategy is not viable at this venue or
  this size, regardless of backtest quality.
- **K6.** Twelve months elapsed with no strategy past gate G5: the project has produced no edge.
  Treat the time as the cost of finding that out, and stop.

**R-KILL-01.** Kill criteria are config, evaluated in the weekly report, and cannot be edited while
any kill criterion is currently triggered.

---

## 16. Open decisions required from the operator

These block specific phases. Each needs an answer before the phase it gates.

| # | Decision | Blocks | Default if unanswered |
|---|----------|--------|-----------------------|
| D1 | Market and instrument (Section 6, Routes A/B/C) | Phase 1 | Route B, CME micro futures via IBKR |
| D2 | Account size and the absolute figure for kill criterion K1 | Phase 0 config | None. Must be answered |
| D3 | Session hours to trade, given Europe/London (US open is 14:30 GMT) | Phase 2 | US cash session only |
| D4 | Per-trade risk fraction and daily loss limit | Phase 0 | 0.5% and 2% |
| D5 | Spread betting (tax-free, no loss relief) vs CFD/futures (taxable, loss-relievable) | Phase 3 | Futures. Loss relief matters more than tax exemption on an unproven edge |
| D6 | Disconnect policy: flatten vs rest on broker stops | Phase 3 | Rest on broker stops, plus flatten on supervisor loss |
| D7 | Is there an existing strategy hypothesis, or does the system need to find one | Phase 1 scope | Assume none. Build the research path |
| D8 | Market data budget per month | Phase 1 | Start on a USD 30/mo tier, upgrade when research is data-constrained |
| D9 | Hosting: local machine vs VPS | Phase 3 | VPS, for uptime |

---

## 17. What this system deliberately will not do for you

Stated plainly so it is not discovered later as a disappointment:

1. It will not find an edge. It will test the ones you propose, honestly, which mostly means telling
   you they do not work.
2. It will not make you faster than the market. Any strategy depending on reacting first is out of
   reach from this stack.
3. It will not make day trading a positive-expectancy activity. It will stop you destroying capital
   while you find out whether you are in the small minority for whom it is.
4. It will not stop you overriding it. It will record every override and show you, in the weekly
   report, what the overrides cost.

---

## Appendix A: Sources

- Chague, F., De-Losso, R., Giovannetti, B. "Day Trading for a Living?" (FGV EESP TD 525, 2020).
  SSRN 3423101.
- Barber, B., Lee, Y-T., Liu, Y-J., Odean, T. "Do Individual Day Traders Make Money? Evidence from
  Taiwan" (2004), and subsequent work on day trader learning.
- FCA PS19/18, "Restricting contract for difference products sold to retail clients" (leverage
  limits 30:1 to 2:1, margin close-out, negative balance protection).
- FCA, "FCA bans the sale of crypto-derivatives to retail consumers" (in force 6 January 2021);
  retail access to crypto ETNs reopened 8 October 2025, derivatives ban retained.
- HMRC / TCGA 1992 s.51 on betting winnings; CGT annual exempt amount GBP 3,000 and rates 18%/24%
  for 2026/27.
- FINRA pattern day trader rule (four or more day trades in five business days, USD 25,000 minimum
  equity) and Interactive Brokers UK margin documentation on PDT application.
- Interactive Brokers TWS API and Client Portal Web API documentation.
- IG Labs REST and streaming API documentation (retail, approximately 40 trade requests per minute).
- nautilus_trader documentation on backtest/live parity and the Interactive Brokers adapter.
- Massive (formerly Polygon.io) and Databento published pricing, 2026. Verify before committing.

**Every figure above that concerns pricing, leverage, tax or broker policy must be re-verified
against the primary source before it is relied upon. Rules change, and this document will go stale.**
