"""Fit per-league models as of a date and turn them into match predictions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from . import config
from .models import counts, dixon_coles as dc, injuries, markets, props

COUNT_STATS = ("shots", "sot", "corners", "cards")


def load_matches(conn, league: str | None = None) -> pd.DataFrame:
    q = "SELECT * FROM matches" + (" WHERE league = ?" if league else "")
    df = pd.read_sql_query(q, conn, params=(league,) if league else ())
    df["date"] = pd.to_datetime(df["date"])
    df["cards_h"] = df["hy"] + df["hr"]
    df["cards_a"] = df["ay"] + df["ar"]
    return df


@dataclass
class LeagueModel:
    league: str
    as_of: date
    goals: dc.DixonColes
    stats: dict[str, counts.CountModel]
    promoted: set[str]
    prior: dict
    league_avgs: dict

    # ---- ratings with a fallback for teams the fit has never seen (newly promoted, no data yet)
    def _dc_rating(self, team):
        i = self.goals.index.get(team)
        if i is None:
            return self.prior["attack"], self.prior["defence"]
        return float(self.goals.attack[i]), float(self.goals.defence[i])

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        ah, dh = self._dc_rating(home)
        aa, da = self._dc_rating(away)
        g = self.goals
        lam = float(np.exp(g.intercept + g.home_adv + ah + da))
        mu = float(np.exp(g.intercept + aa + dh))
        return lam, mu

    def expected_stat(self, stat: str, home: str, away: str, referee: str | None = None):
        m = self.stats.get(stat)
        if m is None:
            return None
        idx = m.index

        def r(t):
            i = idx.get(t)
            return (0.0, 0.0) if i is None else (float(m.gen[i]), float(m.conc[i]))

        gh, ch = r(home)
        ga, ca = r(away)
        eh = float(np.exp(m.intercept + m.home_adv + gh + ca))
        ea = float(np.exp(m.intercept + ga + ch))
        f = m.referee_factor.get(referee, 1.0) if referee else 1.0
        return eh * f, ea * f


def fit_league(df: pd.DataFrame, league: str, as_of: date, xi: float | None = None,
               fixture_teams: set[str] | None = None) -> LeagueModel | None:
    s = config.settings()
    mcfg = s["model"]
    if xi is None:
        xi = config.leagues().get(league, {}).get("time_decay_xi", mcfg["time_decay_xi"])
    as_of_ts = pd.Timestamp(as_of)
    cur = config.season_code(as_of)
    keep = set(config.previous_seasons(cur, s["history_seasons"]) + [cur])
    hist = df[(df.league == league) & (df.status == "FT") & (df.date < as_of_ts)
              & df.season.isin(keep)].dropna(subset=["fthg", "ftag"])
    if len(hist) < 100:
        return None
    days = (as_of_ts - hist.date).dt.days.to_numpy()
    w = dc.time_weights(days, xi)

    prev = config.previous_seasons(cur, 1)[0]
    prev_teams = set(hist[hist.season == prev].home)
    cur_teams = set(df[(df.league == league) & (df.season == cur)].home) | (fixture_teams or set())
    promoted = (cur_teams - prev_teams) if prev_teams else set()
    prior = mcfg["promoted_prior"]
    goals = dc.fit(hist.home.tolist(), hist.away.tolist(), hist.fthg.to_numpy(), hist.ftag.to_numpy(),
                   weights=w, ridge=mcfg["ridge"],
                   prior_attack={t: prior["attack"] for t in promoted},
                   prior_defence={t: prior["defence"] for t in promoted}, xi=xi)

    stats = {}
    avgs = {"goals_home": float(np.average(hist.fthg, weights=w)),
            "goals_away": float(np.average(hist.ftag, weights=w))}
    for stat, (hc, ac) in counts.STATS.items():
        sub = hist.dropna(subset=[hc, ac])
        if len(sub) < 100:
            continue
        sw = w[hist.index.get_indexer(sub.index)]
        refs = sub.referee.tolist() if stat == "cards" and sub.referee.notna().mean() > 0.5 else None
        stats[stat] = counts.fit_count_model(stat, sub.home.tolist(), sub.away.tolist(),
                                             sub[hc].to_numpy(), sub[ac].to_numpy(), weights=sw,
                                             ridge=mcfg["count_ridge"], referees=refs)
        avgs[stat] = float(np.average(sub[hc] + sub[ac], weights=sw))
    return LeagueModel(league=league, as_of=as_of, goals=goals, stats=stats, promoted=promoted,
                       prior=prior, league_avgs=avgs)


def _round_tree(x, n=4):
    if isinstance(x, dict):
        return {k: _round_tree(v, n) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_tree(v, n) for v in x]
    if isinstance(x, float):
        return round(x, n)
    return x


def predict_match(model: LeagueModel, home: str, away: str, referee: str | None = None,
                  absences: dict | None = None, player_data: dict | None = None,
                  lineup_status: dict | None = None) -> dict:
    """Full prediction for one fixture.

    absences: {"home": [share, ...], "away": [...]} attacking shares of missing players
    player_data: {"home": {"players": [...], "team_goals":..., ...}, "away": {...}}
    """
    s = config.settings()
    lines = s["lines"]
    lam, mu = model.expected_goals(home, away)
    absences = absences or {}
    inj = s["injuries"]
    mult_h = injuries.absence_multiplier(absences.get("home", []), inj["replacement_retention"], inj["min_multiplier"])
    mult_a = injuries.absence_multiplier(absences.get("away", []), inj["replacement_retention"], inj["min_multiplier"])
    lam, mu = lam * mult_h, mu * mult_a

    m = dc.score_matrix(lam, mu, model.goals.rho, s["model"]["max_goals"])
    mk = markets.goal_markets(m, lines["goals_total"], lines["team_goals"])

    stats_out = {}
    stat_lines = {"corners": ("corners_total", "corners_team"), "sot": ("sot_total", "sot_team"),
                  "cards": ("cards_total", "cards_team")}
    for stat in COUNT_STATS:
        e = model.expected_stat(stat, home, away, referee if stat == "cards" else None)
        if e is None:
            continue
        eh, ea = e
        # attacking absences also dent shot volume
        if stat in ("shots", "sot"):
            eh, ea = eh * mult_h, ea * mult_a
        entry = {"home": eh, "away": ea, "total": eh + ea}
        if stat in stat_lines:
            cm = model.stats[stat]
            ph, pa = cm.pmf(eh), cm.pmf(ea)
            tl, tml = stat_lines[stat]
            entry["total_lines"] = counts.line_probs(counts.convolve(ph, pa), lines[tl])
            entry["home_lines"] = counts.line_probs(ph, lines[tml])
            entry["away_lines"] = counts.line_probs(pa, lines[tml])
        stats_out[stat] = entry
    if referee and "cards" in model.stats:
        stats_out.setdefault("cards", {})["referee_factor"] = model.stats["cards"].referee_factor.get(referee)

    prop_out = {}
    if player_data:
        for side, xg in (("home", lam), ("away", mu)):
            pdict = player_data.get(side)
            if not pdict:
                continue
            team_sot = stats_out.get("sot", {}).get(side, xg * 3.0)
            prop_out[side] = props.player_props(
                pdict["players"], team_xg=xg, team_sot=team_sot, team_goals=pdict["team_goals"],
                team_shots=pdict["team_shots"], team_sot_total=pdict["team_sot"],
                team_minutes=pdict["team_minutes"], statuses=(lineup_status or {}).get(side))

    payload = {
        "xg": {"home": lam, "away": mu},
        "predicted_score": markets.most_likely_score(m),
        "markets": mk,
        "fair_odds": markets.attach_fair_odds(mk),
        "stats": stats_out,
        "adjustments": {"home_multiplier": mult_h, "away_multiplier": mult_a},
        "props": prop_out,
        "promoted": {"home": home in model.promoted, "away": away in model.promoted},
    }
    return _round_tree(payload)
