"""Broker adapters. Import only from tradingdesk.execution.gateway (R-ARCH-01)."""

from __future__ import annotations

from pathlib import Path

from tradingdesk.adapters.base import BrokerAdapter, BrokerError, BrokerErrorKind


def make_adapter(name: str, data_dir: Path) -> BrokerAdapter:
    if name == "paper":
        from tradingdesk.adapters.paper import PaperAdapter

        return PaperAdapter(state_path=data_dir / "paper_broker.json")
    if name == "ig":
        from tradingdesk.adapters.ig import IGAdapter, IGCredentials

        return IGAdapter(IGCredentials.from_env())
    raise ValueError(f"unknown broker '{name}' (expected paper or ig)")


__all__ = ["BrokerAdapter", "BrokerError", "BrokerErrorKind", "make_adapter"]
