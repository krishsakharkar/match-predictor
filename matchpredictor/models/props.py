"""Player props from player share of team output x team expected output.

    lambda_goals(player) = team_xg  * goal_share_per90 * expected_minutes / 90
    lambda_sot(player)   = team_sot * sot_share_per90  * expected_minutes / 90
    P(anytime scorer) = 1 - exp(-lambda_goals);  P(1+ SOT) = 1 - exp(-lambda_sot)

Per-90 shares are relative to the team's total over the player's minutes, so
a regular starter and an impact sub are treated fairly. The goal share is a
blend of the player's goal share and shot share (shrinks lucky finishers).
"""
from __future__ import annotations

import math

MAX_SHARE = 0.6


def per90_share(player_value: float, player_minutes: float, team_value: float,
                team_minutes: float) -> float:
    if player_minutes <= 0 or team_value <= 0:
        return 0.0
    share = (player_value / team_value) * (team_minutes / player_minutes)
    return min(share, MAX_SHARE)


def expected_minutes(apps: int, minutes: int, status: str = "unknown") -> float:
    """status: starter | bench | out | unknown (no lineup yet)."""
    if status == "out":
        return 0.0
    avg = minutes / apps if apps else 0.0
    if status == "starter":
        return max(avg, 75.0)
    if status == "bench":
        return 15.0
    return avg


def player_props(players: list[dict], team_xg: float, team_sot: float, team_goals: float,
                 team_shots: float, team_sot_total: float, team_minutes: float,
                 statuses: dict | None = None, top: int = 5) -> list[dict]:
    statuses = statuses or {}
    out = []
    for p in players:
        mins = p.get("minutes") or 0
        if mins < 270:          # need a few full games of evidence
            continue
        g_share = 0.5 * per90_share(p.get("goals") or 0, mins, team_goals, team_minutes) + \
            0.5 * per90_share(p.get("shots") or 0, mins, team_shots, team_minutes)
        sot_share = per90_share(p.get("sot") or 0, mins, team_sot_total, team_minutes)
        em = expected_minutes(p.get("apps") or 0, mins, statuses.get(p["player"], "unknown"))
        lam_g = team_xg * g_share * em / 90
        lam_s = team_sot * sot_share * em / 90
        pg = 1 - math.exp(-lam_g)
        ps = 1 - math.exp(-lam_s)
        out.append({
            "player": p["player"],
            "anytime_scorer": round(pg, 4),
            "anytime_scorer_fair": round(1 / pg, 2) if pg > 0 else None,
            "sot_1plus": round(ps, 4),
            "sot_1plus_fair": round(1 / ps, 2) if ps > 0 else None,
            "exp_minutes": round(em),
        })
    out.sort(key=lambda r: r["anytime_scorer"], reverse=True)
    return out[:top]
