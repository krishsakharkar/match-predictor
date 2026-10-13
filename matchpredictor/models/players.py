"""Player markets from Understat season shares x the team's expected output for this match.

For each player (per 90 minutes he is on the pitch):
    goal share   = player npxG / team npxG       -> lambda_goal   = team xG          * share * mins/90
    assist share = player xA   / team xA         -> lambda_assist = team xG * A_RATE * share * mins/90
    shot share   = player shots / team shots     -> lambda_shots  = team exp. shots  * share * mins/90
                                                    lambda_sot    = team exp. SOT    * share * mins/90
    P(anytime goal) = 1 - e^-lambda_goal     P(assist) = 1 - e^-lambda_assist
    P(1+ SOT)       = 1 - e^-lambda_sot      P(2+ shots) = 1 - e^-l (1 + l)
Shares are measured over the player's own minutes (per-90), so regular starters and impact subs are
compared fairly. Penalties are excluded from npxG; the team-level penalty rate is added back to the
player with the most penalty xG (the usual taker). Expected minutes: season minutes per appearance,
replaced by 85 / 15 / 0 when a confirmed lineup lists him as starter / bench / absent.
"""
from __future__ import annotations

import math

A_RATE = 0.72          # share of goals that have an assist (top-5 leagues, long-run average)
MAX_SHARE = 0.6
MIN_MINUTES = 270


def _per90(value: float, minutes: float, team_value: float, team_minutes: float) -> float:
    if not minutes or not team_value:
        return 0.0
    return min(MAX_SHARE, (value / team_value) * (team_minutes / minutes))


def expected_minutes(p: dict, status: str | None) -> float:
    if status == "out":
        return 0.0
    if status == "starter":
        return 85.0
    if status == "bench":
        return 15.0
    return min(90.0, (p.get("minutes") or 0) / max(1, p.get("games") or 1))


def predict(players: list[dict], team_games: int, team_xg: float, team_shots: float | None,
            team_sot: float | None, statuses: dict | None = None, top: int = 6) -> list[dict]:
    """players: Understat rows (player, position, games, minutes, goals, xg, npxg, assists, xa, shots)."""
    statuses = statuses or {}
    if not players or not team_games:
        return []
    team_minutes = team_games * 90.0
    tot = {k: sum((p.get(k) or 0) for p in players) for k in ("npxg", "xa", "shots", "xg")}
    pen_xg = {p["player"]: (p.get("xg") or 0) - (p.get("npxg") or 0) for p in players}
    taker = max(pen_xg, key=pen_xg.get) if pen_xg and max(pen_xg.values()) > 0.5 else None
    team_pen_share = (tot["xg"] - tot["npxg"]) / tot["xg"] if tot["xg"] else 0.0
    out = []
    for p in players:
        mins = p.get("minutes") or 0
        if mins < MIN_MINUTES or "GK" in (p.get("position") or "").split():
            continue
        em = expected_minutes(p, statuses.get(p["player"]))
        f = em / 90.0
        g_share = _per90(p.get("npxg") or 0, mins, tot["npxg"], team_minutes)
        lam_goal = team_xg * (1 - team_pen_share) * g_share * f
        if p["player"] == taker and em > 0:
            lam_goal += team_xg * team_pen_share * min(1.0, f)
        lam_ast = team_xg * A_RATE * _per90(p.get("xa") or 0, mins, tot["xa"], team_minutes) * f
        s_share = _per90(p.get("shots") or 0, mins, tot["shots"], team_minutes)
        lam_shots = (team_shots or 0) * s_share * f
        lam_sot = (team_sot or 0) * s_share * f
        row = {
            "player": p["player"], "position": p.get("position"), "exp_minutes": round(em),
            "anytime_goal": 1 - math.exp(-lam_goal), "assist": 1 - math.exp(-lam_ast),
            "sot_1plus": (1 - math.exp(-lam_sot)) if team_sot else None,
            "shots_2plus": (1 - math.exp(-lam_shots) * (1 + lam_shots)) if team_shots else None,
            "penalty_taker": p["player"] == taker,
        }
        out.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()})
    out.sort(key=lambda r: r["anytime_goal"], reverse=True)
    return out[:top]
