"""data/warehouse.db: one clean, unified copy of all match data (rebuilt from scratch, never edited by hand).

Inputs:  data/match.db  - results, football-data match stats + closing odds, Understat match xG, seeds
         data/detail.db - Understat player rows + shots, API-Football history (af_* tables)
Output:  one row per real match under our match id, our club names (football-data spellings), one id per
         player, every column taken from the best source for it (see docs/DATA_MODEL.md).

Rebuild: python -m matchpredictor warehouse   (seconds to a few minutes; safe to run any time)
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import config

WAREHOUSE_DB = config.DATA_DIR / "warehouse.db"
FINISHED = ("FT", "AET", "PEN")

SCHEMA = """
CREATE TABLE competitions (comp TEXT PRIMARY KEY, name TEXT, type TEXT, country TEXT, af_league_id INTEGER);
CREATE TABLE teams (team TEXT PRIMARY KEY, country TEXT, af_team_id INTEGER, tracked INTEGER, domestic_comp TEXT);
CREATE TABLE matches (
    match_id TEXT PRIMARY KEY, comp TEXT, season TEXT, date TEXT, kickoff_utc TEXT, round TEXT,
    home TEXT, away TEXT, status TEXT,
    home_goals INTEGER, away_goals INTEGER,            -- 90 minutes
    ht_home INTEGER, ht_away INTEGER, et_home INTEGER, et_away INTEGER, pen_home INTEGER, pen_away INTEGER,
    home_xg REAL, away_xg REAL, xg_source TEXT,          -- Understat shot sum > Understat page > API-Football
    home_xg_page REAL, away_xg_page REAL,              -- Understat's league-page value (older model in 2021-23)
    referee TEXT, venue TEXT,
    close_home REAL, close_draw REAL, close_away REAL, close_over25 REAL, close_under25 REAL,
    af_fixture_id INTEGER, understat_id TEXT, sources TEXT);
CREATE INDEX ix_m_date ON matches (date);
CREATE INDEX ix_m_home ON matches (home);
CREATE INDEX ix_m_away ON matches (away);
CREATE TABLE team_match_stats (
    match_id TEXT, team TEXT, side TEXT, opponent TEXT, goals INTEGER, conceded INTEGER, xg REAL, xg_against REAL,
    shots INTEGER, shots_on_target INTEGER, shots_inside_box INTEGER, corners INTEGER, fouls INTEGER,
    offsides INTEGER, yellow INTEGER, red INTEGER, possession REAL, passes INTEGER, pass_accuracy REAL,
    saves INTEGER, stats_source TEXT, PRIMARY KEY (match_id, team));
CREATE TABLE players (
    player_id TEXT PRIMARY KEY, name TEXT, birth_date TEXT, nationality TEXT, position TEXT,
    af_player_id INTEGER, understat_player_id INTEGER);
CREATE TABLE player_match_stats (
    match_id TEXT, team TEXT, player_id TEXT, player TEXT, started INTEGER, minutes INTEGER, position TEXT,
    number INTEGER, rating REAL, captain INTEGER, goals INTEGER, own_goals INTEGER, assists INTEGER,
    shots INTEGER, shots_on_target INTEGER, key_passes INTEGER, xg REAL, xa REAL, xg_chain REAL, xg_buildup REAL,
    passes INTEGER, pass_accuracy INTEGER, tackles INTEGER, interceptions INTEGER, blocks INTEGER, duels INTEGER,
    duels_won INTEGER, dribbles INTEGER, dribbles_won INTEGER, fouls_committed INTEGER, fouls_drawn INTEGER,
    yellow INTEGER, red INTEGER, pen_scored INTEGER, pen_missed INTEGER, pen_saved INTEGER, saves INTEGER,
    conceded INTEGER, sources TEXT, PRIMARY KEY (match_id, team, player_id));
CREATE INDEX ix_pms_player ON player_match_stats (player_id);
CREATE TABLE lineups (match_id TEXT, team TEXT, player_id TEXT, player TEXT, started INTEGER, position TEXT,
    number INTEGER, grid TEXT, formation TEXT, coach TEXT, PRIMARY KEY (match_id, team, player_id));
CREATE TABLE shots (match_id TEXT, team TEXT, shot_id INTEGER PRIMARY KEY, minute INTEGER, player_id TEXT,
    player TEXT, result TEXT, x REAL, y REAL, xg REAL, situation TEXT, shot_type TEXT, assisted_by TEXT,
    last_action TEXT);
CREATE TABLE events (match_id TEXT, seq INTEGER, team TEXT, minute INTEGER, extra INTEGER, type TEXT, detail TEXT,
    player_id TEXT, player TEXT, assist_id TEXT, assist TEXT, PRIMARY KEY (match_id, seq));
CREATE TABLE injuries (match_id TEXT, team TEXT, player_id TEXT, player TEXT, type TEXT, reason TEXT,
    PRIMARY KEY (match_id, player_id));
