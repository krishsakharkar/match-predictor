"""Our own club Elo ratings, computed from the stored results (replaces ClubElo, unreachable since 2026-10-13).

A club's rating is its own rating plus its league's rating:

    R(club) = own[club] + league[L(club)]          L(club) = the domestic league it last played in

* league matches: only the two clubs' own ratings move (the league part cancels);
* European matches (and cups between leagues): the clubs' own ratings move, and so do the two leagues'
  ratings, so one surprise result shifts every club of both leagues a little. Plain Elo needs years of
  European results to separate the leagues; the league term does it within a season.

Update (World Football Elo style):  delta = K * G * (S - E),  E = 1 / (1 + 10^(-(R_home + H - R_away) / 400)),
S = 1 / 0.5 / 0, G = goal-difference multiplier (1, 1.5, (11 + gd) / 8). Clubs promoted into a league start
at a low quantile of that league's own ratings; clubs seen only in Europe start at `europe_start`.

The book answers point-in-time questions without leakage: rating(team, day) uses only matches before `day`.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULTS = {"k": 20.0, "k_europe": 30.0, "k_league": 8.0, "home_adv": 65.0, "start": 1500.0,
            "europe_start": 1400.0, "promoted_quantile": 0.15, "goal_diff": True}


def goal_multiplier(gd: int) -> float:
    gd = abs(gd)
    return 1.0 if gd <= 1 else (1.5 if gd == 2 else (11.0 + gd) / 8.0)


@dataclass
class _Timeline:
    days: list
    values: list

    def at(self, day: str):
        i = bisect.bisect_left(self.days, day) - 1          # strictly before `day`
        return self.values[i] if i >= 0 else None


class TeamElo:
    """Same interface as ingest.clubelo.EloBook: team_rating / rating / club_for / last_match."""

    def __init__(self, matches: pd.DataFrame, league_types: dict[str, str], params: dict | None = None):
        p = {**DEFAULTS, **(params or {})}
        self.params = p
        g = matches[(matches.status == "FT") & matches.fthg.notna() & matches.ftag.notna()]
        g = g.sort_values(["date", "kickoff_utc"] if "kickoff_utc" in g else ["date"], kind="stable")
        own: dict[str, float] = {}
        league_r: dict[str, float] = {}
        team_league: dict[str, str] = {}
        own_tl: dict[str, _Timeline] = {}
        lg_tl: dict[str, _Timeline] = {}
        member_tl: dict[str, _Timeline] = {}          # team -> its domestic league over time
        self._match_days: dict[str, list] = {}

        def rec(tl: dict, key, day, value):
            t = tl.setdefault(key, _Timeline([], []))
            if t.days and t.days[-1] == day:
                t.values[-1] = value
            else:
                t.days.append(day)
                t.values.append(value)

        def start_rating(team, lg):
            if lg is None:
                return p["europe_start"]
            peers = [own[t] for t, l in team_league.items() if l == lg and t in own]
            return float(np.quantile(peers, p["promoted_quantile"])) if len(peers) >= 6 else p["start"]

        for r in g.itertuples(index=False):
            day = r.date.strftime("%Y-%m-%d") if hasattr(r.date, "strftime") else str(r.date)[:10]
            ctype = league_types.get(r.league)
            domestic = ctype in ("league", "extra")
            for t in (r.home, r.away):
                if domestic and team_league.get(t) != r.league:
                    if t in own:                      # moved league (promotion / relegation within our data)
                        own[t] += league_r.get(team_league.get(t), 0.0) - league_r.get(r.league, 0.0)
                    team_league[t] = r.league
                    rec(member_tl, t, day, r.league)
                if t not in own:
                    own[t] = start_rating(t, team_league.get(t))
                self._match_days.setdefault(t, []).append(day)
            lh, la = team_league.get(r.home), team_league.get(r.away)
            rh = own[r.home] + league_r.get(lh, 0.0)
            ra = own[r.away] + league_r.get(la, 0.0)
            e = 1.0 / (1.0 + 10 ** (-(rh + p["home_adv"] - ra) / 400.0))
            gd = int(r.fthg) - int(r.ftag)
            s = 1.0 if gd > 0 else (0.5 if gd == 0 else 0.0)
            mult = goal_multiplier(gd) if p["goal_diff"] else 1.0
            k = p["k"] if domestic else p["k_europe"]
            d = k * mult * (s - e)
            own[r.home] += d
            own[r.away] -= d
            if not domestic and lh != la:
                dl = p["k_league"] * mult * (s - e)
                if lh is not None:
                    league_r[lh] = league_r.get(lh, 0.0) + dl
                    rec(lg_tl, lh, day, league_r[lh])
                if la is not None:
                    league_r[la] = league_r.get(la, 0.0) - dl
                    rec(lg_tl, la, day, league_r[la])
            rec(own_tl, r.home, day, own[r.home])
            rec(own_tl, r.away, day, own[r.away])
        # timelines hold the value after each match day; lookups for `day` read strictly earlier days
        self._own, self._league, self._member = own_tl, lg_tl, member_tl
        self.final = {t: own[t] + league_r.get(team_league.get(t), 0.0) for t in own}
        self.league_ratings = dict(league_r)

    def __bool__(self):
        return bool(self._own)

    def club_for(self, team: str, country: str | None = None) -> str | None:
        return team if team in self._own else None

    def rating(self, club: str | None, day: str) -> float | None:
        if not club or club not in self._own:
            return None
        o = self._own[club].at(day)
        if o is None:
            return None
        lg = self._member[club].at(day) if club in self._member else None
        off = self._league[lg].at(day) if lg in self._league else None
        return float(o + (off or 0.0))

    def team_rating(self, team: str, day: str, country: str | None = None) -> float | None:
        return self.rating(team, day)

    def last_match(self, club: str | None, day: str) -> str | None:
        days = self._match_days.get(club or "")
        if not days:
            return None
        i = bisect.bisect_left(days, day) - 1
        return days[i] if i >= 0 else None


def build(matches: pd.DataFrame, params: dict | None = None) -> TeamElo:
    from .. import config
    types = {c: v.get("type") for c, v in config.competitions(enabled_only=False).items()}
    if "comp_type" in matches:          # the warehouse frame also types API-Football competitions
        types = {**dict(zip(matches.league, matches.comp_type.fillna("cup"))), **types}
    return TeamElo(matches, types, params or config.settings()["elo"].get("own"))
