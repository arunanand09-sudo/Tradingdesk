"""R-ARCH-01: only execution.gateway may import from tradingdesk.adapters.

R-EXE-01: an Intent cannot be constructed without a stop.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

SRC = Path(__file__).resolve().parents[1] / "src" / "tradingdesk"
ALLOWED = {SRC / "execution" / "gateway.py"}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_only_gateway_imports_adapters() -> None:
    offenders = []
    for f in SRC.rglob("*.py"):
        if f in ALLOWED or (SRC / "adapters") in f.parents:
            continue
        if any(m.startswith("tradingdesk.adapters") for m in _imports(f)):
            offenders.append(str(f.relative_to(SRC)))
    assert offenders == [], f"modules bypassing the risk gateway: {offenders}"


def test_risk_engine_has_no_io() -> None:
    banned = {"requests", "socket", "sqlite3", "os", "time", "tradingdesk.store",
              "tradingdesk.adapters", "urllib"}
    for f in (SRC / "risk").glob("*.py"):
        mods = _imports(f)
        assert not any(m.split(".")[0] in banned or m in banned for m in mods), (f, mods)


def test_intent_without_stop_is_impossible() -> None:
    from decimal import Decimal

    from tradingdesk.domain import Instrument, Intent, Side
    with pytest.raises(ValidationError):
        Intent(instrument=Instrument(symbol="X"), side=Side.BUY,  # type: ignore[call-arg]
               entry_price=Decimal("1"), target_price=Decimal("2"), reason="r",
               pre_trade_note="n")