CREATE TABLE squads (season TEXT, team TEXT, comp TEXT, player_id TEXT, player TEXT, position TEXT,
    appearances INTEGER, starts INTEGER, minutes INTEGER, rating REAL, goals INTEGER, assists INTEGER,
    shots INTEGER, shots_on_target INTEGER, key_passes INTEGER, yellow INTEGER, red INTEGER,
    PRIMARY KEY (season, team, comp, player_id));
CREATE TABLE transfers (player_id TEXT, player TEXT, date TEXT, type TEXT, team_in TEXT, team_out TEXT,
    PRIMARY KEY (player_id, date, team_in, team_out));
CREATE TABLE build_log (step TEXT PRIMARY KEY, rows INTEGER, note TEXT);
"""

# relegation / promotion play-offs that API-Football files under a top league ("Relegation Round", "Final")
_PLAYOFF = re.compile(r"relegation|play-?off|final|promotion", re.I)

# youth, reserve and women's sides: never mapped onto a senior club by name
_YOUTH = re.compile(r"(\bU-?\d{2}\b|\bII\b|\bB\b$|\bW\b|\bWomen\b|\bFemenino\b|\bFrauen\b|\bReserves?\b|\bJong\b|"
                    r"\bPrimavera\b|\bAcademy\b|\bYouth\b|\bJuvenil\b|\bSub-?\d{2}\b)", re.I)

# API-Football team statistic name -> team_match_stats column
AF_STAT = {"Total Shots": "shots", "Shots on Goal": "shots_on_target", "Shots insidebox": "shots_inside_box",
           "Corner Kicks": "corners", "Fouls": "fouls", "Offsides": "offsides", "Yellow Cards": "yellow",
           "Red Cards": "red", "Ball Possession": "possession", "Total passes": "passes",
           "Passes %": "pass_accuracy", "Goalkeeper Saves": "saves", "expected_goals": "xg"}


def af_season_code(season: int) -> str:
    """API-Football season start year -> our code: 2025 -> '2526'."""
    return f"{season % 100:02d}{(season + 1) % 100:02d}"


def _num(v):
    if v is None:
        return None
    s = str(v).strip().rstrip("%")
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- player names
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ø", "o").replace("ß", "ss"))
    s = s.encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z ]+", " ", s).split())


def name_key(name: str) -> tuple[str, str]:
    """('saka', 'b') for 'B. Saka' and 'Bukayo Saka': last name + first initial ('' for one-word names)."""
    toks = _norm(name).split()
    if not toks:
        return "", ""
    return toks[-1], (toks[0][0] if len(toks) > 1 else "")


def same_player(a: str, b: str) -> bool:
    (la, ia), (lb, ib) = name_key(a), name_key(b)
    if not la or not lb:
        return False
    if la == lb:
        return not ia or not ib or ia == ib
    na, nb = _norm(a), _norm(b)     # one-word names ('Rodri') vs full names ('Rodrigo Hernandez Cascante')
    return (not ia and la in nb.split()) or (not ib and lb in na.split())


class Builder:
    def __init__(self, main_path: Path | str, detail_path: Path | str, out_path: Path | str | None = None):
        self.out = Path(out_path or WAREHOUSE_DB)
        tmp = self.out.with_suffix(".building")
        tmp.unlink(missing_ok=True)
        self.tmp = tmp
        self.w = sqlite3.connect(str(tmp))
        self.w.executescript(SCHEMA)
        self.w.execute("ATTACH DATABASE ? AS m", (str(main_path),))
        self.w.execute("ATTACH DATABASE ? AS d", (str(detail_path),))
        self.has_af = bool(self.w.execute("SELECT COUNT(*) FROM d.sqlite_master WHERE name='af_fixtures'").fetchone()[0])
        self.has_us = bool(self.w.execute("SELECT COUNT(*) FROM d.sqlite_master WHERE name='player_matches'").fetchone()[0])
        self.log: dict[str, tuple[int, str]] = {}
        self.comp_type: dict[str, str] = {}       # comp -> league | extra | europe | cup | ...
        self.comp_country: dict[str, str] = {}    # comp -> country name as API-Football writes it
        self.score_mismatch: list[str] = []
        self.score_fixed: list[str] = []
        self.top_leagues = set(config.leagues())   # pairing happens once per season (match id without date)

    def _log(self, step: str, rows: int, note: str = "") -> None:
        self.log[step] = (rows, note)

    # ---------------------------------------------------------------- reference data
    def competitions(self) -> dict[int, str]:
        """{API league id: comp code}. Our competitions keep their codes; others become 'AF<id>'."""
        comps = config.competitions(enabled_only=False)
        af_to_comp = {}
        for code, c in comps.items():
            self.w.execute("INSERT INTO competitions VALUES (?,?,?,?,?)",
                           (code, c.get("name"), c.get("type"), c.get("country"), c.get("api_football_id")))
            self.comp_type[code] = c.get("type")
            if c.get("api_football_id"):
                af_to_comp[int(c["api_football_id"])] = code
        if self.has_af:
            for lid, name, typ, country in self.w.execute(
                    "SELECT league_id, name, type, country FROM d.af_competitions GROUP BY league_id").fetchall():
                if lid not in af_to_comp:
                    af_to_comp[lid] = f"AF{lid}"
                    self.comp_type[f"AF{lid}"] = (typ or "").lower() or None
                    self.w.execute("INSERT INTO competitions VALUES (?,?,?,?,?)",
                                   (f"AF{lid}", name, self.comp_type[f"AF{lid}"], country, lid))
                else:      # our competition: keep our row, record the API's country name
                    self.w.execute("UPDATE competitions SET country=? WHERE comp=? AND ? IS NOT NULL",
                                   (country, af_to_comp[lid], country))
                if country:
                    self.comp_country[af_to_comp[lid]] = country
        self._log("competitions", len(af_to_comp))
        return af_to_comp

    def teams(self, af_to_comp: dict[int, str]) -> dict[int, str]:
        """{API team id: our name}. API clubs map onto our names only when the countries agree (so a
        foreign 'Inter ...' can never become Inter); unknown clubs keep the API name."""
        known = {r[0] for r in self.w.execute("SELECT home FROM m.matches UNION SELECT away FROM m.matches")}
        types = self.comp_type
        country_of_comp = self.comp_country
        domestic = {}
        for team, comp, n in self.w.execute(
                "SELECT team, league, COUNT(*) n FROM (SELECT home team, league FROM m.matches UNION ALL "
                "SELECT away, league FROM m.matches) GROUP BY team, league ORDER BY n").fetchall():
            if types.get(comp) in ("league", "extra"):
                domestic[team] = comp            # most-played domestic league wins (ORDER BY n)
        tracked = set(config.settings().get("tracked_teams", []))
        try:
            from .ingest.api_football_history import load_team_list
            tracked |= set(load_team_list())
        except (IndexError, OSError):
            pass
        af_map = self.map_af_teams(known, domestic, af_to_comp) if self.has_af else {}
        for team in sorted(known | set(af_map.values())):
            self.w.execute("INSERT OR IGNORE INTO teams VALUES (?,?,?,?,?)",
                           (team, country_of_comp.get(domestic.get(team)), None, int(team in tracked),
                            domestic.get(team)))
        for tid, team in af_map.items():
            self.w.execute("UPDATE teams SET af_team_id=COALESCE(af_team_id, ?) WHERE team=?", (tid, team))
        self._log("teams", self.w.execute("SELECT COUNT(*) FROM teams").fetchone()[0])
        return af_map

    # ---------------------------------------------------------------- club mapping
    def map_af_teams(self, known: set[str], domestic: dict[str, str], af_to_comp: dict[int, str]) -> dict[int, str]:
        """{API team id: warehouse club name}. Evidence first, names second:
        1. trusted: af_teams.our_name (the team list, resolved by the backfill)
        2. fixture alignment: an API club whose matches in one of our competitions line up with ours on
           date (+-1 day) and score at least 3 times, in >= 70% of its aligned matches, takes that name
        3. name match (alias / exact / fuzzy) only when the countries agree, never for youth, reserve or
           women's sides ('Manchester United U21' is not Man United)
        Each of our names goes to one API club (the one with the most evidence); every other API club keeps
        its own name, made unique with its country when it collides."""
        rows = self.w.execute("SELECT team_id, name, our_name, country FROM d.af_teams").fetchall()
        # country of every API club: its own, else the country of the competitions it plays in
        comp_country = {r[0]: r[1] for r in self.w.execute("SELECT league_id, country FROM d.af_competitions")}
        played = defaultdict(Counter)
        for lid, h, a in self.w.execute("SELECT league_id, home_id, away_id FROM d.af_fixtures"):
            c = comp_country.get(lid)
            if c and c != "World":
                played[h][c] += 1
                played[a][c] += 1
        country = {tid: (c if c and c != "World" else (played[tid].most_common(1)[0][0] if played[tid] else None))
                   for tid, _, _, c in rows}
        # evidence: fixture alignment in our competitions
        ours_by = defaultdict(list)
        for comp, d_, h, a, hg, ag in self.w.execute(
                "SELECT league, date, home, away, fthg, ftag FROM m.matches WHERE status='FT' AND fthg IS NOT NULL"):
            ours_by[(comp, d_)].append((h, a, hg, ag))
        votes = defaultdict(Counter)
        for lid, d_, hid, aid, st, hg, ag, eth, eta in self.w.execute(
                "SELECT league_id, date, home_id, away_id, status, home_goals, away_goals, et_home, et_away "
                "FROM d.af_fixtures WHERE status IN ('FT','AET','PEN')"):
            comp = af_to_comp.get(lid)
            if not comp or comp.startswith("AF") or not d_:
                continue
            if st != "FT":
                hg, ag = hg - (eth or 0), ag - (eta or 0)
            day = date.fromisoformat(d_)
            cands = [m for k in (-1, 0, 1) for m in ours_by.get((comp, (day + timedelta(days=k)).isoformat()), [])
                     if (m[2], m[3]) == (hg, ag)]
            if len(cands) == 1:
                votes[hid][cands[0][0]] += 1
                votes[aid][cands[0][1]] += 1
        claim: dict[int, tuple[str, float]] = {}        # API id -> (our name, strength)
        for tid, name, ours, _ in rows:
            if ours:
                claim[tid] = (ours, 1e9)
                continue
            v = votes.get(tid)
            if v:
                best, n = v.most_common(1)[0]
                if n >= 3 and n >= 0.7 * sum(v.values()):
                    claim[tid] = (best, float(n))
                    continue
            if _YOUTH.search(name or ""):
                continue
            cand = config.canonical_team(name, known)
            if cand in known:
                ours_country = self.comp_country.get(domestic.get(cand))
                if country.get(tid) and ours_country and _norm(ours_country) == _norm(country[tid]):
                    claim[tid] = (cand, 0.5 + sum(played[tid].values()) / 1e6)
                elif not ours_country and country.get(tid) is None and cand == name:
                    claim[tid] = (cand, 0.1)            # same exact name, nothing to contradict it
        owner: dict[str, int] = {}
        for tid, (name, strength) in claim.items():
            if name not in owner or strength > claim[owner[name]][1]:
                owner[name] = tid
        out, used = {}, set(known)
        for tid, name, _, _ in rows:
            if tid in claim and owner.get(claim[tid][0]) == tid:
                out[tid] = claim[tid][0]
                continue
            own = name or f"team {tid}"
            if own in used:
                own = f"{own} ({country.get(tid) or tid})"
                if own in used:
                    own = f"{name} #{tid}"
            out[tid] = own
            used.add(own)
        self.mapping_votes, self.af_claims = votes, claim
        return out

    # ---------------------------------------------------------------- matches
    def matches(self, af_to_comp: dict[int, str], af_team: dict[int, str]) -> dict[int, str]:
        """Our match rows first, then API-Football fixtures merged onto them (or added). {fixture: match_id}"""
        self.w.execute("""
            INSERT INTO matches (match_id, comp, season, date, kickoff_utc, home, away, status, home_goals, away_goals,
                home_xg, away_xg, xg_source, referee, close_home, close_draw, close_away, close_over25, close_under25,
                understat_id, sources)
            SELECT id, league, season, date, kickoff_utc, home, away, status, fthg, ftag, xg_home, xg_away,
                CASE WHEN xg_home IS NOT NULL THEN 'understat' END, referee, close_h, close_d, close_a, close_o25,
                close_u25, understat_id, source FROM m.matches""")
        fixture_to_match: dict[int, str] = {}
        if not self.has_af:
            return fixture_to_match
        types = self.comp_type
        # (comp, home, away) -> [(date, match_id)] for cup / European lookups by date
        by_pair = defaultdict(list)
        for mid, comp, d_, h, a in self.w.execute("SELECT match_id, comp, date, home, away FROM matches"):
            by_pair[(comp, h, a)].append((d_, mid))
        added = merged = 0
        claimed: set[str] = set()          # match ids already matched to an API fixture
        have = {r[1] for r in self.w.execute("PRAGMA d.table_info(af_fixtures)")}
        ft = "ft_home, ft_away" if "ft_home" in have else "NULL, NULL"     # older detail.db / archives
        rows = self.w.execute(
            "SELECT fixture_id, league_id, season, round, date, kickoff_utc, referee, venue, status, home_id, away_id, "
            f"home, away, home_goals, away_goals, ht_home, ht_away, et_home, et_away, pen_home, pen_away, {ft} "
            "FROM d.af_fixtures").fetchall()
        for (fid, lid, season, rnd, d_, ko, ref, venue, st, hid, aid, hname, aname, hg, ag, hth, hta, eth, eta,
             penh, pena, fth, fta) in rows:
            if st not in FINISHED and st not in ("NS", "TBD"):
                continue        # cancelled, postponed, abandoned, awarded: not a match that was (or will be) played
            comp = af_to_comp.get(lid, f"AF{lid}")
            if comp in self.top_leagues and _PLAYOFF.search(rnd or ""):
                comp = self._playoff_comp(comp)          # a relegation play-off is not a league game
            home, away = af_team.get(hid, hname), af_team.get(aid, aname)
            finished = st in FINISHED
            if finished and fth is None and hg is not None:     # older archives: goals include extra time
                fth, fta = (hg - (eth or 0), ag - (eta or 0)) if st in ("AET", "PEN") else (hg, ag)
            mid = None
            for md, cand in by_pair.get((comp, home, away), []):
                if cand in claimed:
                    continue
                if md and d_ and abs((date.fromisoformat(md) - date.fromisoformat(d_)).days) <= 3:
                    mid = cand
                    break
            if mid is None and comp in self.top_leagues:       # one pairing per season: rescheduled games still merge
                hit = self.w.execute("SELECT match_id FROM matches WHERE comp=? AND season=? AND home=? AND away=?",
                                     (comp, af_season_code(season), home, away)).fetchone()
                mid = hit[0] if hit and hit[0] not in claimed else None   # a play-off is not the league game
            if mid and finished and fth is not None:
                ours = self.w.execute("SELECT home_goals, away_goals FROM matches WHERE match_id=?", (mid,)).fetchone()
                if ours and ours[0] is not None and (ours[0], ours[1]) != (fth, fta):
                    if st in ("AET", "PEN") and (ours[0], ours[1]) == (hg, ag):
                        # our seed stored the score after extra time: API-Football's 90-minute score is right
                        self.w.execute("UPDATE matches SET home_goals=?, away_goals=? WHERE match_id=?", (fth, fta, mid))
                        self.score_fixed.append(mid)
                    else:
                        self.score_mismatch.append(f"{mid}: ours {ours[0]}-{ours[1]}, API-Football {fth}-{fta} ({st})")
            if mid:
                self.w.execute(
                    "UPDATE matches SET af_fixture_id=?, round=COALESCE(round, ?), venue=COALESCE(venue, ?), "
                    "referee=COALESCE(referee, ?), kickoff_utc=COALESCE(kickoff_utc, ?), ht_home=?, ht_away=?, "
                    "et_home=?, et_away=?, pen_home=?, pen_away=?, "
                    "home_goals=COALESCE(home_goals, ?), away_goals=COALESCE(away_goals, ?), "
                    "status=CASE WHEN status='FT' THEN status WHEN ? THEN 'FT' ELSE status END, "
                    "sources=sources || ',api-football' WHERE match_id=?",
                    (fid, rnd, venue, ref, ko, hth, hta, eth, eta, penh, pena, fth, fta, int(finished), mid))
                merged += 1
            else:
                season_code = config.season_code(d_) if d_ else af_season_code(season)
                # same id rules as db.match_id: only our top-level leagues omit the date (and a second game of the
                # same pairing in one season - a play-off - gets the dated form)
                mid = f"{comp}:{season_code}:{home}:{away}" if comp in self.top_leagues else f"{comp}:{d_}:{home}:{away}"
                if self.w.execute("SELECT 1 FROM matches WHERE match_id=?", (mid,)).fetchone():
                    mid = f"{comp}:{d_}:{home}:{away}"
                self.w.execute(
                    "INSERT OR IGNORE INTO matches (match_id, comp, season, date, kickoff_utc, round, home, away, "
                    "status, home_goals, away_goals, ht_home, ht_away, et_home, et_away, pen_home, pen_away, referee, "
                    "venue, af_fixture_id, sources) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, comp, season_code, d_, ko, rnd, home, away, "FT" if finished else (st or "SCHEDULED"),
                     fth if finished else None, fta if finished else None, hth, hta, eth, eta, penh, pena, ref, venue,
                     fid, "api-football"))
                by_pair[(comp, home, away)].append((d_, mid))
                added += 1
            fixture_to_match[fid] = mid
            claimed.add(mid)
        self._log("matches", self.w.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                  f"{merged} API-Football fixtures merged onto existing matches, {added} added")
        return fixture_to_match

    def _playoff_comp(self, comp: str) -> str:
        code = f"{comp}_PO"
        if code not in self.comp_type:
            name = config.competitions(enabled_only=False).get(comp, {}).get("name", comp)
            self.comp_type[code] = "playoff"
            self.w.execute("INSERT OR IGNORE INTO competitions VALUES (?,?,?,?,?)",
                           (code, f"{name} relegation play-off", "playoff", self.comp_country.get(comp), None))
        return code

    # ---------------------------------------------------------------- team stats
    def team_stats(self, fixture_to_match: dict[int, str], af_team: dict[int, str]) -> None:
        """football-data stats where present (the source the market models were tuned on), else API-Football.
        xG: Understat where present, else API-Football expected_goals."""
        self.w.execute("""
            INSERT INTO team_match_stats (match_id, team, side, opponent, goals, conceded, xg, xg_against, shots,
                shots_on_target, corners, fouls, yellow, red, stats_source)
            SELECT m.match_id, m.home, 'h', m.away, m.home_goals, m.away_goals, m.home_xg, m.away_xg, o.hs, o.hst,
                   o.hc, o.hf, o.hy, o.hr, CASE WHEN o.hs IS NOT NULL THEN 'football-data' END
            FROM matches m JOIN m.matches o ON o.id = m.match_id WHERE m.status='FT'
            UNION ALL
            SELECT m.match_id, m.away, 'a', m.home, m.away_goals, m.home_goals, m.away_xg, m.home_xg, o.as_, o.ast,
                   o.ac, o."af", o.ay, o.ar, CASE WHEN o.as_ IS NOT NULL THEN 'football-data' END
            FROM matches m JOIN m.matches o ON o.id = m.match_id WHERE m.status='FT'""")
        if self.has_af:
            side_of = {mid: (h, a) for mid, h, a in self.w.execute("SELECT match_id, home, away FROM matches")}
            stats = defaultdict(dict)
            for fid, tid, stat, value in self.w.execute("SELECT fixture_id, team_id, stat, value FROM d.af_team_stats"):
                col = AF_STAT.get(stat)
                mid = fixture_to_match.get(fid)
                if col and mid:
                    stats[(mid, af_team.get(tid))][col] = _num(value)
            for (mid, team), vals in stats.items():
                h, a = side_of.get(mid, (None, None))
                if team not in (h, a):
                    continue
                side, opp = ("h", a) if team == h else ("a", h)
                self.w.execute("INSERT OR IGNORE INTO team_match_stats (match_id, team, side, opponent) VALUES (?,?,?,?)",
                               (mid, team, side, opp))
                fill = {k: v for k, v in vals.items() if k != "xg"}
                sets = ", ".join(f"{k}=COALESCE({k}, ?)" for k in fill)
                self.w.execute(f"UPDATE team_match_stats SET {sets + ', ' if sets else ''}"
                               "stats_source=COALESCE(stats_source, 'api-football') WHERE match_id=? AND team=?",
                               (*fill.values(), mid, team))
                if vals.get("xg") is not None:
                    col = "home_xg" if side == "h" else "away_xg"
                    self.w.execute(f"UPDATE matches SET {col}=COALESCE({col}, ?), "
                                   "xg_source=COALESCE(xg_source, 'api-football') WHERE match_id=?", (vals["xg"], mid))
            self.w.execute("""UPDATE team_match_stats SET
                goals=(SELECT CASE WHEN side='h' THEN home_goals ELSE away_goals END FROM matches m WHERE m.match_id=team_match_stats.match_id),
                conceded=(SELECT CASE WHEN side='h' THEN away_goals ELSE home_goals END FROM matches m WHERE m.match_id=team_match_stats.match_id),
                xg=(SELECT CASE WHEN side='h' THEN home_xg ELSE away_xg END FROM matches m WHERE m.match_id=team_match_stats.match_id),
                xg_against=(SELECT CASE WHEN side='h' THEN away_xg ELSE home_xg END FROM matches m WHERE m.match_id=team_match_stats.match_id)""")
        self._log("team_match_stats", self.w.execute("SELECT COUNT(*) FROM team_match_stats").fetchone()[0])

    # ---------------------------------------------------------------- players
    def link_players(self, fixture_to_match: dict[int, str], af_team: dict[int, str]) -> dict[int, int]:
        """{Understat player id: API-Football player id}, by name agreement in the same match and team,
        majority vote over every shared match (a transfer or a namesake cannot flip a well-attested link)."""
        if not (self.has_af and self.has_us):
            return {}
        match_of = fixture_to_match
        af_rows = defaultdict(list)
        for fid, tid, pid, name in self.w.execute("SELECT fixture_id, team_id, player_id, player FROM d.af_player_match"):
            mid = match_of.get(fid)
            if mid:
                af_rows[(mid, af_team.get(tid))].append((pid, name))
        votes = Counter()
        for mid, team, pid, name in self.w.execute("SELECT match_id, team, player_id, player FROM d.player_matches"):
            cands = [a for a, an in af_rows.get((mid, team), []) if same_player(name, an)]
            if len(cands) == 1:
                votes[(pid, cands[0])] += 1
        best: dict[int, tuple[int, int]] = {}
        for (us, af), n in votes.items():
            if us not in best or n > best[us][1]:
                best[us] = (af, n)
        return {us: af for us, (af, n) in best.items()}

    def players(self, fixture_to_match: dict[int, str], af_team: dict[int, str]) -> None:
        us_to_af = self.link_players(fixture_to_match, af_team)
        pid = lambda af=None, us=None: f"af:{af}" if af is not None else (f"af:{us_to_af[us]}" if us in us_to_af else f"us:{us}")
        # players table: API-Football identity (with birth date / nationality from squads) + Understat-only
        if self.has_af:
            self.w.execute("""INSERT OR IGNORE INTO players (player_id, name, birth_date, nationality, position, af_player_id)
                SELECT 'af:' || player_id, player, birth_date, nationality, position, player_id
                FROM d.af_player_seasons WHERE player_id IS NOT NULL GROUP BY player_id""")
            self.w.execute("""INSERT OR IGNORE INTO players (player_id, name, af_player_id)
                SELECT 'af:' || player_id, player, player_id FROM d.af_player_match GROUP BY player_id""")
        if self.has_us:
            for us, name, pos in self.w.execute(
                    "SELECT player_id, player, position FROM d.player_matches GROUP BY player_id").fetchall():
                key = pid(us=us)
                self.w.execute("INSERT OR IGNORE INTO players (player_id, name, position) VALUES (?,?,?)", (key, name, pos))
                self.w.execute("UPDATE players SET understat_player_id=?, name=CASE WHEN name LIKE '_. %' THEN ? "
                               "ELSE name END WHERE player_id=?", (us, name, key))   # prefer full names
        # player_match_stats: API-Football rows, then Understat rows merged on (match, team, player)
        if self.has_af:
            for r in self.w.execute("""SELECT fixture_id, team_id, player_id, player, substitute, minutes, position,
                    number, rating, captain, goals, assists, shots, shots_on, key_passes, passes, pass_accuracy, tackles,
                    interceptions, blocks, duels, duels_won, dribbles, dribbles_won, fouls_committed, fouls_drawn,
                    yellow, red, pen_scored, pen_missed, pen_saved, saves, conceded FROM d.af_player_match""").fetchall():
                mid = fixture_to_match.get(r[0])
                if not mid or not r[5]:          # unused substitutes (0 / no minutes) are lineup rows, not stats
                    continue
                if not 0 < r[5] <= 130:          # provider error (one row says -86 minutes): drop the value
                    r = (*r[:5], None, *r[6:])
                self.w.execute(f"INSERT OR REPLACE INTO player_match_stats (match_id, team, player_id, player, started, "
                               "minutes, position, number, rating, captain, goals, assists, shots, shots_on_target, "
                               "key_passes, passes, pass_accuracy, tackles, interceptions, blocks, duels, duels_won, "
                               "dribbles, dribbles_won, fouls_committed, fouls_drawn, yellow, red, pen_scored, "
                               "pen_missed, pen_saved, saves, conceded, sources) VALUES "
                               f"({','.join('?' * 34)})",
                               (mid, af_team.get(r[1]), pid(af=r[2]), r[3], int(not r[4]), *r[5:], "api-football"))
        if self.has_us:
            for r in self.w.execute("""SELECT match_id, team, player_id, player, started, minutes, position, goals,
                    own_goals, assists, shots, key_passes, xg, xa, xg_chain, xg_buildup, yellow, red
                    FROM d.player_matches""").fetchall():
                (mid, team, us, name, started, minutes, pos, goals, og, ast, shots, kp, xg, xa, xgc, xgb, yc, rc) = r
                key = pid(us=us)
                hit = self.w.execute("SELECT 1 FROM player_match_stats WHERE match_id=? AND team=? AND player_id=?",
                                     (mid, team, key)).fetchone()
                if hit:
                    self.w.execute("UPDATE player_match_stats SET xg=?, xa=?, xg_chain=?, xg_buildup=?, own_goals=?, "
                                   "key_passes=COALESCE(key_passes, ?), sources=sources || ',understat' "
                                   "WHERE match_id=? AND team=? AND player_id=?", (xg, xa, xgc, xgb, og, kp, mid, team, key))
                else:
                    self.w.execute("INSERT INTO player_match_stats (match_id, team, player_id, player, started, minutes, "
                                   "position, goals, own_goals, assists, shots, key_passes, xg, xa, xg_chain, xg_buildup, "
                                   "yellow, red, sources) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (mid, team, key, name, started, minutes, pos, goals, og, ast, shots, kp, xg, xa, xgc,
                                    xgb, yc, rc, "understat"))
            for r in self.w.execute("""SELECT match_id, team, shot_id, minute, player_id, player, result, x, y, xg,
                    situation, shot_type, assisted_by, last_action FROM d.shots""").fetchall():
                self.w.execute("INSERT OR REPLACE INTO shots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (*r[:4], pid(us=r[4]), *r[5:]))
        self._log("players", self.w.execute("SELECT COUNT(*) FROM players").fetchone()[0],
                  f"{len(us_to_af)} Understat players linked to API-Football ids")
        self._log("player_match_stats", self.w.execute("SELECT COUNT(*) FROM player_match_stats").fetchone()[0])
        self._log("shots", self.w.execute("SELECT COUNT(*) FROM shots").fetchone()[0])

    # ---------------------------------------------------------------- lineups, events, injuries, squads, transfers
    def af_tables(self, fixture_to_match: dict[int, str], af_team: dict[int, str], af_to_comp: dict[int, str]) -> None:
        if not self.has_af:
            return
        for fid, tid, formation, coach, pid_, name, number, pos, grid, started in self.w.execute(
                "SELECT fixture_id, team_id, formation, coach, player_id, player, number, pos, grid, started "
                "FROM d.af_lineups").fetchall():
            mid = fixture_to_match.get(fid)
            if mid:
                self.w.execute("INSERT OR REPLACE INTO lineups VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (mid, af_team.get(tid), f"af:{pid_}", name, started, pos, number, grid, formation, coach))
        for fid, seq, tid, minute, extra, typ, det, pid_, name, aid, aname in self.w.execute(
                "SELECT fixture_id, seq, team_id, minute, extra, type, detail, player_id, player, assist_id, assist "
                "FROM d.af_events").fetchall():
            mid = fixture_to_match.get(fid)
            if mid:
                self.w.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (mid, seq, af_team.get(tid), minute, extra, typ, det,
                                f"af:{pid_}" if pid_ else None, name, f"af:{aid}" if aid else None, aname))
        for fid, tid, pid_, name, typ, reason in self.w.execute(
                "SELECT fixture_id, team_id, player_id, player, type, reason FROM d.af_injuries").fetchall():
            mid = fixture_to_match.get(fid)
            if mid:
                self.w.execute("INSERT OR REPLACE INTO injuries VALUES (?,?,?,?,?,?)",
                               (mid, af_team.get(tid), f"af:{pid_}", name, typ, reason))
        for r in self.w.execute("""SELECT season, team_id, league_id, player_id, player, position, appearances, lineups,
                minutes, rating, goals, assists, shots, shots_on, key_passes, yellow, red FROM d.af_player_seasons
                WHERE player_id IS NOT NULL""").fetchall():
            self.w.execute("INSERT OR REPLACE INTO squads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (af_season_code(r[0]), af_team.get(r[1], str(r[1])), af_to_comp.get(r[2], f"AF{r[2]}"),
                            f"af:{r[3]}", *r[4:]))
        name_of = {tid: team for tid, team in af_team.items()}
        for pid_, name, d_, typ, tin, tin_name, tout, tout_name in self.w.execute(
                "SELECT player_id, player, date, type, team_in_id, team_in, team_out_id, team_out FROM d.af_transfers").fetchall():
            self.w.execute("INSERT OR REPLACE INTO transfers VALUES (?,?,?,?,?,?)",
                           (f"af:{pid_}", name, d_, typ, name_of.get(tin, tin_name), name_of.get(tout, tout_name)))
        for t in ("lineups", "events", "injuries", "squads", "transfers"):
            self._log(t, self.w.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])

    def shot_xg(self) -> None:
        """Match xG = sum of Understat's shot xG where we have the shots. Understat re-rated old shots but not
        the match totals on its league pages (2021-23: 10% of matches differ by > 0.1), so the shot sum is the
        one definition that is the same in every season. The page value is kept in *_xg_page."""
        if not self.has_us:
            return
        self.w.execute("UPDATE matches SET home_xg_page=home_xg, away_xg_page=away_xg WHERE xg_source='understat'")
        self.w.execute("""UPDATE matches SET
            home_xg=(SELECT SUM(xg) FROM shots s WHERE s.match_id=matches.match_id AND s.team=matches.home AND s.result<>'OwnGoal'),
            away_xg=(SELECT SUM(xg) FROM shots s WHERE s.match_id=matches.match_id AND s.team=matches.away AND s.result<>'OwnGoal'),
            xg_source='understat-shots'
            WHERE match_id IN (SELECT match_id FROM shots)""")
        for col, side in (("home", "h"), ("away", "a")):     # a side without a single shot has 0 xG
            self.w.execute(f"UPDATE matches SET {col}_xg=0 WHERE xg_source='understat-shots' AND {col}_xg IS NULL")
        self.w.execute("""UPDATE team_match_stats SET
            xg=(SELECT CASE WHEN side='h' THEN home_xg ELSE away_xg END FROM matches m WHERE m.match_id=team_match_stats.match_id),
            xg_against=(SELECT CASE WHEN side='h' THEN away_xg ELSE home_xg END FROM matches m WHERE m.match_id=team_match_stats.match_id)
            WHERE match_id IN (SELECT match_id FROM shots)""")

    # ---------------------------------------------------------------- checks
    def checks(self) -> list[str]:
        """Cross-source sanity checks; problems are logged in build_log, not hidden."""
        out = []
        out.append(f"scores corrected to 90 minutes (extra time had been included): {len(self.score_fixed)}")
        out.append(f"score disagreements, our data vs API-Football 90-minute score: {len(self.score_mismatch)}"
                   + (f" e.g. {self.score_mismatch[:3]}" if self.score_mismatch else ""))
        dup = self.w.execute("""SELECT COUNT(*) FROM (SELECT comp, date, home FROM matches WHERE status='FT'
            GROUP BY comp, date, home HAVING COUNT(*) > 1)""").fetchone()[0]
        out.append(f"possible duplicate matches (same comp, date, home): {dup}")
        split = sorted({tuple(sorted(n for n, k in v.items() if k >= 3)) for v in getattr(self, "mapping_votes", {}).values()
                        if sum(1 for k in v.values() if k >= 3) > 1})
        out.append(f"one API club matching two of our names (spelling split - add an alias): {len(split)}"
                   + (f" {split[:5]}" if split else ""))
        lost = self.w.execute("SELECT COUNT(*) FROM teams WHERE tracked=1 AND af_team_id IS NULL").fetchone()[0]
        out.append(f"tracked clubs without an API-Football id: {lost}")
        for i, note in enumerate(out):
            self._log(f"check_{i + 1}", 0, note)
        return out

    def build(self) -> dict:
        af_to_comp = self.competitions()
        af_team = self.teams(af_to_comp)
        fixture_to_match = self.matches(af_to_comp, af_team)
        self.team_stats(fixture_to_match, af_team)
        self.players(fixture_to_match, af_team)
        self.af_tables(fixture_to_match, af_team, af_to_comp)
        self.shot_xg()
        checks = self.checks()
        for step, (rows, note) in self.log.items():
            self.w.execute("INSERT OR REPLACE INTO build_log VALUES (?,?,?)", (step, rows, note))
        self.w.commit()
        self.w.execute("DETACH DATABASE m")
        self.w.execute("DETACH DATABASE d")
        self.w.close()
        self.tmp.replace(self.out)          # atomic: readers never see a half-built warehouse
        return {"tables": {k: v[0] for k, v in self.log.items() if not k.startswith("check")}, "checks": checks}


def build(main_path=None, detail_path=None, out_path=None) -> dict:
    from .detail import DETAIL_DB, connect as detail_connect
    detail_path = Path(detail_path or DETAIL_DB)
    if not detail_path.exists():
        detail_connect(detail_path).close()        # fresh checkout: an empty detail schema
    return Builder(main_path or config.DB_PATH, detail_path, out_path).build()
