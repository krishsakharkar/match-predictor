"""Phase A pipeline: fetch -> predict W/D/L + scorelines -> track -> site.

Scope (config): every Premier League and Champions League match, plus every match of the tracked
clubs in any competition. Value bets, odds polling and corners/cards/shots/props are paused.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

import pandas as pd

from . import MODEL_VERSION, config, db, history, tracking
from .absences import match_absences
from .ingest import api_football, clubelo, football_data, odds_api
from .ingest.http_cache import iso, now_utc, parse_iso
from .predict import Predictor, load_matches

log = logging.getLogger(__name__)


def league_teams(conn) -> set[str]:
    return history.all_team_names(conn, leagues_only=True)


# ---------------------------------------------------------------- fixtures / results

def sync_fixtures(conn, notes: list[str]) -> int:
    """Upcoming fixtures for every competition from The Odds API /events (free), filtered to what we show."""
    known = league_teams(conn)
    n = 0
    for key, c in config.competitions().items():
        try:
            evs = odds_api.events(conn, c["odds_api_sport"])
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Fixtures unavailable for {c['name']}: {e}")
            continue
        for e in evs:
            home = config.canonical_team(e["home_team"], known)
            away = config.canonical_team(e["away_team"], known)
            if not config.is_displayed(key, home, away):
                continue
            ko = e["commence_time"]
            db.upsert_match(conn, {"league": key, "season": config.season_code(ko), "date": ko[:10],
                                   "kickoff_utc": ko, "home": home, "away": away, "odds_api_id": e["id"],
                                   "source": "odds-api"})
            n += 1
    cur = config.season_code(now_utc())
    for key, c in config.leagues().items():
        fd = {r[0] for r in conn.execute("SELECT DISTINCT home FROM matches WHERE league=? AND season=? AND "
                                         "source IN ('football-data','mirror','understat')", (key, cur))}
        if not fd:
            continue
        odd = {r[0] for r in conn.execute(
            "SELECT home FROM matches WHERE league=? AND season=? AND source='odds-api' UNION "
            "SELECT away FROM matches WHERE league=? AND season=? AND source='odds-api'", (key, cur, key, cur))}
        if odd - fd:
            notes.append(f"{c['name']}: fixture team names not matched to results data: {sorted(odd - fd)} "
                         "(add to config/team_aliases.json)")
    return n


def sync_referees_fd(conn) -> None:
    """football-data fixtures.csv carries the referee for upcoming English games."""
    for fx in football_data.fetch_fixtures_csv():
        if fx["referee"]:
            conn.execute("UPDATE matches SET referee=? WHERE league=? AND home=? AND away=? AND status='SCHEDULED'",
                         (fx["referee"], fx["league"], fx["home"], fx["away"]))


def sync_results(conn, notes: list[str]) -> None:
    """Results for shown matches via Odds API /scores (2 credits/competition, cached ~20h, only when needed)."""
    now = now_utc()
    for key, c in config.competitions().items():
        pending = sum(1 for r in conn.execute(
            "SELECT home, away FROM matches WHERE league=? AND status='SCHEDULED' AND odds_api_id IS NOT NULL "
            "AND kickoff_utc < ? AND kickoff_utc > ?", (key, iso(now - timedelta(hours=2)), iso(now - timedelta(days=3))))
            if config.is_displayed(key, r["home"], r["away"]))
        if not pending:
            continue
        try:
            res = odds_api.scores(conn, c["odds_api_sport"], days_from=3)
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Results unavailable for {c['name']}: {e}")
            continue
        for g in res:
            if not g.get("completed") or not g.get("scores"):
                continue
            sc = {s["name"]: s["score"] for s in g["scores"]}
            try:
                hg, ag = int(sc[g["home_team"]]), int(sc[g["away_team"]])
            except (KeyError, ValueError):
                continue
            conn.execute("UPDATE matches SET fthg=?, ftag=?, status='FT', updated_at=CURRENT_TIMESTAMP "
                         "WHERE odds_api_id=?", (hg, ag, g["id"]))


# ---------------------------------------------------------------- API-Football (Pro for the current season)

def enrich(conn, notes: list[str], upcoming: pd.DataFrame) -> None:
    """Fixture ids + referees, injuries/suspensions (<=3 days out), lineups (<=100 min), penalties of
    finished shown matches. Free plan: first call fails with a plan error and everything is skipped."""
    client = api_football.Client(conn)
    now = now_utc()
    af_season = 2000 + int(config.season_code(now)[:2])
    known = league_teams(conn)
    try:
        for key, c in config.competitions().items():
            ups = upcoming[upcoming.league == key]
            if ups.empty or not c.get("api_football_id"):
                continue
            fx = client.fixtures(c["api_football_id"], af_season, now.strftime("%Y-%m-%d"),
                                 (now + timedelta(days=config.settings()["display"]["days_ahead"])).strftime("%Y-%m-%d"))
            for f in map(api_football.parse_fixture, fx):
                h, a = config.canonical_team(f["home"], known), config.canonical_team(f["away"], known)
                conn.execute("UPDATE matches SET api_football_id=?, referee=COALESCE(?, referee) WHERE league=? "
                             "AND home=? AND away=? AND status='SCHEDULED'", (f["id"], f["referee"], key, h, a))
            for m in ups.itertuples():
                row = conn.execute("SELECT api_football_id FROM matches WHERE id=?", (m.id,)).fetchone()
                fid = row["api_football_id"] if row else None
                if not fid:
                    continue
                to_ko = parse_iso(m.kickoff_utc) - now
                if to_ko < timedelta(days=3):
                    for inj in api_football.parse_injuries(client.injuries(fid)):
                        team = config.canonical_team(inj["team"], known)
                        conn.execute("INSERT OR REPLACE INTO absences (match_id, team, player_id, player, reason) "
                                     "VALUES (?,?,?,?,?)", (m.id, team, inj["player_id"], inj["player"], inj["reason"]))
                if to_ko < timedelta(minutes=100):
                    for team_name, payload in api_football.parse_lineup(client.lineups(fid)).items():
                        conn.execute("INSERT OR REPLACE INTO lineups (match_id, team, payload) VALUES (?,?,?)",
                                     (m.id, config.canonical_team(team_name, known), json.dumps(payload)))
        # penalties for finished shown matches (feeds the referee penalty rate), a few per run
        done = conn.execute("SELECT id, api_football_id, home, away FROM matches WHERE status='FT' AND "
                            "api_football_id IS NOT NULL AND pens_home IS NULL ORDER BY date DESC LIMIT 15").fetchall()
        for r in done:
            ev = client.events(r["api_football_id"])
            teams = {t for e in ev for t in [(e.get("team") or {}).get("name")] if t}
            raw = {config.canonical_team(t, known): t for t in teams}
            ph, pa = api_football.count_penalties(ev, raw.get(r["home"], r["home"]), raw.get(r["away"], r["away"]))
            conn.execute("UPDATE matches SET pens_home=?, pens_away=? WHERE id=?", (ph, pa, r["id"]))
    except api_football.ApiFootballUnavailable as e:
        notes.append(f"API-Football: {e} - injuries, lineups and referee assignments need a plan that covers the "
                     "current season (Pro)")


def raw_absences(conn, match_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT team, player, reason FROM absences WHERE match_id=?", (match_id,))]


# ---------------------------------------------------------------- main

def displayed_upcoming(df: pd.DataFrame, now, hours: float | None = None) -> pd.DataFrame:
    days = config.settings()["display"]["days_ahead"]
    end = iso(now + (timedelta(hours=hours) if hours else timedelta(days=days)))
    up = df[(df.status == "SCHEDULED") & df.kickoff_utc.notna() & (df.kickoff_utc > iso(now)) & (df.kickoff_utc < end)]
    return up[[config.is_displayed(r.league, r.home, r.away) for r in up.itertuples()]]


def run(conn, mode: str = "daily") -> dict:
    notes: list[str] = []
    now = now_utc()
    cur = config.season_code(now)
    conn.execute("INSERT INTO runs (started_at, mode) VALUES (?, ?)", (iso(now), mode))

    if mode == "daily":
        history.refresh(conn, cur, notes)
        sync_referees_fd(conn)
    sync_fixtures(conn, notes)
    sync_results(conn, notes)
    conn.commit()

    df = load_matches(conn)
    upcoming = displayed_upcoming(df, now, hours=3 if mode == "prekickoff" else None)
    enrich(conn, notes, upcoming)
    conn.commit()
    df = load_matches(conn)
    upcoming = displayed_upcoming(df, now, hours=3 if mode == "prekickoff" else None)

    book = clubelo.EloBook(conn)
    book = book if book else None
    elo_params = None
    if book:
        elo_params = db.get_param(conn, "elo_params")
        if mode == "daily" or elo_params is None:
            fitted = history.fit_elo_params(df, book)
            if fitted:
                elo_params = fitted
                db.set_param(conn, "elo_params", fitted, iso(now))
    fixture_teams = {lg: set(upcoming[upcoming.league == lg].home) | set(upcoming[upcoming.league == lg].away)
                     for lg in config.leagues()}
    pred = Predictor(df, now.date(), elo_book=book, elo_params=elo_params, fixture_teams=fixture_teams)

    n_pred, skipped = 0, []
    for m in upcoming.itertuples():
        day = m.kickoff_utc[:10]
        lh, la = pred.team_league.get(m.home), pred.team_league.get(m.away)
        effects, shown = match_absences(conn, m.id, m.home, m.away, lh, la, cur)
        ref = m.referee if isinstance(m.referee, str) else None
        p = pred.predict(m.league, m.home, m.away, day, absences=effects, referee=ref)
        if p is None:
            skipped.append(f"{m.home} v {m.away}")
            continue
        p["absences"] = shown or [dict(a, status="out") for a in raw_absences(conn, m.id)]
        p["referee"] = ref
        lu = {r["team"] for r in conn.execute("SELECT team FROM lineups WHERE match_id=?", (m.id,))}
        p["lineups_confirmed"] = bool(lu)
        db.save_prediction(conn, m.id, iso(now), MODEL_VERSION, p)
        n_pred += 1
    if skipped:
        notes.append(f"No strength rating for {len(skipped)} fixtures (no league history or ClubElo rating): "
                     + ", ".join(skipped[:8]))
    conn.commit()

    from .site import build
    build(conn, notes)
    conn.execute("UPDATE runs SET notes=? WHERE started_at=? AND mode=?", ("\n".join(notes), iso(now), mode))
    return {"mode": mode, "predictions": n_pred, "notes": notes,
            "tracked": tracking.score_predictions(tracking.tracked_rows(conn)).get("n", 0)}
