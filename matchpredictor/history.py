"""Assemble training / backtest data from every source, with graceful degradation.

  football-data.co.uk  results, stats, closing odds, EPL referees  (5 leagues x history seasons)
  Understat            match xG + player xG/xA                      (same leagues)
  ClubElo              snapshots + per-club rating history          (Elo model, rest days)
  API-Football         Champions League results, league referees, injuries (seasons the plan allows)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from . import config, db
from .ingest import api_football, clubelo, football_data, understat
from .ingest.http_cache import iso, now_utc
from .models import elo as elo_model

log = logging.getLogger(__name__)


def history_seasons(current: str) -> list[str]:
    return config.previous_seasons(current, config.settings()["history_seasons"]) + [current]


def all_team_names(conn, leagues_only: bool = False) -> set[str]:
    q = "SELECT home FROM matches" + (" WHERE league IN (%s)" % ",".join("?" * len(config.leagues()))
                                      if leagues_only else "")
    params = list(config.leagues()) if leagues_only else []
    names = {r[0] for r in conn.execute(q, params)}
    q2 = q.replace("SELECT home", "SELECT away")
    return names | {r[0] for r in conn.execute(q2, params)}


# ---------------------------------------------------------------- ClubElo

def elo_snapshots(conn, current: str, notes: list[str]) -> bool:
    """Today's ratings plus one snapshot per history season start (so relegated clubs are known)."""
    days = [now_utc().strftime("%Y-%m-%d")] + [f"20{s[:2]}-08-01" for s in history_seasons(current)]
    ok = False
    for d in days:
        if d != days[0] and conn.execute("SELECT 1 FROM elo_ratings WHERE date_from <= ? AND date_to >= ? LIMIT 1",
                                         (d, d)).fetchone():
            continue
        try:
            clubelo.fetch_snapshot(conn, d)
            ok = True
        except clubelo.ClubEloUnavailable as e:
            notes.append(f"ClubElo: {e} - Elo ratings, cross-competition strength and rest days skipped")
            return False
    conn.commit()
    return ok or bool(conn.execute("SELECT 1 FROM elo_ratings LIMIT 1").fetchone())


def elo_backfill(conn, current: str, notes: list[str], force: bool = False) -> int:
    """Per-club rating history for every club we model (monthly). Needed for backtests and rest days."""
    s = config.settings()["elo"]
    last = db.get_param(conn, "elo_history_at")
    if not force and last and datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(
            days=s["history_refresh_days"]):
        return 0
    book = clubelo.EloBook(conn)
    if not book:
        return 0
    since = f"20{history_seasons(current)[0][:2]}-07-01"
    teams = {}
    for lg, c in config.leagues().items():
        for r in conn.execute("SELECT DISTINCT home FROM matches WHERE league=?", (lg,)):
            teams[r[0]] = c.get("country")
    for t in all_team_names(conn) - set(teams):
        teams[t] = None
    clubs, unmatched = set(), []
    for t, ctry in teams.items():
        c = book.club_for(t, ctry)
        (clubs.add(c) if c else unmatched.append(t))
    n = 0
    for c in sorted(clubs):
        try:
            n += clubelo.fetch_history(conn, c, since)
        except clubelo.ClubEloUnavailable as e:
            notes.append(f"ClubElo history incomplete: {e}")
            break
    if unmatched:
        notes.append(f"ClubElo: no rating found for {len(unmatched)} teams, e.g. {sorted(unmatched)[:12]} "
                     "(add to config/elo_aliases.json)")
    db.set_param(conn, "elo_history_at", datetime.now(timezone.utc).isoformat(), iso(now_utc()))
    conn.commit()
    return n


def elo_training_rows(df, book, before: str | None = None):
    """(elo_home, elo_away, hg, ag, comp) for finished matches with both pre-match ratings."""
    comps = config.competitions()
    sub = df[(df.status == "FT") & df.fthg.notna()]
    if before:
        sub = sub[sub.date < before]
    team_country = {}
    for lg in config.leagues():
        for t in set(sub[sub.league == lg].home):
            team_country[t] = comps[lg].get("country")
    rows = []
    for r in sub.itertuples():
        d = r.date.strftime("%Y-%m-%d")
        eh = book.team_rating(r.home, d, team_country.get(r.home))
        ea = book.team_rating(r.away, d, team_country.get(r.away))
        if eh and ea:
            ctype = comps.get(r.league, {}).get("type")
            rows.append((eh, ea, r.fthg, r.ftag, r.league if ctype in ("league", "europe") else "_"))
    return rows


