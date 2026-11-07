"""Operational store. SQLite for Phase 0 (single file, no server).

Audit tables are append-only, enforced by triggers (R-NFR-04, R-JRN-01).
The PRD names Postgres for the operational store; the SQL here is kept plain so
moving is a driver change, not a redesign.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_plan(
  id INTEGER PRIMARY KEY, trade_date TEXT NOT NULL UNIQUE, generated_at TEXT NOT NULL,
  account_equity TEXT, notes TEXT, frozen_at TEXT);
CREATE TABLE IF NOT EXISTS candidate(
  id INTEGER PRIMARY KEY, session_plan_id INTEGER NOT NULL REFERENCES session_plan(id),
  symbol TEXT NOT NULL, rank INTEGER NOT NULL, setup_type TEXT, entry_trigger TEXT,
  stop_level TEXT, target_level TEXT, thesis TEXT, invalidation TEXT);
CREATE TABLE IF NOT EXISTS intent(
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, order_type TEXT NOT NULL, entry_price TEXT NOT NULL,
  stop_price TEXT NOT NULL, target_price TEXT NOT NULL, qty TEXT, risk_gbp TEXT,
  r_value_gbp TEXT, reason TEXT NOT NULL, decision TEXT NOT NULL, rejected_code TEXT,
  rejected_detail TEXT, override_reason TEXT, broker TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broker_order(
  id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intent(id),
  client_order_id TEXT NOT NULL UNIQUE, deal_id TEXT, status TEXT NOT NULL,
  submitted_at TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS trade(
  id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE REFERENCES intent(id),
  closed_at TEXT NOT NULL, entry_fill TEXT NOT NULL, exit_price TEXT NOT NULL,
  pnl_gbp TEXT NOT NULL, r_multiple TEXT NOT NULL, rule_followed INTEGER NOT NULL,
  deviation_reason TEXT, broker TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, ts_utc TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('PRE','POST')), note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_event(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  detail TEXT NOT NULL, action_taken TEXT);
CREATE TABLE IF NOT EXISTS halt_log(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN
  ('HALT','CLEAR')), scope TEXT NOT NULL, trade_date TEXT, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weekly_review(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, week_start TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_snapshot(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, trade_date TEXT NOT NULL, equity TEXT NOT NULL,
  cash TEXT NOT NULL, margin_used TEXT, broker TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS economic_event(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, symbol TEXT, name TEXT NOT NULL);
"""

APPEND_ONLY = ("intent", "trade", "journal", "risk_event", "halt_log", "weekly_review",
               "account_snapshot")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for t in APPEND_ONLY:
            self.db.executescript(f"""
              CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
              BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
              CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
              BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;""")
        # A frozen plan cannot gain candidates (R-SCAN-02).
        self.db.executescript("""
          CREATE TRIGGER IF NOT EXISTS candidate_frozen BEFORE INSERT ON candidate
          WHEN (SELECT frozen_at FROM session_plan WHERE id = NEW.session_plan_id) IS NOT NULL
          BEGIN SELECT RAISE(ABORT, 'session plan is frozen'); END;
          CREATE TRIGGER IF NOT EXISTS plan_refreeze BEFORE UPDATE ON session_plan
          WHEN OLD.frozen_at IS NOT NULL
          BEGIN SELECT RAISE(ABORT, 'session plan is frozen'); END;""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, tuple(args)))

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self.db.execute(sql, tuple(args))
        self.db.commit()
        return cur.lastrowid or 0

    # ---- plans ------------------------------------------------------------------
    def plan_for(self, d: date) -> sqlite3.Row | None:
        rows = self.q("SELECT * FROM session_plan WHERE trade_date=?", [d.isoformat()])
        return rows[0] if rows else None

    def ensure_plan(self, d: date, equity: Decimal) -> int:
        p = self.plan_for(d)
        if p:
            return int(p["id"])
        return self.x("INSERT INTO session_plan(trade_date, generated_at, account_equity)"
                      " VALUES(?,?,?)", [d.isoformat(), _now(), str(equity)])

    def add_candidate(self, plan_id: int, symbol: str, **kw: str) -> int:
        rank = len(self.q("SELECT 1 FROM candidate WHERE session_plan_id=?", [plan_id])) + 1
        return self.x(
            "INSERT INTO candidate(session_plan_id, symbol, rank, setup_type, entry_trigger,"
            " stop_level, target_level, thesis, invalidation) VALUES(?,?,?,?,?,?,?,?,?)",
            [plan_id, symbol, rank, kw.get("setup_type"), kw.get("entry_trigger"),
             kw.get("stop_level"), kw.get("target_level"), kw.get("thesis"),
             kw.get("invalidation")])

    def freeze_plan(self, plan_id: int) -> None:
        self.x("UPDATE session_plan SET frozen_at=? WHERE id=?", [_now(), plan_id])

    def frozen_symbols(self, d: date) -> frozenset[str]:
        p = self.plan_for(d)
        if not p or not p["frozen_at"]:
            return frozenset()
        return frozenset(r["symbol"] for r in
                         self.q("SELECT symbol FROM candidate WHERE session_plan_id=?", [p["id"]]))

    # ---- events / halts ------------------------------------------------------------
    def risk_event(self, kind: str, severity: str, detail: dict[str, Any] | str,
                   action: str | None = None) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        self.x("INSERT INTO risk_event(ts_utc, kind, severity, detail, action_taken)"
               " VALUES(?,?,?,?,?)", [_now(), kind, severity, text, action])

    def halt(self, scope: str, reason: str, trade_date: date | None = None) -> None:
        self.x("INSERT INTO halt_log(ts_utc, action, scope, trade_date, reason) VALUES(?,?,?,?,?)",
               [_now(), "HALT", scope, trade_date.isoformat() if trade_date else None, reason])

    def clear_halt(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("a reason is required to clear a halt")
        self.x("INSERT INTO halt_log(ts_utc, action, scope, reason) VALUES(?,?,?,?)",
               [_now(), "CLEAR", "GLOBAL", reason])

    def is_halted(self, today: date) -> tuple[bool, str]:
        """Global halt persists until cleared. A DAILY halt lasts for its trade date."""
        rows = self.q("SELECT * FROM halt_log ORDER BY id")
        halted, why = False, ""
        for r in rows:
            if r["action"] == "CLEAR":
                halted, why = False, ""
            elif r["scope"] == "GLOBAL" or r["trade_date"] == today.isoformat():
                halted, why = True, f"{r['scope']}: {r['reason']}"
        return halted, why

    def events_for(self, symbol: str) -> tuple[datetime, ...]:
        rows = self.q("SELECT ts_utc FROM economic_event WHERE symbol IS NULL OR symbol=?",
                      [symbol])
        return tuple(datetime.fromisoformat(r["ts_utc"]) for r in rows)

    def snapshot(self, d: date, equity: Decimal, cash: Decimal, margin: Decimal,
                 broker: str) -> None:
        self.x("INSERT INTO account_snapshot(ts_utc, trade_date, equity, cash, margin_used, broker)"
               " VALUES(?,?,?,?,?,?)", [_now(), d.isoformat(), str(equity), str(cash),
                                       str(margin), broker])

    def first_equity_since(self, d: date, broker: str) -> Decimal | None:
        rows = self.q("SELECT equity FROM account_snapshot WHERE trade_date>=? AND broker=?"
                      " ORDER BY id LIMIT 1", [d.isoformat(), broker])
        return Decimal(rows[0]["equity"]) if rows else None
