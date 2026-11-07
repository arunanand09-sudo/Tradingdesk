"""Weekly report (R-JRN-02, R-RISK-12, R-JRN-04, Section 15 kill criteria)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from tradingdesk.store import Store

Z95 = Decimal("1.96")


@dataclass(frozen=True)
class Stats:
    n: int
    expectancy_r: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None

    @property
    def distinguishable_from_zero(self) -> bool:
        return self.ci_low is not None and self.ci_high is not None and (
            self.ci_low > 0 or self.ci_high < 0)

    def line(self) -> str:
        if self.n == 0 or self.expectancy_r is None:
            return "n=0, no data"
        if self.ci_low is None:
            return f"n={self.n}, expectancy {self.expectancy_r:+.2f}R (CI needs n>=2)"
        return (f"n={self.n}, expectancy {self.expectancy_r:+.2f}R, "
                f"95% CI [{self.ci_low:+.2f}, {self.ci_high:+.2f}]")


def stats(rs: list[Decimal]) -> Stats:
    n = len(rs)
    if n == 0:
        return Stats(0, None, None, None)
    mean = sum(rs, Decimal("0")) / n
    if n < 2:
        return Stats(n, mean, None, None)
    sd = statistics.stdev(rs)
    half = Z95 * Decimal(sd) / Decimal(n).sqrt()
    return Stats(n, mean, mean - half, mean + half)


def weekly_report(store: Store, broker: str) -> str:
    trades = store.q("SELECT t.*, i.symbol, i.reason FROM trade t JOIN intent i ON i.id=t.intent_id"
                     " WHERE t.broker=? ORDER BY t.id", [broker])
    rs = [Decimal(t["r_multiple"]) for t in trades]
    s = stats(rs)
    followed = stats([Decimal(t["r_multiple"]) for t in trades if t["rule_followed"]])
    deviated = stats([Decimal(t["r_multiple"]) for t in trades if not t["rule_followed"]])
    rej = store.q("SELECT kind, COUNT(*) n FROM risk_event WHERE action_taken='REJECTED'"
                  " GROUP BY kind ORDER BY n DESC")
    worst = sorted(trades, key=lambda t: Decimal(t["r_multiple"]))[:5]

    out = [f"# Weekly report ({broker})", "",
           f"1. Trades of evidence: {s.n}",
           f"2. Expectancy: {s.line()}",
           "3. Distinguishable from zero: " + ("YES" if s.distinguishable_from_zero else
                                               "NO (not enough evidence of an edge)"),
           f"4. Rule followed: {followed.line()}",
           f"   Rule deviated: {deviated.line()}",
           "5. Worst five trades:"]
    for t in worst:
        out.append(f"   {t['symbol']} {Decimal(t['r_multiple']):+.2f}R  "
                   f"deviation: {t['deviation_reason'] or '-'}")
    if not worst:
        out.append("   none")
    out.append("6. Kill criteria:")
    k2 = s.n >= 100 and not s.distinguishable_from_zero
    out.append(f"   K2 (100 trades, no edge): {'TRIGGERED' if k2 else 'ok'} (n={s.n})")
    last50 = trades[-50:]
    rate = (Decimal(sum(t["rule_followed"] for t in last50)) / len(last50)) if last50 else None
    k4 = len(last50) >= 50 and rate is not None and rate < Decimal("0.8")
    out.append(f"   K4 (rule-following <80% over 50): {'TRIGGERED' if k4 else 'ok'} "
               f"(rate={'n/a' if rate is None else f'{rate:.0%}'}, n={len(last50)})")
    out.append("")
    out.append("Rejections by reason:")
    total = sum(r["n"] for r in rej)
    for r in rej:
        out.append(f"   {r['kind']}: {r['n']} (of {total})")
    if not rej:
        out.append("   none")
    return "\n".join(out)
