"""Bulk API-Football history for the tracked clubs (data/teams_<season>.json), into data/detail.db.

For every club and season (settings.api_football.history.first_season .. current):
  1. team ids          /teams?league&season for the competitions in the team list
  2. competitions      /leagues?team&season  (with the per-season coverage flags)
  3. fixtures          /fixtures?league&season  for every competition found (all clubs, so opponents count)
  4. match detail      /fixtures?ids=a-b-...  (20 per request: team stats, lineups, player stats, events)
                       for finished league matches, all European matches, and cup matches of our clubs
  5. injuries          /injuries?league&season  where covered
  6. squads            /players?team&season (paged): every player's season stats per competition
  7. transfers         /transfers?team

Resumable: progress markers (af_progress) and af_fixtures.detail_at mean a run that hits the daily quota
continues where it stopped. Finished seasons are fetched once; the current season is refreshed each run.
Parsed rows are stored (raw responses are not kept: they would be gigabytes).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .. import config
from .api_football import ApiFootballUnavailable, Client

log = logging.getLogger(__name__)
FINISHED = ("FT", "AET", "PEN")
EUROPE_IDS = {2, 3, 848}
# API league ids whose /teams list gives the ids of the clubs in the team list
TEAM_LIST_LEAGUES = {"EPL": 39, "LALIGA": 140, "UCL": 2}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _i(x):
    try:
        return int(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


class Backfill:
    def __init__(self, conn, dconn, notes: list[str], client: Client | None = None):
        self.conn, self.d, self.notes = conn, dconn, notes
        self.client = client or Client(conn)
        hist = config.settings()["api_football"].get("history", {})
        self.batch = int(hist.get("batch_size", 20))
        self.exclude = [w.lower() for w in hist.get("exclude_league_words", ["friendl"])]
        self.requests = 0

    # ---------------------------------------------------------------- plumbing
    def _call(self, path: str, params: dict) -> dict:
        for attempt in (1, 2):
            try:
                body = self.client.fetch(path, params)
                self.requests += 1
                return body
            except ApiFootballUnavailable as e:
                if attempt == 1 and "ratelimit" in str(e).lower().replace(" ", ""):
                    time.sleep(61)      # per-minute limit: wait one window, retry once
                    continue
                raise

    def _paged(self, path: str, params: dict) -> list:
        out, page = [], 1
        while True:
            body = self._call(path, {**params, "page": page} if page > 1 else params)
            out += body.get("response") or []
            paging = body.get("paging") or {}
            if page >= int(paging.get("total") or 1):
                return out
            page += 1

    def _done(self, key: str) -> bool:
        return self.d.execute("SELECT 1 FROM af_progress WHERE key=?", (key,)).fetchone() is not None

    def _mark(self, key: str) -> None:
        self.d.execute("INSERT OR REPLACE INTO af_progress VALUES (?, ?)", (key, _now()))

    # ---------------------------------------------------------------- 1. team ids
    def team_ids(self, our_teams: dict[str, dict], season: int) -> dict[str, int]:
        """{our name: API team id} for the clubs in the team list."""
        have = {r["our_name"]: r["team_id"] for r in self.d.execute(
            "SELECT our_name, team_id FROM af_teams WHERE our_name IS NOT NULL")}
        missing = [t for t in our_teams if t not in have]
        if not missing:
            return {t: have[t] for t in our_teams}
        comps = {c for t in missing for c in our_teams[t]["competitions"]}
        known = set(our_teams)
        for comp in sorted(comps):
            lid = TEAM_LIST_LEAGUES.get(comp)
            if lid is None:
                continue
            for item in self._call("/teams", {"league": lid, "season": season}).get("response") or []:
                tm = item["team"]
                ours = config.canonical_team(tm["name"], known)
                self.d.execute(
                    "INSERT INTO af_teams (team_id, name, our_name, country, code, founded) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(team_id) DO UPDATE SET name=excluded.name, country=excluded.country, "
                    "our_name=COALESCE(af_teams.our_name, excluded.our_name)",
                    (tm["id"], tm["name"], ours if ours in known else None, tm.get("country"), tm.get("code"),
                     tm.get("founded")))
        self.d.commit()
        have = {r["our_name"]: r["team_id"] for r in self.d.execute(
            "SELECT our_name, team_id FROM af_teams WHERE our_name IS NOT NULL")}
        unmatched = [t for t in our_teams if t not in have]
        if unmatched:
            self.notes.append(f"API-Football: no team id for {unmatched} (map them in config/team_aliases.json)")
        return {t: have[t] for t in our_teams if t in have}

    # ---------------------------------------------------------------- 2. competitions
    def competitions(self, team_id: int, season: int, current: int) -> None:
        key = f"leagues:{team_id}:{season}"
        if season < current and self._done(key):
            return
        for item in self._call("/leagues", {"team": team_id, "season": season}).get("response") or []:
            lg = item["league"]
            if any(w in lg["name"].lower() for w in self.exclude):
                continue
            cov = next((s.get("coverage") or {} for s in item.get("seasons") or [] if s.get("year") == season), {})
            fx = cov.get("fixtures") or {}
            self.d.execute(
                "INSERT OR REPLACE INTO af_competitions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (lg["id"], season, lg["name"], lg.get("type"), (item.get("country") or {}).get("name"),
                 int(bool(fx.get("events"))), int(bool(fx.get("lineups"))), int(bool(fx.get("statistics_fixtures"))),
                 int(bool(fx.get("statistics_players"))), int(bool(cov.get("injuries"))), int(bool(cov.get("players")))))
            self.d.execute("INSERT OR IGNORE INTO af_team_competitions VALUES (?,?,?)", (team_id, season, lg["id"]))
        self._mark(key)
        self.d.commit()

    # ---------------------------------------------------------------- 3. fixtures
    def fixtures(self, league_id: int, season: int, current: int) -> int:
        key = f"fixtures:{league_id}:{season}"
        if season < current and self._done(key):
            return 0
        resp = self._call("/fixtures", {"league": league_id, "season": season}).get("response") or []
        for f in resp:
            self._store_fixture(f, season)
        self._mark(key)
        self.d.commit()
        return len(resp)

    def _store_fixture(self, f: dict, season: int) -> None:
        fx, lg, tm, g, sc = f["fixture"], f["league"], f["teams"], f.get("goals") or {}, f.get("score") or {}
        st = fx.get("status") or {}
        ht, et, pen = sc.get("halftime") or {}, sc.get("extratime") or {}, sc.get("penalty") or {}
        self.d.execute(
            "INSERT INTO af_fixtures (fixture_id, league_id, season, round, date, kickoff_utc, referee, venue, status, "
            "home_id, away_id, home, away, home_goals, away_goals, ht_home, ht_away, et_home, et_away, pen_home, "
            "pen_away) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fixture_id) DO UPDATE SET "
            "round=excluded.round, date=excluded.date, kickoff_utc=excluded.kickoff_utc, referee=excluded.referee, "
            "venue=excluded.venue, status=excluded.status, home_goals=excluded.home_goals, "
            "away_goals=excluded.away_goals, ht_home=excluded.ht_home, ht_away=excluded.ht_away, "
            "et_home=excluded.et_home, et_away=excluded.et_away, pen_home=excluded.pen_home, pen_away=excluded.pen_away",
            (fx["id"], lg["id"], season, lg.get("round"), (fx.get("date") or "")[:10], fx.get("date"), fx.get("referee"),
             (fx.get("venue") or {}).get("name"), st.get("short"), tm["home"]["id"], tm["away"]["id"],
             tm["home"]["name"], tm["away"]["name"], g.get("home"), g.get("away"), ht.get("home"), ht.get("away"),
             et.get("home"), et.get("away"), pen.get("home"), pen.get("away")))
        for side in ("home", "away"):
            self.d.execute("INSERT OR IGNORE INTO af_teams (team_id, name) VALUES (?, ?)",
                           (tm[side]["id"], tm[side]["name"]))

    # ---------------------------------------------------------------- 4. match detail
    def detail_todo(self, our_ids: set[int]) -> list[int]:
        ph = ",".join("?" * len(our_ids)) or "NULL"
        eu = ",".join(str(i) for i in EUROPE_IDS)
        return [r[0] for r in self.d.execute(
            f"SELECT f.fixture_id FROM af_fixtures f JOIN af_competitions c ON c.league_id=f.league_id AND "
            f"c.season=f.season WHERE f.detail_at IS NULL AND f.status IN ('FT','AET','PEN') "
            f"AND (c.events OR c.lineups OR c.stats_fixtures OR c.stats_players) "
            f"AND (c.type='League' OR f.league_id IN ({eu}) OR f.home_id IN ({ph}) OR f.away_id IN ({ph})) "
            f"ORDER BY f.date", (*our_ids, *our_ids))]

    def details(self, ids: list[int]) -> int:
        n = 0
        for i in range(0, len(ids), self.batch):
            chunk = ids[i:i + self.batch]
            resp = self._call("/fixtures", {"ids": "-".join(str(x) for x in chunk)}).get("response") or []
            for f in resp:
                self.store_detail(f)
                n += 1
            self.d.commit()
            if (i // self.batch) % 25 == 0:
                log.info("api-football detail: %d/%d fixtures", i + len(chunk), len(ids))
        return n

    def store_detail(self, f: dict) -> None:
        fid = f["fixture"]["id"]
        season = (f.get("league") or {}).get("season")
        self._store_fixture(f, season)
        for t in ("af_team_stats", "af_lineups", "af_player_match", "af_events"):
            self.d.execute(f"DELETE FROM {t} WHERE fixture_id=?", (fid,))
        for blk in f.get("statistics") or []:
            for s in blk.get("statistics") or []:
                self.d.execute("INSERT OR REPLACE INTO af_team_stats VALUES (?,?,?,?,?)",
                               (fid, season, blk["team"]["id"], s.get("type"),
                                None if s.get("value") is None else str(s["value"])))
        for lu in f.get("lineups") or []:
            coach = lu.get("coach") or {}
            for started, key in ((1, "startXI"), (0, "substitutes")):
                for p in lu.get(key) or []:
                    p = p.get("player") or {}
                    if p.get("id") is None:
                        continue
                    self.d.execute("INSERT OR REPLACE INTO af_lineups VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (fid, season, lu["team"]["id"], lu.get("formation"), coach.get("id"),
                                    coach.get("name"), p["id"], p.get("name"), p.get("number"), p.get("pos"),
                                    p.get("grid"), started))
        for blk in f.get("players") or []:
            for p in blk.get("players") or []:
                pl = p.get("player") or {}
                if pl.get("id") is None:
                    continue
                s = (p.get("statistics") or [{}])[0]
                gm, sh, go, pa = s.get("games") or {}, s.get("shots") or {}, s.get("goals") or {}, s.get("passes") or {}
                tk, du, dr, fo = s.get("tackles") or {}, s.get("duels") or {}, s.get("dribbles") or {}, s.get("fouls") or {}
                ca, pe = s.get("cards") or {}, s.get("penalty") or {}
                self.d.execute(
                    f"INSERT OR REPLACE INTO af_player_match VALUES ({','.join('?' * 38)})",
                    (fid, season, blk["team"]["id"], pl["id"], pl.get("name"), _i(gm.get("minutes")),
                     _i(gm.get("number")), gm.get("position"), _f(gm.get("rating")), int(bool(gm.get("captain"))),
                     int(bool(gm.get("substitute"))), _i(s.get("offsides")), _i(sh.get("total")), _i(sh.get("on")),
                     _i(go.get("total")), _i(go.get("conceded")), _i(go.get("assists")), _i(go.get("saves")),
                     _i(pa.get("total")), _i(pa.get("key")), _i(pa.get("accuracy")), _i(tk.get("total")),
                     _i(tk.get("blocks")), _i(tk.get("interceptions")), _i(du.get("total")), _i(du.get("won")),
                     _i(dr.get("attempts")), _i(dr.get("success")), _i(dr.get("past")), _i(fo.get("drawn")),
                     _i(fo.get("committed")), _i(ca.get("yellow")), _i(ca.get("red")), _i(pe.get("won")),
                     _i(pe.get("commited")), _i(pe.get("scored")), _i(pe.get("missed")), _i(pe.get("saved"))))
        for seq, e in enumerate(f.get("events") or []):
            tm, pl, asst = e.get("team") or {}, e.get("player") or {}, e.get("assist") or {}
            t = e.get("time") or {}
            self.d.execute("INSERT OR REPLACE INTO af_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (fid, season, seq, tm.get("id"), t.get("elapsed"), t.get("extra"), e.get("type"),
                            e.get("detail"), pl.get("id"), pl.get("name"), asst.get("id"), asst.get("name"),
                            e.get("comments")))
        self.d.execute("UPDATE af_fixtures SET detail_at=? WHERE fixture_id=?", (_now(), fid))

    # ---------------------------------------------------------------- 5-7. injuries, squads, transfers
    def injuries(self, league_id: int, season: int, current: int) -> int:
        key = f"injuries:{league_id}:{season}"
        if season < current and self._done(key):
            return 0
        rows = self._paged("/injuries", {"league": league_id, "season": season})
        for r in rows:
            pl, tm, fx = r.get("player") or {}, r.get("team") or {}, r.get("fixture") or {}
            if pl.get("id") is None or fx.get("id") is None:
                continue
            self.d.execute("INSERT OR REPLACE INTO af_injuries VALUES (?,?,?,?,?,?,?,?,?)",
                           (fx["id"], season, league_id, tm.get("id"), pl["id"], pl.get("name"), pl.get("type"),
                            pl.get("reason"), (fx.get("date") or "")[:10]))
        self._mark(key)
        self.d.commit()
        return len(rows)

    def squad(self, team_id: int, season: int, current: int) -> int:
        key = f"players:{team_id}:{season}"
        if season < current and self._done(key):
            return 0
        rows = self._paged("/players", {"team": team_id, "season": season})
        for r in rows:
            p = r.get("player") or {}
            for s in r.get("statistics") or []:
                gm, sh, go, pa = s.get("games") or {}, s.get("shots") or {}, s.get("goals") or {}, s.get("passes") or {}
                tk, du, dr, fo = s.get("tackles") or {}, s.get("duels") or {}, s.get("dribbles") or {}, s.get("fouls") or {}
                ca, pe = s.get("cards") or {}, s.get("penalty") or {}
                lg = s.get("league") or {}
                self.d.execute(
                    f"INSERT OR REPLACE INTO af_player_seasons VALUES ({','.join('?' * 30)})",
                    (season, p.get("id"), (s.get("team") or {}).get("id"), lg.get("id"), p.get("name"),
                     p.get("firstname"), p.get("lastname"), (p.get("birth") or {}).get("date"), p.get("nationality"),
                     p.get("height"), p.get("weight"), gm.get("position"), _i(gm.get("appearences")),
                     _i(gm.get("lineups")), _i(gm.get("minutes")), _f(gm.get("rating")), _i(go.get("total")),
                     _i(go.get("assists")), _i(sh.get("total")), _i(sh.get("on")), _i(pa.get("total")),
                     _i(pa.get("key")), _i(tk.get("total")), _i(du.get("won")), _i(dr.get("success")),
                     _i(fo.get("committed")), _i(ca.get("yellow")), _i(ca.get("red")), _i(pe.get("scored")),
                     _i(pe.get("missed"))))
        self._mark(key)
        self.d.commit()
        return len(rows)

    def transfers(self, team_id: int) -> int:
        key = f"transfers:{team_id}:{_now()[:10]}"
        if self._done(key):
            return 0
        rows = self._call("/transfers", {"team": team_id}).get("response") or []
        for r in rows:
            p = r.get("player") or {}
            for t in r.get("transfers") or []:
                tin, tout = (t.get("teams") or {}).get("in") or {}, (t.get("teams") or {}).get("out") or {}
                self.d.execute("INSERT OR REPLACE INTO af_transfers VALUES (?,?,?,?,?,?,?,?)",
                               (p.get("id"), p.get("name"), t.get("date"), t.get("type"), tin.get("id"),
                                tin.get("name"), tout.get("id"), tout.get("name")))
        self._mark(key)
        self.d.commit()
        return len(rows)

    # ---------------------------------------------------------------- quick real-data check
    def sample(self, our_teams: dict[str, dict], current: int) -> dict:
        """~8 requests: team ids, last season's Premier League fixtures, 20 match details, one squad and
        one injuries page - to check parsing against real responses before a full run."""
        ids = self.team_ids(our_teams, current)
        last = current - 1
        self.competitions(ids["Arsenal"], last, current)
        self.fixtures(39, last, current)
        todo = [r[0] for r in self.d.execute("SELECT fixture_id FROM af_fixtures WHERE league_id=39 AND season=? "
                                             "AND status IN ('FT','AET','PEN') ORDER BY date LIMIT ?", (last, self.batch))]
        self.details(todo)
        self.squad(ids["Arsenal"], last, current)
        body = self._call("/injuries", {"league": 39, "season": last})
        tables = [r[0] for r in self.d.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'af_%'")]
        return {"requests": self.requests, "teams_mapped": len(ids), "injury_rows_page1": len(body.get("response") or []),
                "injury_pages": (body.get("paging") or {}).get("total"),
                "rows": {t: self.d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables},
                "player_match_example": dict(self.d.execute("SELECT * FROM af_player_match ORDER BY rating DESC LIMIT 1").fetchone() or {}),
                "team_stats_names": [r[0] for r in self.d.execute("SELECT DISTINCT stat FROM af_team_stats")]}

    # ---------------------------------------------------------------- everything
    def run(self, our_teams: dict[str, dict], current: int, first: int | None = None) -> dict:
        first = first or int(config.settings()["api_football"].get("history", {}).get("first_season", current - 5))
        seasons = list(range(first, current + 1))
        out = {"requests": 0}
        try:
            ids = self.team_ids(our_teams, current)
            out["teams"] = len(ids)
            for tid in ids.values():
                for s in seasons:
                    self.competitions(tid, s, current)
            pairs = self.d.execute("SELECT DISTINCT league_id, season FROM af_team_competitions WHERE season>=? "
                                   "ORDER BY season, league_id", (first,)).fetchall()
            out["competitions"] = len(pairs)
            out["fixtures"] = sum(self.fixtures(lid, s, current) for lid, s in pairs)
            todo = self.detail_todo(set(ids.values()))
            out["detail_todo"] = len(todo)
            out["detail"] = self.details(todo)
            inj = self.d.execute("SELECT league_id, season FROM af_competitions WHERE injuries=1 AND season>=? AND "
                                 "(league_id, season) IN (SELECT league_id, season FROM af_team_competitions)",
                                 (first,)).fetchall()
            out["injuries"] = sum(self.injuries(lid, s, current) for lid, s in inj)
            out["squad_rows"] = sum(self.squad(tid, s, current) for tid in ids.values() for s in seasons)
            out["transfers"] = sum(self.transfers(tid) for tid in ids.values())
            out["complete"] = True
        except ApiFootballUnavailable as e:
            self.notes.append(f"API-Football history stopped (resumes next run): {e}")
            out["complete"] = False
        out["requests"] = self.requests
        self.d.commit()
        return out


def load_team_list(season_label: str | None = None) -> dict[str, dict]:
    path = sorted(config.DATA_DIR.glob("teams_*.json"))[-1] if season_label is None else \
        config.DATA_DIR / f"teams_{season_label}.json"
    return json.loads(path.read_text())["teams"]
