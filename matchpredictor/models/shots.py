"""Shots market: expected shots and shots on target per team with over/under lines."""
from __future__ import annotations

from .. import config
from . import counts


def predict(shot_models: dict, sot_models: dict, comp: str, home: str, away: str, team_league: dict) -> dict | None:
    lines = config.settings()["lines"]
    out = {}
    for key, models, tl, ml in (("shots", shot_models, "shots_total", "shots_team"),
                                ("sot", sot_models, "sot_total", "sot_team")):
        exp = counts.expectation(key, models, comp, home, away, team_league)
        if exp is not None:
            out[key] = counts.market_block(exp, lines[tl], lines[ml])
    return out or None
