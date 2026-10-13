"""Daily / pre-kickoff pipeline: fetch -> predict -> value -> track -> site."""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

import pandas as pd

from . import MODEL_VERSION, config, db, tracking, value
from .ingest import api_football, football_data, odds_api
from .ingest.http_cache import iso, now_utc, parse_iso
from .models import injuries
from .predict import fit_league, load_matches, predict_match

log = logging.getLogger(__name__)
PREDICT_HORIZON_DAYS = 10


def normalise_referee(name: str | None) -> str | None:
    """'M. Oliver, England' -> 'M Oliver' (football-data style)."""
    if not name:
        return None
    name = name.split(",")[0]
    return re.sub(r"\s+", " ", name.replace(".", " ")).strip() or None


def known_teams(conn, league: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT home FROM matches WHERE league=?", (league,))}


# ---------------------------------------------------------------- fixtures / results / odds

def sync_fixtures(conn, notes: list[str]) -> None:
    for key, lg in config.leagues().items():
        known = known_teams(conn, key)
        try:
            evs = odds_api.events(conn, lg["odds_api_sport"])
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Odds API fixtures unavailable for {lg['name']}: {e}")
            continue
        for e in evs:
            home = config.canonical_team(e["home_team"], known)
            away = config.canonical_team(e["away_team"], known)
            ko = e["commence_time"]
            db.upsert_match(conn, {"league": key, "season": config.season_code(ko), "date": ko[:10],
                                   "kickoff_utc": ko, "home": home, "away": away,
                                   "odds_api_id": e["id"], "source": "odds-api"})
        cur = config.season_code(now_utc())
        fd_teams = {r[0] for r in conn.execute(
            "SELECT DISTINCT home FROM matches WHERE league=? AND season=? AND source IN ('football-data','mirror')",
            (key, cur))}
        if fd_teams:
            unknown = {r[0] for r in conn.execute(
                "SELECT home FROM matches WHERE league=? AND season=? AND source='odds-api' "
                "UNION SELECT away FROM matches WHERE league=? AND season=? AND source='odds-api'",
                (key, cur, key, cur))} - fd_teams
            if unknown:
                notes.append(f"{lg['name']}: fixture team names not matched to football-data: "
                             f"{sorted(unknown)} (add to config/team_aliases.json)")


def sync_referees(conn) -> None:
    for fx in football_data.fetch_fixtures_csv():
        if fx["referee"]:
            conn.execute("UPDATE matches SET referee=? WHERE league=? AND home=? AND away=? AND status='SCHEDULED'",
                         (fx["referee"], fx["league"], fx["home"], fx["away"]))


def sync_results(conn, notes: list[str]) -> None:
    """Fast results via Odds API /scores, only when there are recent unsettled kickoffs."""
    now = now_utc()
    for key, lg in config.leagues().items():
        pending = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE league=? AND status='SCHEDULED' AND kickoff_utc < ? AND kickoff_utc > ?",
            (key, iso(now - timedelta(hours=2)), iso(now - timedelta(days=3)))).fetchone()[0]
        if not pending:
            continue
        try:
            res = odds_api.scores(conn, lg["odds_api_sport"], days_from=3)
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Odds API scores unavailable for {lg['name']}: {e}")
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


