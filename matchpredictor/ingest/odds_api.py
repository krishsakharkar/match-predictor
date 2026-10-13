"""The Odds API client: fixtures (free /events), FanDuel odds, and scores.

Free tier = 500 credits/month. Cost per call: /events 0, /odds = markets x regions,
/scores = 1 (2 with daysFrom). Every response is cached in SQLite and calls are
skipped once remaining credits fall below `reserve_credits`.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import requests

from .. import config
from .http_cache import cache_get, cache_put, last_remaining, record_usage

log = logging.getLogger(__name__)
BASE = "https://api.the-odds-api.com/v4"
PROVIDER = "odds_api"


class OddsApiUnavailable(RuntimeError):
    pass


def _key() -> str | None:
    return os.environ.get("ODDS_API_KEY")


def _call(conn, path: str, params: dict, cost: int, ttl: timedelta, force=False):
    cfg = config.settings()["odds_api"]
    cache_key = f"{PROVIDER}:{path}:{sorted(params.items())}"
    cached = cache_get(conn, cache_key, ttl)
    if cached is not None and not force:
        return cached
    key = _key()
    if not key:
        raise OddsApiUnavailable("ODDS_API_KEY not set")
    remaining = last_remaining(conn, PROVIDER)
    if cost > 0 and remaining is not None and remaining - cost < cfg["reserve_credits"]:
        raise OddsApiUnavailable(f"credit reserve reached ({remaining} left)")
    try:
        r = requests.get(f"{BASE}{path}", params={**params, "apiKey": key}, timeout=30)
    except requests.RequestException as e:
        raise OddsApiUnavailable(f"network error: {type(e).__name__}") from None
    rem = r.headers.get("x-requests-remaining")
    last = r.headers.get("x-requests-last")
    record_usage(conn, PROVIDER, int(float(last)) if last else 0, int(float(rem)) if rem else None)
    if r.status_code != 200:
        raise OddsApiUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    cache_put(conn, cache_key, body)
    return body


def events(conn, sport: str) -> list[dict]:
    """Upcoming fixtures. Free (does not consume credits)."""
    return _call(conn, f"/sports/{sport}/events", {}, cost=0, ttl=timedelta(hours=6))


def odds(conn, sport: str, force=False) -> list[dict]:
    cfg = config.settings()["odds_api"]
    markets = cfg["markets"]
    params = {"regions": cfg["region"], "bookmakers": cfg["bookmakers"], "markets": markets,
              "oddsFormat": "decimal"}
    return _call(conn, f"/sports/{sport}/odds", params, cost=len(markets.split(",")),
                 ttl=timedelta(minutes=cfg["cache_ttl_minutes"]), force=force)


def scores(conn, sport: str, days_from: int = 3, ttl_minutes: float | None = None) -> list[dict]:
    cfg = config.settings()["odds_api"]
    return _call(conn, f"/sports/{sport}/scores", {"daysFrom": days_from}, cost=2,
                 ttl=timedelta(minutes=cfg["scores_cache_ttl_minutes"] if ttl_minutes is None else ttl_minutes))


def parse_odds(event: dict, home: str, away: str) -> list[dict]:
    """Flatten one event's bookmaker markets into snapshot rows (selection in home/draw/away/over/under)."""
    rows = []
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            for o in mk.get("outcomes", []):
                name = o["name"]
                if mk["key"] == "h2h":
                    sel = {"Draw": "draw"}.get(name)
                    if sel is None:
                        sel = "home" if name == event["home_team"] else "away" if name == event["away_team"] else None
                    line = None
                elif mk["key"] in ("totals", "alternate_totals"):
                    sel = name.lower()
                    line = o.get("point")
                elif mk["key"] == "btts":
                    sel = name.lower()
                    line = None
                else:
                    continue
                if sel is None:
                    continue
                rows.append({"bookmaker": bm["key"], "market": "totals" if "totals" in mk["key"] else mk["key"],
                             "selection": sel, "line": line, "price": float(o["price"])})
    return rows
