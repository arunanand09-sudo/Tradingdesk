# Tradingdesk

Risk-first intraday trading system, built from [`docs/PRD.md`](docs/PRD.md).
This is **Phase 0** of the PRD plus an **IG adapter** (spread bet or CFD account, demo by default).

It does **not** trade on its own. The PRD puts unattended automation in Phase 5, only after a
strategy has passed the promotion gates (Section 11). Today the operator submits each trade
through the CLI, and the system sizes it, risk-checks it, places it at IG with the stop attached,
and journals it.

## What is built

| Area | Status | PRD refs |
|---|---|---|
| Risk engine: pure function, 100% branch coverage, property-tested sizing | Done | R-RISK-01 to 14 |
| Paper broker: spread, slippage, gaps through stops, trade-through limits, partial fills, rejects, timeouts, persisted resting stops | Done | R-ADP-01, R-EXE-04 |
| IG REST adapter: login, account switch, positions, working orders, market entry with attached stop/limit, idempotency via `dealReference`, confirm polling, close/cancel, error mapping, rate throttling | Done, tested against a fake IG. **Not yet run against the real IG demo** | R-VENUE-01, R-ADP-02, R-EXE-01/02 |
| Single gateway to broker, enforced by a test | Done | R-ARCH-01 |
| Session plan: freeze, OFF_PLAN rejection with logged override, candidate cap | Done (manual entry; no scanner yet) | R-SCAN-02/03 |
| Append-only journal, intents, trades, risk events (DB triggers) | Done | R-JRN-01, R-NFR-04 |
| Mandatory rule-followed / deviation on close | Done | R-DATA-07 |
| Reconciliation: unknown broker positions or missing stops halt new entries | Done | R-EXE-03 |
| Kill switch: flag file + cancel all + optional flatten; halt survives restart | Done | R-EXE-05, R-RISK-11 |
| Weekly report: expectancy with CI, rule split, worst 5, rejections, K2/K4 | Done (basic) | R-JRN-02, R-JRN-04 |
| Decimal for all money; DST-safe session times | Done | R-TECH-01/02 |
| Data layer, backtester (nautilus_trader), walk-forward, promotion gates | **Not started** (Phase 1, 4) | Sections 8, 9.1, 11 |
| Lightstreamer streaming, monitoring/alerts, automated strategy runner | **Not started** (Phase 3, 5) | |

## Setup

```bash
uv venv && . .venv/bin/activate     # or python -m venv .venv
uv pip install -e ".[dev]"
pytest -q                            # 76 tests, no credentials needed
```

### Connect to IG (demo first)

1. Create an API key at ig.com: My IG > Settings > API keys. Use a **demo** account key.
2. `cp .env.example .env`, fill in key, username, password and (optionally) account ID.
3. `set -a; . ./.env; set +a`
4. Check every epic in `config/desk.toml` exists on your account and edit the registry.
5. Set `kill_k1_absolute_loss_gbp` in `config/desk.toml` (PRD decision D2). It is a placeholder.

Live needs `IG_ENV=live` **and** `TDESK_ALLOW_LIVE=yes`. Do not set those until a strategy
has a paper record (gate G4).

## Daily use

```bash
tdesk plan add IX.D.FTSE.DAILY.IP --setup ORB --trigger "break 8000" \
      --stop 7980 --target 8040 --thesis "..." --invalidation "..."
tdesk plan freeze                       # nothing off this list without --override
tdesk event --ts 2026-11-09T13:30Z --name "US CPI"     # blackout window
tdesk submit IX.D.FTSE.DAILY.IP buy --entry 8000 --stop 7980 --target 8040 \
      --reason "ORB long" --note "why I am taking this, written before the outcome"
tdesk status
tdesk reconcile
tdesk close <intent-id> --entry-fill 8000.6 --exit 8040 --rule yes --note "..."
tdesk report
tdesk kill --flatten                    # cancel everything, flatten, halt
tdesk clear-halt --reason "..."         # logged
```

Use `--broker paper` (default) or `--broker ig`. On paper, move the market with
`tdesk price <epic> <mid>`.

## Deviations from the PRD

- Operational store is SQLite, not Postgres. Plain SQL, so moving is a driver change.
- Instruments are defined in a TOML registry rather than an `instrument` table.
- PRD recommends IBKR micro futures (Route B) first. This build targets IG (Route C) at the
  operator's request. The `BrokerAdapter` interface keeps an IBKR adapter possible later.
