"""CLI smoke test on the paper broker. Uses the real config, a temp data dir."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradingdesk import cli
from tradingdesk.analytics import stats

ROOT = Path(__file__).resolve().parents[1]
FTSE = "IX.D.FTSE.DAILY.IP"


def run(tmp: Path, *args: str) -> int:
    return cli.main(["--config", str(ROOT / "config/desk.toml"), "--data", str(tmp), *args])


def test_cli_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                  monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 11, 9, 11, 0, tzinfo=UTC)
    import tradingdesk.execution.gateway as g
    orig = g.Gateway.open.__func__  # type: ignore[attr-defined]
    monkeypatch.setattr(g.Gateway, "open", classmethod(
        lambda cls, s, d, b, clock=None: orig(cls, s, d, b, lambda: fixed)))

    assert run(tmp_path, "price", FTSE, "8000") == 0
    assert run(tmp_path, "plan", "add", FTSE, "--setup", "ORB", "--trigger", "break 8000",
               "--stop", "7980", "--target", "8040", "--thesis", "t", "--invalidation", "i") == 0
    assert run(tmp_path, "submit", FTSE, "buy", "--entry", "8000", "--stop", "7980",
               "--target", "8040", "--reason", "r", "--note", "n") == 1  # plan not frozen
    assert run(tmp_path, "plan", "freeze") == 0
    assert run(tmp_path, "submit", FTSE, "buy", "--entry", "8000", "--stop", "7980",
               "--target", "8040", "--reason", "r", "--note", "n") == 0
    out = capsys.readouterr().out
    assert "PLACED" in out and "OFF_PLAN" not in out
    intent_id = out.split("intent=")[-1].strip()
    assert run(tmp_path, "status") == 0
    assert run(tmp_path, "reconcile") == 0
    assert run(tmp_path, "price", FTSE, "8050") == 0
    assert run(tmp_path, "close", intent_id, "--entry-fill", "8000.5", "--exit", "8040",
               "--rule", "yes", "--note", "clean") == 0
    assert run(tmp_path, "event", "--ts", "2026-11-09T13:30:00+00:00", "--name", "NFP") == 0
    assert run(tmp_path, "review", "--text", "fine") == 0
    assert run(tmp_path, "report") == 0
    assert "Trades of evidence: 1" in capsys.readouterr().out
    assert run(tmp_path, "kill", "--flatten") == 0
    assert (tmp_path / "KILL").exists()
    assert run(tmp_path, "clear-halt", "--reason", "ok") == 0
    assert not (tmp_path / "KILL").exists()


def test_random_strategy_shows_no_edge() -> None:
    """A coin-flip 1R:1R strategy must not look like an edge (R-RES-06 spirit)."""
    import random
    from decimal import Decimal
    rng = random.Random(7)
    rs = [Decimal("1") if rng.random() < 0.5 else Decimal("-1.05") for _ in range(200)]
    assert not stats(rs).distinguishable_from_zero or stats(rs).expectancy_r < 0  # type: ignore[operator]


def test_session_hours_dst_and_weekend() -> None:
    from datetime import date

    from tradingdesk.settings import SessionHours
    us = SessionHours(tz="Europe/London", open_local=__import__("datetime").time(14, 30),
                      close_local=__import__("datetime").time(21, 0))
    summer_open, _ = us.bounds_utc(date(2026, 7, 1))
    winter_open, _ = us.bounds_utc(date(2026, 12, 1))
    assert summer_open.hour == 13 and winter_open.hour == 14  # BST vs GMT
    o, c = us.bounds_utc(date(2026, 11, 7))  # Saturday
    assert o == c
