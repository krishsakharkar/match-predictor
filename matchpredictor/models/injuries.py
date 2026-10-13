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


def _share(x, total):
    return (x or 0) / total if total and total > 0 else 0.0


def is_goalkeeper(position: str | None) -> bool:
    return bool(position) and "GK" in position.split()


def first_choice_gk(team_players: list[dict]) -> str | None:
    gks = [p for p in team_players if is_goalkeeper(p.get("position"))]
    return max(gks, key=lambda p: p.get("minutes") or 0)["player"] if gks else None


def absence_effect(absent: list[str], team_players: list[dict], cfg: dict,
                   partial: dict[str, float] | None = None) -> dict:
    """Effect of missing players on a team, from their season shares.

    absent: names (as in team_players) who will not play; partial: {name: weight} for e.g. bench (0.5).
    Returns {"attack": multiplier on own expected goals, "defence": multiplier on the opponent's expected
    goals, "players": [{"player", "attack_share", "gk"}...]}.
      attack share  = 0.5 * npxG share + 0.3 * xA share + 0.2 * goal share  (shares of team totals)
      attack mult   = max(min_multiplier, 1 - sum(share * weight) * (1 - replacement_retention))
      defence mult  = 1 + gk_penalty (first-choice keeper out)
                        + defender_penalty * sum(minutes share of absent regular defenders)
    """
    weights = {n: 1.0 for n in absent}
    weights.update(partial or {})
    if not weights or not team_players:
        return {"attack": 1.0, "defence": 1.0, "players": []}
    tot = {k: sum((p.get(k) or 0) for p in team_players) for k in ("npxg", "xa", "goals", "minutes")}
    team_minutes = max((p.get("minutes") or 0) for p in team_players) or 1   # ~ the team's league minutes
    gk1 = first_choice_gk(team_players)
    lost, defence, detail = 0.0, 1.0, []
    for p in team_players:
        wgt = weights.get(p["player"])
        if not wgt:
            continue
        share = (0.5 * _share(p.get("npxg"), tot["npxg"]) + 0.3 * _share(p.get("xa"), tot["xa"])
                 + 0.2 * _share(p.get("goals"), tot["goals"]))
        lost += share * wgt
        gk = p["player"] == gk1
        if gk:
            defence += cfg["gk_penalty"] * wgt
        elif "D" in (p.get("position") or "").split():
            defence += cfg["defender_penalty"] * wgt * min(1.0, (p.get("minutes") or 0) / team_minutes)
        detail.append({"player": p["player"], "attack_share": round(share, 3), "gk": gk,
                       "minutes_share": round(min(1.0, (p.get("minutes") or 0) / team_minutes), 2)})
    attack = max(cfg["min_multiplier"], 1.0 - lost * (1.0 - cfg["replacement_retention"]))
    return {"attack": attack, "defence": defence, "players": detail}


def match_player(name: str, candidates: list[str]) -> str | None:
    """Match a provider's player name ('B. Fernandes', 'Bruno Fernandes') to one of `candidates`."""
    from .. import config
    if name in candidates:
        return name
    m = config.match_name(name, candidates, cutoff=0.85)
    if m:
        return m
    toks = config.normalise(name).split()
    if not toks:
        return None
    last, initial = toks[-1], toks[0][0]
    hits = [c for c in candidates if config.normalise(c).split()[-1:] == [last]]
    if len(hits) > 1:
        hits = [c for c in hits if config.normalise(c)[:1] == initial]
    return hits[0] if len(hits) == 1 else None
