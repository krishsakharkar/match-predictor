"""Understat (understat.com): match xG and player xG/xA for the top-5 leagues. Free, no key.

Two page formats are supported:
  * JSON:  GET /getLeagueData/{league}/{year}  (X-Requested-With: XMLHttpRequest) -> {"dates": [...], "players": [...]}
  * HTML:  GET /league/{league}/{year} with `datesData = JSON.parse('...')` / `playersData = ...` blobs
`year` is the season's start year (2025 = 2025-26). Raw responses are cached under data/cache/understat;
finished seasons are never re-downloaded.
"""
from __future__ import annotations

import codecs
import html
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

from .. import config, db

log = logging.getLogger(__name__)
BASE = "https://understat.com"
LEAGUE_KEYS = {"EPL": "EPL", "LALIGA": "La_liga", "BUNDESLIGA": "Bundesliga", "SERIEA": "Serie_A", "LIGUE1": "Ligue_1"}
CACHE_DIR = config.DATA_DIR / "cache" / "understat"
HEADERS = {"User-Agent": "Mozilla/5.0 (match-predictor; +https://github.com/krishsakharkar/match-predictor)"}


class UnderstatUnavailable(RuntimeError):
    pass


def _blob(html: str, var: str):
    m = re.search(var + r"\s*=\s*JSON\.parse\('(.*?)'\)", html, re.S)
    if not m:
        return None
    return json.loads(codecs.decode(m.group(1), "unicode_escape"))


def parse_payload(text: str) -> dict:
    """Return {"dates": [...], "players": [...]} from either response format."""
    text = text.strip()
    if text.startswith("{"):
        d = json.loads(text)
        return {"dates": d.get("dates") or [], "players": d.get("players") or []}
    dates = _blob(text, "datesData")
    if dates is None:
        raise UnderstatUnavailable("unrecognised Understat page format")
    return {"dates": dates, "players": _blob(text, "playersData") or []}


def fetch_league(league_key: str, year: int, max_age_hours: float | None = 12) -> dict:
    """Download (or read from cache) one league-season. max_age_hours=None = cached copy never expires."""
    lk = LEAGUE_KEYS[league_key]
    path = CACHE_DIR / f"{lk}_{year}.json"
    if path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if max_age_hours is None or age_h < max_age_hours:
            return json.loads(path.read_text())
    last = None
    for url, hdr in ((f"{BASE}/getLeagueData/{lk}/{year}", {"X-Requested-With": "XMLHttpRequest"}),
                     (f"{BASE}/league/{lk}/{year}", {})):
        try:
            r = requests.get(url, headers={**HEADERS, **hdr}, timeout=30)
        except requests.RequestException as e:
            last = type(e).__name__
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        try:
            payload = parse_payload(r.text)
        except (UnderstatUnavailable, ValueError) as e:
            last = str(e)
            continue
        if payload["dates"]:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
            return payload
        last = "empty response"
    raise UnderstatUnavailable(f"understat.com {lk} {year}: {last}")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def store(conn, league_key: str, season: str, payload: dict, known: set[str]) -> dict:
    """Write match xG onto matches rows and player season stats. Returns counts + unmatched team names."""
    unmatched = set()
    n_matches = 0
    for m in payload["dates"]:
        if not m.get("isResult"):
            continue
        home = config.canonical_team(m["h"]["title"], known)
        away = config.canonical_team(m["a"]["title"], known)
        bad = [raw for t, raw in ((home, m["h"]["title"]), (away, m["a"]["title"])) if known and t not in known]
        if bad:
            unmatched.update(bad)      # never create rows under a name the results data doesn't use
            continue
        dt = m["datetime"][:10]
        db.upsert_match(conn, {
            "league": league_key, "season": season, "date": dt, "home": home, "away": away, "status": "FT",
            "fthg": int(m["goals"]["h"]), "ftag": int(m["goals"]["a"]),
            "xg_home": _f(m["xG"]["h"]), "xg_away": _f(m["xG"]["a"]), "understat_id": str(m["id"]),
            "source": "understat"})
        n_matches += 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p in payload["players"]:
        if "," in p["team_title"]:
            # multi-club titles are alphabetical, not chronological (and occasionally corrupt), so the
            # current club can't be known: skip rather than credit the wrong team
            continue
        team = config.canonical_team(p["team_title"].strip(), known)
        conn.execute(
            "INSERT INTO understat_players (league, season, player_id, player, team, position, games, minutes, goals, "
            "xg, npxg, assists, xa, shots, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(league, season, player_id) DO UPDATE SET player=excluded.player, team=excluded.team, "
            "position=excluded.position, games=excluded.games, minutes=excluded.minutes, goals=excluded.goals, "
            "xg=excluded.xg, npxg=excluded.npxg, assists=excluded.assists, xa=excluded.xa, shots=excluded.shots, "
            "updated_at=excluded.updated_at",
            (league_key, season, int(p["id"]), p["player_name"], team, p.get("position"), int(p.get("games") or 0),
             int(p.get("time") or 0), int(p.get("goals") or 0), _f(p.get("xG")), _f(p.get("npxG")),
             int(p.get("assists") or 0), _f(p.get("xA")), int(p.get("shots") or 0), now))
    return {"matches": n_matches, "players": len(payload["players"]), "unmatched": sorted(unmatched)}


