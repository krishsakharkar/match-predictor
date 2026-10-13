"""Injury / absence adjustments.

A player's attacking importance is their share of team goals and shots
(shots are a stable proxy for xG, which football-data does not provide).
When key players are missing, the team's expected goals are scaled by

    multiplier = max(floor, 1 - sum(share_i) * (1 - retention))

`retention` is the fraction of a missing player's output that the
replacement recovers (0.5 = replacements are half as productive).
"""
from __future__ import annotations


def attacking_share(player_goals: float, player_shots: float, team_goals: float,
                    team_shots: float) -> float:
    """Blend of goal share and shot share (equal weights, shots stabilise small samples)."""
    gs = player_goals / team_goals if team_goals > 0 else 0.0
    ss = player_shots / team_shots if team_shots > 0 else 0.0
    if team_shots <= 0:
        return gs
    if team_goals <= 0:
        return ss
    return 0.5 * gs + 0.5 * ss


def absence_multiplier(absent_shares, retention: float = 0.5, floor: float = 0.75) -> float:
    lost = sum(max(0.0, s) for s in absent_shares) * (1.0 - retention)
    return max(floor, 1.0 - lost)
