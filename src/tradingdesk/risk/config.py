"""Risk limits, loaded from a version-controlled TOML file (R-RISK-13)."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tradingdesk.domain import DisconnectPolicy

HARD_MAX_PER_TRADE_RISK_PCT = Decimal("1.0")


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # R-RISK-01
    per_trade_risk_pct: Decimal = Decimal("0.5")
    # R-RISK-02 (whichever is smaller)
    daily_loss_pct: Decimal = Decimal("2.0")
    daily_loss_r: Decimal = Decimal("3")
    # R-RISK-03
    weekly_loss_pct: Decimal = Decimal("5.0")
    weekly_loss_r: Decimal = Decimal("10")
    # R-RISK-04
    max_open_risk_pct: Decimal = Decimal("2.0")
    # R-RISK-06
    max_trades_per_session: int = 5
    # R-RISK-07
    no_entry_after_open_min: int = 15
    no_entry_before_close_min: int = 15
    open_close_windows_tradeable: bool = False
    # R-RISK-08: notional / equity. Binds before the broker's own cap.
    max_leverage: Decimal = Decimal("5")
    # R-RISK-09
    pdt_equity_threshold_usd: Decimal = Decimal("25000")
    pdt_max_day_trades_5d: int = 3
    gbp_usd: Decimal = Decimal("1.30")
    # R-RISK-10
    event_blackout_min: int = 5
    is_event_strategy: bool = False
    # R-SCAN-03
    max_candidates: int = 5
    # R-EXE-04: required, no default.
    disconnect_policy: DisconnectPolicy
    # K1 (D2): required, no default. Absolute cumulative live loss that stops everything.
    kill_k1_absolute_loss_gbp: Decimal = Field(gt=0)

    @field_validator("per_trade_risk_pct")
    @classmethod
    def _cap_per_trade(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > HARD_MAX_PER_TRADE_RISK_PCT:
            raise ValueError(f"per_trade_risk_pct must be in (0, {HARD_MAX_PER_TRADE_RISK_PCT}]")
        return v

    @classmethod
    def load(cls, path: str | Path) -> RiskConfig:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        data = raw.get("risk", raw)
        return cls.model_validate({k: _dec(v) for k, v in data.items()})


def _dec(v: object) -> object:
    # TOML floats would be lossy; convert via str so 0.5 stays exactly 0.5.
    if isinstance(v, float):
        return Decimal(str(v))
    return v


# Limits where a larger number means looser risk.
_LOOSER_IF_HIGHER = (
    "per_trade_risk_pct",
    "daily_loss_pct",
    "daily_loss_r",
    "weekly_loss_pct",
    "weekly_loss_r",
    "max_open_risk_pct",
    "max_trades_per_session",
    "max_leverage",
    "pdt_max_day_trades_5d",
    "kill_k1_absolute_loss_gbp",
    "max_candidates",
)
# Limits where a smaller number means looser risk.
_LOOSER_IF_LOWER = ("no_entry_after_open_min", "no_entry_before_close_min", "event_blackout_min")
# Booleans where True means looser.
_LOOSER_IF_TRUE = ("open_close_windows_tradeable", "is_event_strategy")


def loosened_fields(old: RiskConfig, new: RiskConfig) -> list[str]:
    """Names of limits that `new` loosens relative to `old`."""
    out: list[str] = []
    for f in _LOOSER_IF_HIGHER:
        if getattr(new, f) > getattr(old, f):
            out.append(f)
    for f in _LOOSER_IF_LOWER:
        if getattr(new, f) < getattr(old, f):
            out.append(f)
    for f in _LOOSER_IF_TRUE:
        if getattr(new, f) and not getattr(old, f):
            out.append(f)
    return out
