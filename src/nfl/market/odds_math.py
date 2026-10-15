"""Pure odds arithmetic for Phase 4 (standard library only)."""
from __future__ import annotations

import statistics
from collections import Counter


def implied_prob(american: float | None) -> float | None:
    """Raw implied probability of an American price (vig included)."""
    if american is None:
        return None
    a = float(american)
    if a != a:  # NaN
        return None
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def decimal_odds(american: float) -> float:
    a = float(american)
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / -a)


def devig_over(over_price: float, under_price: float) -> float:
    """No-vig P(Over) for ONE book's two-way market at ONE total number."""
    po, pu = implied_prob(over_price), implied_prob(under_price)
    return po / (po + pu)


def cents(american: float) -> float:
    """Continuous 'cents' scale: -110 -> -110, -100/+100 -> -100, +105 -> -95."""
    a = float(american)
    return a if a <= -100 else a - 200.0


def price_better(a: float, b: float) -> bool:
    """For the bettor, is American price a better than b?"""
    return cents(a) > cents(b)


def canonical_line(lines: list[float]) -> float | None:
    """Most common posted line; ties -> closest to the median; then the lower number.
    Always returns a number that at least one book actually posted."""
    if not lines:
        return None
    counts = Counter(float(x) for x in lines)
    top = max(counts.values())
    modes = [ln for ln, c in counts.items() if c == top]
    med = statistics.median(float(x) for x in lines)
    return sorted(modes, key=lambda ln: (abs(ln - med), ln))[0]


def median_price(prices: list[float]) -> float | None:
    """Median on the continuous cents scale, mapped back to American odds."""
    if not prices:
        return None
    m = statistics.median(cents(p) for p in prices)
    return m if m <= -100 else m + 200.0
