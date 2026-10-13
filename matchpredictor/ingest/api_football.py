"""API-Football (v3) client: fixtures metadata, referees, lineups, injuries, player stats.

The free plan allows 100 requests/day and (as of 2026) only seasons 2022-2024,
so for the current season every call may come back with a plan error. The
client detects that once per run and turns itself off, and the pipeline
degrades to "no lineups / no injuries / no player props" with a note.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import requests

from .. import config
from .http_cache import cache_get, cache_put, record_usage, usage_today

log = logging.getLogger(__name__)
BASE = "https://v3.football.api-sports.io"
PROVIDER = "api_football"


class ApiFootballUnavailable(RuntimeError):
    pass


class Client:
    def __init__(self, conn):
        self.conn = conn
        self.disabled_reason: str | None = None
        if not os.environ.get("API_FOOTBALL_KEY"):
            self.disabled_reason = "API_FOOTBALL_KEY not set"

    def get(self, path: str, params: dict, ttl_hours: float | None = None):
        if self.disabled_reason:
            raise ApiFootballUnavailable(self.disabled_reason)
        cfg = config.settings()["api_football"]
        ttl = timedelta(hours=ttl_hours if ttl_hours is not None else cfg["cache_ttl_hours"])
        key = f"{PROVIDER}:{path}:{sorted(params.items())}"
        cached = cache_get(self.conn, key, ttl)
        if cached is not None:
            return cached
        if usage_today(self.conn, PROVIDER) >= cfg["daily_limit"] - cfg["reserve_requests"]:
            self.disabled_reason = "daily request budget reached"
            raise ApiFootballUnavailable(self.disabled_reason)
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=30,
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
            if isinstance(errors, dict) and ("plan" in errors or "token" in errors or "requests" in errors):
                self.disabled_reason = msg       # plan/quota errors will repeat: stop for this run
            raise ApiFootballUnavailable(msg)
        cache_put(self.conn, key, body)
        return body["response"]

    # ---- endpoints ----
    def fixtures(self, league_id: int, season: int, date_from: str, date_to: str):
        return self.get("/fixtures", {"league": league_id, "season": season,
                                      "from": date_from, "to": date_to}, ttl_hours=12)

    def injuries(self, fixture_id: int):
        return self.get("/injuries", {"fixture": fixture_id}, ttl_hours=6)

    def lineups(self, fixture_id: int):
        return self.get("/fixtures/lineups", {"fixture": fixture_id}, ttl_hours=0.5)

    def topscorers(self, league_id: int, season: int):
        return self.get("/players/topscorers", {"league": league_id, "season": season}, ttl_hours=72)


def parse_topscorers(resp: list[dict]) -> list[dict]:
    out = []
    for item in resp:
        p = item["player"]
        st = item["statistics"][0]
        out.append({
            "player_id": p["id"], "player": p["name"], "team": st["team"]["name"],
            "apps": st["games"].get("appearences") or 0, "minutes": st["games"].get("minutes") or 0,
            "goals": st["goals"].get("total") or 0, "shots": st["shots"].get("total") or 0,
            "sot": st["shots"].get("on") or 0,
        })
    return out


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


def parse_injuries(resp: list[dict]) -> list[dict]:
    return [{"team": x["team"]["name"], "player_id": x["player"]["id"], "player": x["player"]["name"],
             "reason": x["player"].get("reason") or x["player"].get("type")} for x in resp]
