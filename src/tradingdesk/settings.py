"""Desk settings: risk limits, session hours and the instrument registry, from one TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingdesk.domain import Instrument
from tradingdesk.risk.config import RiskConfig, _dec

UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class SessionHours:
    """Session in the venue's local time; converted per date, so DST is handled (R-TECH-02)."""

    tz: str = "Europe/London"
    open_local: time = time(8, 0)
    close_local: time = time(16, 30)

    def bounds_utc(self, d: date) -> tuple[datetime, datetime]:
        z = ZoneInfo(self.tz)
        o = datetime.combine(d, self.open_local, tzinfo=z).astimezone(UTC)
        c = datetime.combine(d, self.close_local, tzinfo=z).astimezone(UTC)
        if d.weekday() >= 5:  # no weekend session: zero-length window
            return o, o
        return o, c


@dataclass(frozen=True)
class Settings:
    risk: RiskConfig
    session: SessionHours
    instruments: dict[str, Instrument]
    path: Path

    def instrument(self, symbol: str) -> Instrument:
        if symbol not in self.instruments:
            raise KeyError(f"{symbol} is not in the instrument registry ({self.path})")
        return self.instruments[symbol]

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        p = Path(path)
        raw = tomllib.loads(p.read_text())
        risk = RiskConfig.model_validate({k: _dec(v) for k, v in raw["risk"].items()})
        s = raw.get("session", {})
        session = SessionHours(
            tz=s.get("tz", "Europe/London"),
            open_local=time.fromisoformat(s.get("open", "08:00")),
            close_local=time.fromisoformat(s.get("close", "16:30")),
        )
        instruments = {}
        for sym, spec in raw.get("instruments", {}).items():
            fields = {k: (Decimal(str(v)) if isinstance(v, float | int) and k != "finra_regulated"
                          else v) for k, v in spec.items()}
            instruments[sym] = Instrument(symbol=sym, **fields)
        return cls(risk=risk, session=session, instruments=instruments, path=p)