def update(conn, current_season: str, notes: list[str]) -> dict:
    """Refresh xG for every league with an Understat key, history seasons + current."""
    seasons = config.previous_seasons(current_season, config.settings()["history_seasons"]) + [current_season]
    out = {}
    for key in config.leagues():
        if key not in LEAGUE_KEYS:
            continue
        known = {r[0] for r in conn.execute(
            "SELECT DISTINCT home FROM matches WHERE league=? AND source IN ('football-data','mirror')", (key,))}
        for s in seasons:
            year = 2000 + int(s[:2])
            try:
                payload = fetch_league(key, year, max_age_hours=12 if s == current_season else None)
            except UnderstatUnavailable as e:
                notes.append(f"Understat xG unavailable for {key} {s}: {e}")
                break
            res = store(conn, key, s, payload, known)
            if res["unmatched"]:
                notes.append(f"Understat {key} {s}: team names not matched {res['unmatched']} "
                             "(add to config/team_aliases.json)")
            out[f"{key}:{s}"] = res["matches"]
    return out


# ---------------------------------------------------------------- per-match detail (players, shots)

MATCH_DIR = CACHE_DIR / "matches"


def parse_match(text: str) -> dict:
    """{"rosters": {"h": {...}, "a": {...}}, "shots": {"h": [...], "a": [...]}} from /getMatchData/{id}
    (or the HTML match page's rostersData / shotsData blobs). The HTML roster template is dropped."""
    text = text.strip()
    if text.startswith("{"):
        d = json.loads(text)
        rosters, shots = d.get("rosters"), d.get("shots")
    else:
        rosters, shots = _blob(text, "rostersData"), _blob(text, "shotsData")
    if not rosters or not isinstance(rosters, dict) or not {"h", "a"} <= set(rosters):
        raise UnderstatUnavailable("unrecognised Understat match format")
    return {"rosters": rosters, "shots": shots or {"h": [], "a": []}}


def fetch_match(understat_id: str | int, pause: float = 1.0) -> tuple[dict, bool]:
    """One finished match's rosters and shots, from cache when present (finished matches never change).
    Returns (payload, downloaded); sleeps `pause` seconds after a download to stay polite."""
    path = MATCH_DIR / f"{understat_id}.json"
    if path.exists():
        return json.loads(path.read_text()), False
    last = None
    for url, hdr in ((f"{BASE}/getMatchData/{understat_id}", {"X-Requested-With": "XMLHttpRequest"}),
                     (f"{BASE}/match/{understat_id}", {})):
        try:
            r = requests.get(url, headers={**HEADERS, **hdr}, timeout=30)
        except requests.RequestException as e:
            last = type(e).__name__
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        try:
            payload = parse_match(r.text)
        except (UnderstatUnavailable, ValueError) as e:
            last = str(e)
            continue
        MATCH_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")))
        time.sleep(pause)
        return payload, True
    time.sleep(pause)
    raise UnderstatUnavailable(f"understat.com match {understat_id}: {last}")


def match_rows(payload: dict, match_id: str, home: str, away: str) -> tuple[list[dict], list[dict]]:
    """Flatten one match into player rows and shot rows, keyed by our match id and team names
    (the side h/a decides the team, so no name matching is needed)."""
    team = {"h": home, "a": away}
    players, shots = [], []
    for side in ("h", "a"):
        for p in payload["rosters"][side].values():
            players.append({
                "match_id": match_id, "team": team[side], "side": side, "player_id": int(p["player_id"]),
                "player": html.unescape(p["player"]), "position": p.get("position"), "started": int(p.get("position") != "Sub"),
                "minutes": int(p.get("time") or 0), "goals": int(p.get("goals") or 0),
                "own_goals": int(p.get("own_goals") or 0), "assists": int(p.get("assists") or 0),
                "shots": int(p.get("shots") or 0), "key_passes": int(p.get("key_passes") or 0),
                "xg": _f(p.get("xG")), "xa": _f(p.get("xA")), "xg_chain": _f(p.get("xGChain")),
                "xg_buildup": _f(p.get("xGBuildup")), "yellow": int(p.get("yellow_card") or 0),
                "red": int(p.get("red_card") or 0)})
        for s in payload["shots"].get(side) or []:
            shots.append({
                "match_id": match_id, "team": team[side], "shot_id": int(s["id"]), "minute": int(s.get("minute") or 0),
                "player_id": int(s["player_id"]), "player": html.unescape(s.get("player") or ""), "result": s.get("result"),
                "x": _f(s.get("X")), "y": _f(s.get("Y")), "xg": _f(s.get("xG")), "situation": s.get("situation"),
                "shot_type": s.get("shotType"),
                "assisted_by": html.unescape(s["player_assisted"]) if s.get("player_assisted") else None,
                "last_action": s.get("lastAction")})
    return players, shots
