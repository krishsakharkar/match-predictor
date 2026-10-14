"""API-Football (v3): fixtures, referees, injuries, lineups, penalties, historical Champions League.

Plan limits come from settings.api_football (PRO since 2026-10-14: 7,500 requests/day, every season and
endpoint). On a plan / quota / key error the client turns itself off for the run and the pipeline
degrades to "no absences / no referee" with a note. Bulk history lives in api_football_history.py. Historical calls (backtest) use seasons the plan allows
and are cached on disk forever (data/cache/api_football), so repeated CI runs don't spend quota.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import timedelta

import requests

from .. import config
from .http_cache import cache_get, cache_put, record_usage, usage_today

log = logging.getLogger(__name__)
BASE = "https://v3.football.api-sports.io"
PROVIDER = "api_football"
FILE_CACHE = config.DATA_DIR / "cache" / "api_football"


class ApiFootballUnavailable(RuntimeError):
    pass


class Client:
    _last_call = 0.0

    def __init__(self, conn):
        self.conn = conn
        self.disabled_reason: str | None = None
        if not os.environ.get("API_FOOTBALL_KEY"):
            self.disabled_reason = "API_FOOTBALL_KEY not set"

    def get(self, path: str, params: dict, ttl_hours: float | None = None, forever: bool = False):
        try:
            return self._get(path, params, ttl_hours, forever)
        except ApiFootballUnavailable as e:
            if "ratelimit" not in str(e).lower():
                raise
            time.sleep(61)            # per-minute limit: wait one window and retry once
            return self._get(path, params, ttl_hours, forever)

    def _get(self, path: str, params: dict, ttl_hours: float | None, forever: bool):
        """GET with caching. forever=True: on-disk cache that never expires (immutable historical data)."""
        key = f"{PROVIDER}:{path}:{sorted(params.items())}"
        fpath = FILE_CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json")
        if forever and fpath.exists():
            return json.loads(fpath.read_text())
        cfg = config.settings()["api_football"]
        if not forever:
            ttl = timedelta(hours=ttl_hours if ttl_hours is not None else cfg["cache_ttl_hours"])
            cached = cache_get(self.conn, key, ttl)
            if cached is not None:
                return cached["response"] if isinstance(cached, dict) and "response" in cached else cached
        body = self.fetch(path, params)
        resp = body["response"]
        if forever:
            FILE_CACHE.mkdir(parents=True, exist_ok=True)
            fpath.write_text(json.dumps(resp))
        else:
            cache_put(self.conn, key, body)
        return resp

    def fetch(self, path: str, params: dict) -> dict:
        """One uncached request: pacing, quota accounting, plan errors. Returns the whole body
        (response + paging), for bulk downloads that store parsed rows instead of raw responses."""
        cfg = config.settings()["api_football"]
        if self.disabled_reason:
            raise ApiFootballUnavailable(self.disabled_reason)
        if usage_today(self.conn, PROVIDER) >= cfg["daily_limit"] - cfg["reserve_requests"]:
            self.disabled_reason = "daily request budget reached"
            raise ApiFootballUnavailable(self.disabled_reason)
        wait = cfg.get("min_interval_seconds", 0) - (time.monotonic() - Client._last_call)
        if wait > 0:
            time.sleep(wait)          # per-minute rate limit
        Client._last_call = time.monotonic()
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=60,
                             headers={"x-apisports-key": os.environ["API_FOOTBALL_KEY"]})
        except requests.RequestException as e:
            self.disabled_reason = f"network error: {type(e).__name__}"
            raise ApiFootballUnavailable(self.disabled_reason) from None
        remaining = r.headers.get("x-ratelimit-requests-remaining")
        record_usage(self.conn, PROVIDER, 1, int(remaining) if remaining and remaining.isdigit() else None)
        body = r.json() if r.status_code == 200 else {"errors": {"http": r.status_code}}
        errors = body.get("errors")
        if errors:
            msg = "; ".join(f"{k}: {v}" for k, v in errors.items()) if isinstance(errors, dict) else str(errors)
            if isinstance(errors, dict) and any(k in errors for k in ("plan", "token", "requests", "access")):
                self.disabled_reason = msg       # plan/quota errors will repeat: stop for this run
            raise ApiFootballUnavailable(msg)
        return body

    # ---- endpoints ----
    def fixtures(self, league_id: int, season: int, date_from: str, date_to: str):
        return self.get("/fixtures", {"league": league_id, "season": season, "from": date_from, "to": date_to},
                        ttl_hours=12)

    def season_fixtures(self, league_id: int, season: int):
        return self.get("/fixtures", {"league": league_id, "season": season}, forever=True)

    def season_injuries(self, league_id: int, season: int):
        return self.get("/injuries", {"league": league_id, "season": season}, forever=True)

    def injuries(self, fixture_id: int):
        return self.get("/injuries", {"fixture": fixture_id}, ttl_hours=6)

    def lineups(self, fixture_id: int):
        return self.get("/fixtures/lineups", {"fixture": fixture_id}, ttl_hours=0.5)

    def events(self, fixture_id: int):
        return self.get("/fixtures/events", {"fixture": fixture_id}, forever=True)


def normalise_referee(name: str | None) -> str | None:
    """'M. Oliver, England' -> 'M Oliver' (football-data style)."""
    if not name:
        return None
    name = name.split(",")[0]
    out = " ".join(name.replace(".", " ").split())
    return out or None


def parse_fixture(f: dict) -> dict:
    fx, teams, goals = f["fixture"], f["teams"], f.get("goals") or {}
    status = (fx.get("status") or {}).get("short")
    return {
        "id": fx["id"], "date": fx["date"][:10], "kickoff_utc": fx["date"],
        "home": teams["home"]["name"], "away": teams["away"]["name"],
        "referee": normalise_referee(fx.get("referee")),
        "round": (f.get("league") or {}).get("round", ""),
        "finished": status in ("FT", "AET", "PEN"),
        "fthg": goals.get("home"), "ftag": goals.get("away"),
    }


def is_main_round(round_name: str) -> bool:
    """Exclude qualifying rounds from European competitions."""
    r = round_name.lower()
    return not ("qualifying" in r or "preliminary" in r or r.strip() == "play-offs")


def parse_lineup(resp: list[dict]) -> dict[str, dict]:
    out = {}
    for team in resp:
        out[team["team"]["name"]] = {
            "formation": team.get("formation"),
            "starters": [{"name": x["player"]["name"], "number": x["player"].get("number"),
                          "pos": x["player"].get("pos")} for x in team.get("startXI", [])],
            "subs": [x["player"]["name"] for x in team.get("substitutes", [])],
        }
    return out


def parse_injuries(resp: list[dict], missing_only: bool = True) -> list[dict]:
    """Players listed as missing. 'Questionable' entries are dropped by default."""
    out = []
    for x in resp:
        kind = (x["player"].get("type") or "").lower()
        if missing_only and "missing" not in kind:
            continue
        out.append({"team": x["team"]["name"], "player_id": x["player"]["id"], "player": x["player"]["name"],
                    "reason": x["player"].get("reason") or x["player"].get("type"),
                    "fixture_id": (x.get("fixture") or {}).get("id")})
    return out


def count_penalties(events: list[dict], home_team: str, away_team: str) -> tuple[int, int]:
    """Penalties awarded (scored + missed) per side from a fixture's events (shootouts excluded)."""
    h = a = 0
    for e in events:
        detail = (e.get("detail") or "").lower()
        if detail not in ("penalty", "missed penalty"):
            continue
        if (e.get("comments") or "").lower().startswith("penalty shootout"):
            continue
        team = (e.get("team") or {}).get("name")
        if team == home_team:
            h += 1
        elif team == away_team:
            a += 1
    return h, a
