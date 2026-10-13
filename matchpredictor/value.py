"""Value detection: vig removal, edge, and fractional Kelly staking."""
from __future__ import annotations


def remove_vig(decimal_odds: list[float]) -> list[float]:
    """Multiplicative (proportional) de-vig of a complete 2-way or 3-way market."""
    if any(o <= 1.0 for o in decimal_odds):
        raise ValueError("decimal odds must be > 1")
    implied = [1.0 / o for o in decimal_odds]
    total = sum(implied)
    return [p / total for p in implied]


def overround(decimal_odds: list[float]) -> float:
    return sum(1.0 / o for o in decimal_odds) - 1.0


def expected_value(p: float, decimal_odds: float) -> float:
    return p * decimal_odds - 1.0


def edge(p_model: float, p_market_fair: float) -> float:
    """Relative edge of the model over the vig-free market probability."""
    return p_model / p_market_fair - 1.0


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """Full-Kelly stake fraction (0 when the bet has no positive EV)."""
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, f)


def stake(p: float, decimal_odds: float, fraction: float = 0.25, cap: float = 0.02) -> float:
    return min(cap, fraction * kelly_fraction(p, decimal_odds))


def assess(p_model: float, decimal_odds: float, p_market_fair: float, cfg: dict) -> dict:
    """Evaluate one selection.

    Value = edge vs the vig-free market >= min_edge AND positive EV at the real price AND odds in range.
    Edges above max_edge (if set) are far more likely to be model error than a mispriced line, so they
    are reported as `suspect` and not staked.
    """
    e = edge(p_model, p_market_fair)
    ev = expected_value(p_model, decimal_odds)
    in_range = cfg["min_odds"] <= decimal_odds <= cfg["max_odds"]
    passes = e >= cfg["min_edge"] and ev > 0 and in_range
    max_edge = cfg.get("max_edge")
    suspect = passes and max_edge is not None and e > max_edge
    is_value = passes and not suspect
    return {
        "edge": e,
        "ev": ev,
        "stake": stake(p_model, decimal_odds, cfg["kelly_fraction"], cfg["max_stake"]) if is_value else 0.0,
        "is_value": is_value,
        "suspect": suspect,
    }