def fit_elo_params(df, book, before: str | None = None) -> dict | None:
    rows = elo_training_rows(df, book, before)
    if len(rows) < 200:
        return None
    a = np.array([(r[0], r[1], r[2], r[3]) for r in rows], dtype=float)
    return elo_model.fit(a[:, 0], a[:, 1], a[:, 2], a[:, 3], comps=[r[4] for r in rows])


# ---------------------------------------------------------------- API-Football history

def _store_fixture_rows(conn, comp: str, fixtures: list[dict], known: set[str]) -> int:
    n = 0
    for f in fixtures:
        if not f["finished"] or f["fthg"] is None or not api_football.is_main_round(f["round"]):
            continue
        home, away = config.canonical_team(f["home"], known), config.canonical_team(f["away"], known)
        db.upsert_match(conn, {"league": comp, "season": config.season_code(f["date"]), "date": f["date"],
                               "kickoff_utc": f["kickoff_utc"], "home": home, "away": away, "status": "FT",
                               "fthg": int(f["fthg"]), "ftag": int(f["ftag"]), "referee": f["referee"],
                               "api_football_id": f["id"], "source": "api-football"})
        n += 1
    return n


def backfill_api_football(conn, notes: list[str], seasons=(2022, 2023, 2024)) -> dict:
    """Historical Champions League results; league referees + injuries for the latest allowed season."""
    known = all_team_names(conn, leagues_only=True)
    out = {}
    comps = config.competitions()
    for year in seasons:
        client = api_football.Client(conn)       # fresh client: a plan error for one season mustn't block others
        try:
            fx = [api_football.parse_fixture(f) for f in client.season_fixtures(comps["UCL"]["api_football_id"], year)]
            out[f"UCL:{year}"] = _store_fixture_rows(conn, "UCL", fx, known)
        except api_football.ApiFootballUnavailable as e:
            notes.append(f"API-Football Champions League {year}: {e}")
    year = max(seasons)
    for lg, c in config.leagues().items():
        client = api_football.Client(conn)
        try:
            fx = [api_football.parse_fixture(f) for f in client.season_fixtures(c["api_football_id"], year)]
            inj = api_football.parse_injuries(client.season_injuries(c["api_football_id"], year))
        except api_football.ApiFootballUnavailable as e:
            notes.append(f"API-Football {lg} {year}: {e}")
            continue
        lg_known = {r[0] for r in conn.execute("SELECT DISTINCT home FROM matches WHERE league=?", (lg,))}
        by_fixture = {}
        season = f"{year % 100:02d}{(year + 1) % 100:02d}"
        for f in fx:
            home, away = config.canonical_team(f["home"], lg_known), config.canonical_team(f["away"], lg_known)
            mid = db.match_id(lg, season, home, away)
            by_fixture[f["id"]] = (mid, f["home"], home, f["away"], away)
            conn.execute("UPDATE matches SET referee=COALESCE(referee, ?), api_football_id=? WHERE id=?",
                         (f["referee"], f["id"], mid))
        n_inj = 0
        for i in inj:
            hit = by_fixture.get(i["fixture_id"])
            if not hit:
                continue
            mid, h_raw, h, a_raw, a = hit
            team = h if i["team"] == h_raw else a if i["team"] == a_raw else config.canonical_team(i["team"], lg_known)
            conn.execute("INSERT OR REPLACE INTO absences (match_id, team, player_id, player, reason) VALUES (?,?,?,?,?)",
                         (mid, team, i["player_id"], i["player"], i["reason"]))
            n_inj += 1
        out[f"{lg}:{year}"] = {"fixtures": len(fx), "injuries": n_inj}
    conn.commit()
    return out


# ---------------------------------------------------------------- everything

def refresh(conn, current: str, notes: list[str], backtest: bool = False) -> dict:
    out = {"football_data": football_data.update_history(conn, current, notes)}
    conn.commit()
    out["understat"] = understat.update(conn, current, notes)
    conn.commit()
    if backtest:      # first, so Champions League clubs get Elo history too
        out["api_football"] = backfill_api_football(conn, notes)
    if elo_snapshots(conn, current, notes):
        out["elo_history_rows"] = elo_backfill(conn, current, notes, force=False)
    return out
