"""ClubElo (api.clubelo.com): cross-league club strength ratings. Free, no key.

  /YYYY-MM-DD   all clubs' ratings on that day      (1 request per run)
  /ClubName     one club's full rating history       (backfill, used to calibrate the Elo model)

CSV columns: Rank,Club,Country,Level,Elo,From,To. Each row is an interval during which the rating
held; From is the day after a match, so the interval containing a match date is the *pre-match*
rating. The service is a free hobby project: requests are paused and history is refreshed rarely.
"""
from __future__ import annotations

import bisect
import csv
import io
import logging
import time
from datetime import date, timedelta

import requests

from .. import config

log = logging.getLogger(__name__)
HOSTS = ("https://api.clubelo.com", "http://api.clubelo.com")


class ClubEloUnavailable(RuntimeError):
    pass


def _get(path: str) -> str:
    last = None
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/{path}", timeout=30)
            if r.status_code == 200 and r.text.startswith("Rank"):
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
    raise ClubEloUnavailable(f"api.clubelo.com unreachable ({last})")


def parse_csv(text: str) -> list[dict]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            out.append({"club": r["Club"], "country": r["Country"], "level": int(r["Level"]) if r["Level"] else None,
                        "elo": float(r["Elo"]), "date_from": r["From"], "date_to": r["To"]})
        except (KeyError, ValueError):
            continue
    return out


def store(conn, rows: list[dict], since: str | None = None) -> int:
    n = 0
    for r in rows:
        if since and r["date_to"] < since:
            continue
        conn.execute("INSERT INTO elo_ratings (club, country, level, elo, date_from, date_to) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(club, date_from) DO UPDATE SET elo=excluded.elo, date_to=excluded.date_to, "
                     "country=COALESCE(excluded.country, country), level=COALESCE(excluded.level, level)",
                     (r["club"], r["country"], r["level"], r["elo"], r["date_from"], r["date_to"]))
        n += 1
    return n


def fetch_snapshot(conn, day: str) -> int:
    return store(conn, parse_csv(_get(day)))


CACHE_DIR = config.DATA_DIR / "cache" / "clubelo"


def fetch_history(conn, club: str, since: str, max_age_days: float = 7) -> int:
    """One club's rating history (cached on disk for `max_age_days`)."""
    path = CACHE_DIR / (club.replace(" ", "").replace("/", "_") + ".csv")
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400:
        text = path.read_text()
    else:
        text = _get(club.replace(" ", ""))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        pause = config.settings()["elo"]["request_pause_seconds"]
        if pause:
            time.sleep(pause)
    return store(conn, parse_csv(text), since)


def known_clubs(conn) -> dict[str, str]:
    """ClubElo club -> country, from whatever ratings are stored."""
    return {r["club"]: r["country"] for r in conn.execute(
        "SELECT club, country FROM elo_ratings GROUP BY club")}


def resolve(team: str, clubs: dict[str, str], country: str | None = None) -> str | None:
    """Map one of our team names onto a ClubElo club name."""
    alias = config.elo_aliases().get(team)
    if alias and alias in clubs:
        return alias
    if team in clubs:
        return team
    pool = [c for c, ctry in clubs.items() if ctry == country] if country else list(clubs)
    return config.match_name(team, pool, cutoff=0.8) or (config.match_name(team, list(clubs), cutoff=0.9)
                                                          if country else None)


class EloBook:
    """Fast point-in-time rating lookup: rating(club, 'YYYY-MM-DD') -> pre-match Elo (or None)."""

    def __init__(self, conn):
        self._data: dict[str, tuple[list[str], list[float]]] = {}
        for r in conn.execute("SELECT club, date_from, elo FROM elo_ratings ORDER BY club, date_from"):
            froms, elos = self._data.setdefault(r["club"], ([], []))
            froms.append(r["date_from"])
            elos.append(r["elo"])
        self.clubs = known_clubs(conn)
        self._resolved: dict[tuple, str | None] = {}

    def __bool__(self):
        return bool(self._data)

    def club_for(self, team: str, country: str | None = None) -> str | None:
        key = (team, country)
        if key not in self._resolved:
            self._resolved[key] = resolve(team, self.clubs, country)
        return self._resolved[key]

    def rating(self, club: str | None, day: str) -> float | None:
        if not club or club not in self._data:
            return None
        froms, elos = self._data[club]
        i = bisect.bisect_right(froms, day) - 1
        return elos[i] if i >= 0 else None

    def team_rating(self, team: str, day: str, country: str | None = None) -> float | None:
        return self.rating(self.club_for(team, country), day)

    def last_match(self, club: str | None, day: str) -> str | None:
        """Date of the club's last match before `day` (any competition): a rating interval starts the
        day after each match, so the interval containing `day` began the day after the previous game."""
        if not club or club not in self._data:
            return None
        froms, _ = self._data[club]
        i = bisect.bisect_right(froms, day) - 1
        if i < 1:                      # the very first stored interval is not a match boundary
            return None
        return (date.fromisoformat(froms[i]) - timedelta(days=1)).isoformat()