def sync_odds(conn, notes: list[str], horizon: timedelta) -> int:
    now = now_utc()
    stored = 0
    for key, lg in config.leagues().items():
        upcoming = conn.execute(
            "SELECT id, odds_api_id FROM matches WHERE league=? AND status='SCHEDULED' AND kickoff_utc > ? "
            "AND kickoff_utc < ? AND odds_api_id IS NOT NULL", (key, iso(now), iso(now + horizon))).fetchall()
        if not upcoming:
            continue
        by_event = {r["odds_api_id"]: r["id"] for r in upcoming}
        try:
            evs = odds_api.odds(conn, lg["odds_api_sport"])
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Odds API odds unavailable for {lg['name']}: {e}")
            continue
        fetched = iso(now)
        for e in evs:
            mid = by_event.get(e["id"])
            if not mid:
                continue
            for row in odds_api.parse_odds(e, e["home_team"], e["away_team"]):
                last = conn.execute(
                    "SELECT price FROM odds_snapshots WHERE match_id=? AND bookmaker=? AND market=? AND selection=? "
                    "AND COALESCE(line,-1)=COALESCE(?,-1) ORDER BY fetched_at DESC LIMIT 1",
                    (mid, row["bookmaker"], row["market"], row["selection"], row["line"])).fetchone()
                if last and last["price"] == row["price"]:
                    continue   # store only price changes; the latest row is still the current price
                conn.execute("INSERT INTO odds_snapshots (match_id, fetched_at, bookmaker, market, selection, line, price) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (mid, fetched, row["bookmaker"], row["market"], row["selection"], row["line"], row["price"]))
                stored += 1
    return stored


def latest_odds(conn, match_id: str, before: str | None = None) -> dict:
    q = ("SELECT market, selection, line, price, fetched_at FROM odds_snapshots WHERE match_id=?"
         + (" AND fetched_at <= ?" if before else "") + " ORDER BY fetched_at")
    out: dict = {}
    for r in conn.execute(q, (match_id, before) if before else (match_id,)):
        if r["market"] == "totals":
            out.setdefault("totals", {}).setdefault(str(r["line"]), {})[r["selection"]] = r["price"]
        else:
            out.setdefault(r["market"], {})[r["selection"]] = r["price"]
        out["updated"] = r["fetched_at"]
    return out


# ---------------------------------------------------------------- API-Football enrichment

def enrich(conn, notes: list[str], upcoming: pd.DataFrame) -> None:
    """Best-effort API-Football refresh: fixture ids, referees, player stats, injuries, lineups."""
    client = api_football.Client(conn)
    if upcoming.empty:
        return
    now = now_utc()
    cur = config.season_code(now)
    af_season = 2000 + int(cur[:2])
    try:
        for key, lg in config.leagues().items():
            ups = upcoming[upcoming.league == key]
            if ups.empty:
                continue
            known = known_teams(conn, key)
            fx = client.fixtures(lg["api_football_id"], af_season, now.strftime("%Y-%m-%d"),
                                 (now + timedelta(days=PREDICT_HORIZON_DAYS)).strftime("%Y-%m-%d"))
            for f in fx:
                h = config.canonical_team(f["teams"]["home"]["name"], known)
                a = config.canonical_team(f["teams"]["away"]["name"], known)
                ref = normalise_referee(f["fixture"].get("referee"))
                conn.execute("UPDATE matches SET api_football_id=?, referee=COALESCE(?, referee) "
                             "WHERE league=? AND season=? AND home=? AND away=?",
                             (f["fixture"]["id"], ref, key, cur, h, a))
            for p in api_football.parse_topscorers(client.topscorers(lg["api_football_id"], af_season)):
                conn.execute(
                    "INSERT INTO player_stats (league, season, team, player_id, player, apps, minutes, goals, shots, sot, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(league, season, player_id) DO UPDATE SET "
                    "team=excluded.team, apps=excluded.apps, minutes=excluded.minutes, goals=excluded.goals, "
                    "shots=excluded.shots, sot=excluded.sot, updated_at=excluded.updated_at",
                    (key, cur, config.canonical_team(p["team"], known), p["player_id"], p["player"], p["apps"],
                     p["minutes"], p["goals"], p["shots"], p["sot"], iso(now)))
            for _, m in ups.iterrows():
                row = conn.execute("SELECT api_football_id FROM matches WHERE id=?", (m.id,)).fetchone()
                fid = row["api_football_id"] if row else None
                if not fid:
                    continue
                ko = parse_iso(m.kickoff_utc)
                if ko - now < timedelta(days=3):
                    for inj in api_football.parse_injuries(client.injuries(fid)):
                        team = config.canonical_team(inj["team"], known)
                        conn.execute("INSERT OR REPLACE INTO absences (match_id, team, player_id, player, reason) "
                                     "VALUES (?,?,?,?,?)", (m.id, team, inj["player_id"], inj["player"], inj["reason"]))
                if ko - now < timedelta(minutes=100):
                    lu = api_football.parse_lineup(client.lineups(fid))
                    for team_name, payload in lu.items():
                        team = config.canonical_team(team_name, known)
                        conn.execute("INSERT OR REPLACE INTO lineups (match_id, team, payload) VALUES (?,?,?)",
                                     (m.id, team, json.dumps(payload)))
    except api_football.ApiFootballUnavailable as e:
        notes.append(f"API-Football: {e} - lineups, injuries and player props skipped where missing")


def team_context(conn, league: str, season: str, team: str) -> dict | None:
    """Season totals for a team plus its players' stats (for props and absence shares)."""
    players = [dict(r) for r in conn.execute(
        "SELECT player_id, player, apps, minutes, goals, shots, sot FROM player_stats WHERE league=? AND season=? AND team=?",
        (league, season, team))]
    if not players:
        return None
    t = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN home=? THEN fthg ELSE ftag END) g, SUM(CASE WHEN home=? THEN hs ELSE as_ END) s, "
        "SUM(CASE WHEN home=? THEN hst ELSE ast END) sot FROM matches WHERE league=? AND season=? AND status='FT' "
        "AND (home=? OR away=?)", (team, team, team, league, season, team, team)).fetchone()
    if not t["n"]:
        return None
    return {"players": players, "team_goals": t["g"] or 0, "team_shots": t["s"] or 0, "team_sot": t["sot"] or 0,
            "team_minutes": t["n"] * 90}


