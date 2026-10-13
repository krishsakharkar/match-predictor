"""Corners, cards, shots and player blocks for one prediction payload (uses the Predictor's league
models fitted with their count models). Each market's maths lives in its own module."""
from __future__ import annotations

from . import config
from .absences import absent_names, covered
from .models import cards, corners, players as player_model, shots
from .models.counts import STATS


def stat_models(pred) -> dict:
    return {stat: {lg: m.stats[stat] for lg, m in pred.models.items() if stat in m.stats} for stat in STATS}


def player_context(conn, league: str | None, season: str, team: str) -> tuple[list[dict], int]:
    """Understat players + the number of league games their stats cover (current season once a team
    has 5+ games, else last season)."""
    if not league:
        return [], 0
    for s in (season, config.previous_seasons(season, 1)[0]):
        rows = [dict(r) for r in conn.execute(
            "SELECT player, position, games, minutes, goals, xg, npxg, assists, xa, shots FROM understat_players "
            "WHERE league=? AND season=? AND team=?", (league, s, team))]
        games = conn.execute("SELECT COUNT(*) FROM matches WHERE league=? AND season=? AND status='FT' "
                             "AND (home=? OR away=?)", (league, s, team, team)).fetchone()[0]
        if rows and games >= 5 and covered(conn, league, s, team, rows):
            return rows, games
    return [], 0


def statuses(conn, match_id: str, team: str, players: list[dict]) -> dict:
    absent, partial = absent_names(conn, match_id, team, players)
    st = {n: "out" for n in absent}
    st.update({n: "bench" for n in partial})
    lu = conn.execute("SELECT payload FROM lineups WHERE match_id=? AND team=?", (match_id, team)).fetchone()
    if lu:
        import json
        from .models.injuries import match_player
        names = [p["player"] for p in players]
        for x in json.loads(lu["payload"])["starters"]:
            m = match_player(x["name"], names)
            if m:
                st[m] = "starter"
    return st


def build(conn, pred, comp: str, match_id: str, home: str, away: str, season: str, payload: dict,
          referee: str | None = None) -> None:
    """Adds payload["stats"] (corners, cards, shots, sot) and payload["players"] in place."""
    sm = stat_models(pred)
    tl = pred.team_league
    st = {}
    c = corners.predict(sm["corners"], comp, home, away, tl)
    if c:
        st["corners"] = c
    k = cards.predict(sm["cards"], comp, home, away, tl, referee)
    if k:
        st["cards"] = k
    st.update(shots.predict(sm["shots"], sm["sot"], comp, home, away, tl) or {})
    payload["stats"] = st
    pl = {}
    for side, team in (("home", home), ("away", away)):
        players, games = player_context(conn, tl.get(team), season, team)
        if not players:
            continue
        pl[side] = player_model.predict(
            players, games, payload["xg"][side],
            st.get("shots", {}).get(side), st.get("sot", {}).get(side), statuses(conn, match_id, team, players))
    payload["players"] = pl
