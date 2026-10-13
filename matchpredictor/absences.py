"""Turn injury/suspension lists or confirmed lineups into per-team expected-goal effects.

Player importance comes from Understat season stats (npxG, xA, goals, minutes, position). Live
predictions use the current season once a team has 5+ games, else last season; the backtest uses
only the previous season (no look-ahead).
"""
from __future__ import annotations

import json

from . import config
from .models import injuries


def team_players(conn, league: str | None, season: str, team: str, allow_current: bool = True) -> list[dict]:
    if not league:
        return []
    prev = config.previous_seasons(season, 1)[0]
    seasons = [season, prev] if allow_current else [prev]
    for s in seasons:
        rows = [dict(r) for r in conn.execute(
            "SELECT player, position, games, minutes, goals, npxg, xa FROM understat_players "
            "WHERE league=? AND season=? AND team=?", (league, s, team))]
        if rows and (s != season or max(r["games"] or 0 for r in rows) >= 5) and covered(conn, league, s, team, rows):
            return rows
    return []


def covered(conn, league: str, season: str, team: str, rows: list[dict], min_share: float = 0.7) -> bool:
    """True if the players' minutes add up to most of the team's (11 x 90 per game): guards against
    incomplete provider data."""
    games = conn.execute("SELECT COUNT(*) FROM matches WHERE league=? AND season=? AND status='FT' AND (home=? OR away=?)",
                         (league, season, team, team)).fetchone()[0]
    if not games:
        return True
    return sum((r.get("minutes") or 0) for r in rows) >= min_share * 11 * 90 * games


def absence_streak(conn, match_id: str, team: str, player: str, limit: int = 10) -> int:
    """How many of the team's immediately preceding matches this player also missed."""
    row = conn.execute("SELECT date, league FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        return 0
    # same competition only: injury lists are recorded per competition's fixtures
    prev = conn.execute("SELECT id FROM matches WHERE league=? AND (home=? OR away=?) AND date < ? "
                        "ORDER BY date DESC LIMIT ?", (row["league"], team, team, row["date"], limit)).fetchall()
    n = 0
    for p in prev:
        if not conn.execute("SELECT 1 FROM absences WHERE match_id=? AND team=? AND player=?",
                            (p["id"], team, player)).fetchone():
            break
        n += 1
    return n


def absent_names(conn, match_id: str, team: str, players: list[dict],
                 fresh_k: int | None = None) -> tuple[list[str], dict[str, float]]:
    """(absent, partial) player names in Understat spelling, from injuries and/or a confirmed lineup.

    fresh_k: ignore players who already missed the team's previous `fresh_k` matches - a long-term
    absence is already reflected in recent results, so counting it again double-counts.
    """
    names = [p["player"] for p in players]
    absent = set()
    for r in conn.execute("SELECT player FROM absences WHERE match_id=? AND team=?", (match_id, team)):
        m = injuries.match_player(r["player"], names)
        if m and not (fresh_k and absence_streak(conn, match_id, team, r["player"], fresh_k) >= fresh_k):
            absent.add(m)
    partial = {}
    lu = conn.execute("SELECT payload FROM lineups WHERE match_id=? AND team=?", (match_id, team)).fetchone()
    if lu:
        cfg = config.settings()["injuries"]
        lineup = json.loads(lu["payload"])
        starters = {injuries.match_player(x["name"], names) for x in lineup["starters"]} - {None}
        bench = {injuries.match_player(x, names) for x in lineup["subs"]} - {None}
        top = max((p["minutes"] or 0) for p in players) or 1
        for p in players:
            regular = (p["minutes"] or 0) >= cfg["regular_minutes_share"] * top
            if regular and p["player"] not in starters:
                if p["player"] in bench:
                    partial[p["player"]] = cfg["bench_weight"]
                else:
                    absent.add(p["player"])
    return sorted(absent - set(partial)), partial


def match_absences(conn, match_id: str, home: str, away: str, league_home: str | None, league_away: str | None,
                   season: str, allow_current: bool = True, fresh_k: int | None = None) -> tuple[dict, list[dict]]:
    """({"home": effect, "away": effect}, display list) for a fixture. Empty when nothing is known."""
    cfg = config.settings()["injuries"]
    if fresh_k is None:
        fresh_k = cfg.get("fresh_absence_matches")
    effects, shown = {}, []
    for side, team, lg in (("home", home, league_home), ("away", away, league_away)):
        players = team_players(conn, lg, season, team, allow_current)
        if not players:
            continue
        absent, partial = absent_names(conn, match_id, team, players, fresh_k)
        if not absent and not partial:
            continue
        eff = injuries.absence_effect(absent, players, cfg, partial)
        effects[side] = eff
        for p in sorted(eff["players"], key=lambda x: -x["attack_share"]):
            shown.append({"team": team, "player": p["player"], "attack_share": p["attack_share"], "gk": p["gk"],
                          "status": "bench" if p["player"] in partial else "out"})
    return effects, shown