def match_inputs(conn, m, league: str, season: str) -> tuple[dict, dict, dict, dict]:
    """absence shares, player data, lineup statuses and lineups for one match."""
    absences, pdata, statuses, lineups = {}, {}, {}, {}
    for side, team in (("home", m.home), ("away", m.away)):
        ctx = team_context(conn, league, season, team)
        out = {r["player"] for r in conn.execute("SELECT player FROM absences WHERE match_id=? AND team=?", (m.id, team))}
        lu_row = conn.execute("SELECT payload FROM lineups WHERE match_id=? AND team=?", (m.id, team)).fetchone()
        lu = json.loads(lu_row["payload"]) if lu_row else None
        if lu:
            lineups[side] = lu
        if ctx:
            pdata[side] = ctx
            absences[side] = [injuries.attacking_share(p["goals"], p["shots"], ctx["team_goals"], ctx["team_shots"])
                              for p in ctx["players"] if p["player"] in out]
            st = {p["player"]: "out" for p in ctx["players"] if p["player"] in out}
            if lu:
                starters = {x["name"] for x in lu["starters"]}
                subs = set(lu["subs"])
                for p in ctx["players"]:
                    st[p["player"]] = ("starter" if p["player"] in starters else
                                       "bench" if p["player"] in subs else "out")
            statuses[side] = st
    return absences, pdata, statuses, lineups


# ---------------------------------------------------------------- value

def evaluate_value(payload: dict, odds: dict) -> list[dict]:
    cfg = config.settings()["value"]
    mk = payload["markets"]
    out = []

    def add(market, sel, line, p, price, fair):
        a = value.assess(p, price, fair, cfg)
        out.append({"market": market, "selection": sel, "line": line, "model_prob": round(p, 4),
                    "fair_odds": round(1 / p, 2) if p > 0 else None, "price": price,
                    "market_prob": round(fair, 4), "edge": round(a["edge"], 4), "ev": round(a["ev"], 4),
                    "stake": round(a["stake"], 4), "is_value": a["is_value"], "suspect": a["suspect"]})

    h2h = odds.get("h2h", {})
    if all(k in h2h for k in ("home", "draw", "away")):
        prices = [h2h["home"], h2h["draw"], h2h["away"]]
        for sel, price, fair in zip(("home", "draw", "away"), prices, value.remove_vig(prices)):
            add("h2h", sel, None, mk["1x2"][sel], price, fair)
    for line, sides in odds.get("totals", {}).items():
        if "over" in sides and "under" in sides and line in mk["totals"]:
            prices = [sides["over"], sides["under"]]
            for sel, price, fair in zip(("over", "under"), prices, value.remove_vig(prices)):
                add("totals", sel, float(line), mk["totals"][line][sel], price, fair)
    bt = odds.get("btts", {})
    if "yes" in bt and "no" in bt:
        prices = [bt["yes"], bt["no"]]
        for sel, price, fair in zip(("yes", "no"), prices, value.remove_vig(prices)):
            add("btts", sel, None, mk["btts"][sel], price, fair)
    return out


