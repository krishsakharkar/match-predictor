"""Cards market: expected cards (yellow + red) per team, referee-adjusted, with over/under lines.

The referee factor is a shrunk ratio of the referee's actual cards to what the model expected in
their games (needs referee names in the history: football-data provides them for England).
"""
from __future__ import annotations

from .. import config
from . import counts

STAT = "cards"


def predict(models: dict, comp: str, home: str, away: str, team_league: dict,
            referee: str | None = None) -> dict | None:
    exp = counts.expectation(STAT, models, comp, home, away, team_league, referee)
    if exp is None:
        return None
    lines = config.settings()["lines"]
    out = counts.market_block(exp, lines["cards_total"], lines["cards_team"])
    out["referee"] = referee
    return out
