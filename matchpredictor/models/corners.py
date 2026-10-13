"""Corners market: expected corners per team and over/under lines (total and per team).

Negative-binomial rating model per league (home/away splits, opponent adjustment, time decay),
see models/counts.py. Cross-league games use each team's own league ratings.
"""
from __future__ import annotations

from .. import config
from . import counts

STAT = "corners"


def predict(models: dict, comp: str, home: str, away: str, team_league: dict) -> dict | None:
    exp = counts.expectation(STAT, models, comp, home, away, team_league)
    if exp is None:
        return None
    lines = config.settings()["lines"]
    return counts.market_block(exp, lines["corners_total"], lines["corners_team"])