def log_bets(conn, match_id: str, assessments: list[dict], taken_at: str) -> None:
    for a in assessments:
        if a["is_value"]:
            conn.execute(
                "INSERT OR IGNORE INTO bets (match_id, market, selection, line, model_prob, market_prob, odds_taken, "
                "edge, ev, stake, taken_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (match_id, a["market"], a["selection"], a["line"], a["model_prob"], a["market_prob"], a["price"],
                 a["edge"], a["ev"], a["stake"], taken_at))


# ---------------------------------------------------------------- main

def run(conn, mode: str = "daily") -> dict:
    notes: list[str] = []
    now = now_utc()
    cur = config.season_code(now)
    conn.execute("INSERT INTO runs (started_at, mode) VALUES (?, ?)", (iso(now), mode))

    if mode == "daily":
        football_data.update_history(conn, cur, notes)
        conn.commit()
    sync_fixtures(conn, notes)
    if mode == "daily":
        sync_referees(conn)
    sync_results(conn, notes)
    n_odds = sync_odds(conn, notes, timedelta(days=7) if mode == "daily" else timedelta(hours=3))
    conn.commit()

    df = load_matches(conn)
    horizon = iso(now + timedelta(days=PREDICT_HORIZON_DAYS))
    upcoming = df[(df.status == "SCHEDULED") & df.kickoff_utc.notna() & (df.kickoff_utc > iso(now))
                  & (df.kickoff_utc < horizon)]
    if mode == "prekickoff":
        upcoming = upcoming[upcoming.kickoff_utc < iso(now + timedelta(hours=3))]
    enrich(conn, notes, upcoming)
    conn.commit()

    n_pred = 0
    for key in config.leagues():
        ups = upcoming[upcoming.league == key]
        if ups.empty:
            continue
        model = fit_league(df, key, now.date(), fixture_teams=set(ups.home) | set(ups.away))
        if model is None:
            notes.append(f"{key}: not enough history to fit a model")
            continue
        for _, m in ups.iterrows():
            absences, pdata, statuses, lineups = match_inputs(conn, m, key, cur)
            ref = m.referee if isinstance(m.referee, str) else None
            p = predict_match(model, m.home, m.away, referee=ref, absences=absences,
                              player_data=pdata, lineup_status=statuses)
            odds = latest_odds(conn, m.id)
            p["odds"] = odds
            p["value"] = evaluate_value(p, odds) if odds else []
            p["lineups"] = lineups
            p["referee"] = ref
            p["absences"] = [dict(r) for r in conn.execute(
                "SELECT team, player, reason FROM absences WHERE match_id=?", (m.id,))]
            db.save_prediction(conn, m.id, iso(now), MODEL_VERSION, p)
            log_bets(conn, m.id, p["value"], iso(now))
            n_pred += 1

    tracking.update_closing_odds(conn)
    n_settled = tracking.settle_bets(conn)
    conn.commit()

    from .site import build
    build(conn, notes)
    conn.execute("UPDATE runs SET notes=? WHERE started_at=? AND mode=?", ("\n".join(notes), iso(now), mode))
    return {"mode": mode, "predictions": n_pred, "odds_rows": n_odds, "settled": n_settled, "notes": notes}
