"""Phase A predictor: W/D/L probabilities and scorelines for any competition.

Expected goals for a match come from up to two strength sources plus context factors:

  * Dixon-Coles league model (per domestic league), fitted on a blend of goals and Understat xG.
  * ClubElo -> goals model, which works across leagues and competitions.

  same-league match (league game or a domestic cup between two clubs of that league):
        lambda = blend(DC, Elo, w)                        geometric blend, w tuned in the backtest
  cross-league match (Champions League, cups vs. other leagues' clubs):
        each team's DC strength is mapped onto the Elo scale, blended with its ClubElo rating,
        then the Elo goals model is applied (with that competition's home advantage)
  then: rest days, absences (injuries/suspensions/lineups), referee penalty rate.

Every factor can be switched off (config `factors`), which is how the backtest runs its ablation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from . import config
from .models import counts, dixon_coles as dc, elo as elo_model, markets

COUNT_STATS = ("shots", "sot", "corners", "cards")
ALL_FACTORS = ("xg", "elo", "rest", "injuries", "referee_penalties")


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
    xg_weight: float = 0.0

    def _dc_rating(self, team):
        i = self.goals.index.get(team)
        if i is None:
            return self.prior["attack"], self.prior["defence"]
        return float(self.goals.attack[i]), float(self.goals.defence[i])

    def knows(self, team: str) -> bool:
        return team in self.goals.index or team in self.promoted

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        ah, dh = self._dc_rating(home)
        aa, da = self._dc_rating(away)
        g = self.goals
        return (float(np.exp(g.intercept + g.home_adv + ah + da)), float(np.exp(g.intercept + aa + dh)))

    def strength(self) -> dict[str, float]:
        """attack - defence: one number per team, used to map DC ratings onto the Elo scale."""
        return {t: float(self.goals.attack[i] - self.goals.defence[i]) for t, i in self.goals.index.items()}

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
        f = m.referee_factor.get(referee, 1.0) if referee else 1.0
        return (float(np.exp(m.intercept + m.home_adv + gh + ca)) * f, float(np.exp(m.intercept + ga + ch)) * f)


def fit_league(df: pd.DataFrame, league: str, as_of: date, xi: float | None = None,
               fixture_teams: set[str] | None = None, xg_weight: float = 0.0,
               with_stats: bool = False) -> LeagueModel | None:
    s = config.settings()
    mcfg = s["model"]
    if xi is None:
        xi = config.competitions().get(league, {}).get("time_decay_xi", mcfg["time_decay_xi"])
    as_of_ts = pd.Timestamp(as_of)
    cur = config.season_code(as_of)
    keep = set(config.previous_seasons(cur, s["history_seasons"]) + [cur])
    hist = df[(df.league == league) & (df.status == "FT") & (df.date < as_of_ts)
              & df.season.isin(keep)].dropna(subset=["fthg", "ftag"])
    if len(hist) < 100:
        return None
    w = dc.time_weights((as_of_ts - hist.date).dt.days.to_numpy(), xi)

    prev = config.previous_seasons(cur, 1)[0]
    prev_teams = set(hist[hist.season == prev].home)
    cur_teams = set(df[(df.league == league) & (df.season == cur)].home) | (fixture_teams or set())
    promoted = (cur_teams - prev_teams) if prev_teams else set()
    prior = mcfg["promoted_prior"]

    yh, ya = hist.fthg.to_numpy(float), hist.ftag.to_numpy(float)
    if xg_weight > 0 and "xg_home" in hist:
        xh, xa = hist.xg_home.to_numpy(float), hist.xg_away.to_numpy(float)
        has = ~(np.isnan(xh) | np.isnan(xa))
        yh = np.where(has, xg_weight * np.nan_to_num(xh) + (1 - xg_weight) * yh, yh)
        ya = np.where(has, xg_weight * np.nan_to_num(xa) + (1 - xg_weight) * ya, ya)
    goals = dc.fit(hist.home.tolist(), hist.away.tolist(), yh, ya, weights=w, ridge=mcfg["ridge"],
                   prior_attack={t: prior["attack"] for t in promoted},
                   prior_defence={t: prior["defence"] for t in promoted}, xi=xi,
                   score_home=hist.fthg.to_numpy(float), score_away=hist.ftag.to_numpy(float))

    stats = {}
    avgs = {"goals_home": float(np.average(hist.fthg, weights=w)),
            "goals_away": float(np.average(hist.ftag, weights=w))}
    if with_stats:
        for stat, (hc, ac) in counts.STATS.items():
            sub = hist.dropna(subset=[hc, ac])
            if len(sub) < 100:
                continue
            sw = w[hist.index.get_indexer(sub.index)]
            refs = sub.referee.tolist() if stat == "cards" and sub.referee.notna().mean() > 0.5 else None
            stats[stat] = counts.fit_count_model(stat, sub.home.tolist(), sub.away.tolist(), sub[hc].to_numpy(),
                                                 sub[ac].to_numpy(), weights=sw, ridge=mcfg["count_ridge"],
                                                 referees=refs)
    return LeagueModel(league=league, as_of=as_of, goals=goals, stats=stats, promoted=promoted, prior=prior,
                       league_avgs=avgs, xg_weight=xg_weight)


@dataclass
class JointModel:
    """One Dixon-Coles fit over every modelled league plus European competition results.

    European games link the leagues onto one scale, so this rates any pairing (Champions League,
    cups between leagues) without ClubElo. Clubs never seen get the average rating of the clubs that
    only appear in Europe (outside the modelled leagues)."""
    goals: dc.DixonColes
    unknown: tuple[float, float]

    def expected_goals(self, home: str, away: str, hfa: bool = True) -> tuple[float, float]:
        g, idx = self.goals, self.goals.index

        def r(t):
            i = idx.get(t)
            return self.unknown if i is None else (float(g.attack[i]), float(g.defence[i]))

        (ah, dh), (aa, da) = r(home), r(away)
        return (float(np.exp(g.intercept + (g.home_adv if hfa else 0.0) + ah + da)),
                float(np.exp(g.intercept + aa + dh)))

    def knows(self, team: str) -> bool:
        return team in self.goals.index


def fit_joint(df: pd.DataFrame, as_of: date, xg_weight: float = 0.0, xi: float = 0.003,
              years: int = 4) -> JointModel | None:
    comps = config.competitions()
    europe = [k for k, v in comps.items() if v["type"] == "europe"]
    lgs = list(config.leagues()) + list(config.extra_leagues())
    as_of_ts = pd.Timestamp(as_of)
    h = df[(df.status == "FT") & df.fthg.notna() & df.league.isin(lgs + europe) & (df.date < as_of_ts)
           & (df.date >= as_of_ts - pd.Timedelta(days=365 * years))]
    if not h.league.isin(europe).any():
        return None
    w = dc.time_weights((as_of_ts - h.date).dt.days.to_numpy(), xi)
    yh, ya = h.fthg.to_numpy(float), h.ftag.to_numpy(float)
    if xg_weight > 0 and "xg_home" in h:
        xh, xa = h.xg_home.to_numpy(float), h.xg_away.to_numpy(float)
        has = ~(np.isnan(xh) | np.isnan(xa))
        yh = np.where(has, xg_weight * np.nan_to_num(xh) + (1 - xg_weight) * yh, yh)
        ya = np.where(has, xg_weight * np.nan_to_num(xa) + (1 - xg_weight) * ya, ya)
    top = set(h[h.league.isin(lgs)].home)          # clubs with domestic league data
    others = (set(h.home) | set(h.away)) - top
    p = config.settings()["model"]["outside_club_prior"]
    g = dc.fit(h.home.tolist(), h.away.tolist(), yh, ya, weights=w, ridge=config.settings()["model"]["ridge"],
               prior_attack={t: p["attack"] for t in others}, prior_defence={t: p["defence"] for t in others},
               score_home=h.fthg.to_numpy(float), score_away=h.ftag.to_numpy(float), xi=xi)
    oi = [g.index[t] for t in others if t in g.index]
    unknown = (float(np.mean(g.attack[oi])), float(np.mean(g.defence[oi]))) if oi else (p["attack"], p["defence"])
    return JointModel(goals=g, unknown=unknown)


def referee_penalty_rates(df: pd.DataFrame) -> tuple[dict[str, tuple[int, float]], float | None]:
    """{referee: (games, penalties per game)} and the overall rate, from matches with penalty data."""
    if "pens_home" not in df:
        return {}, None
    sub = df.dropna(subset=["pens_home", "pens_away", "referee"])
    if sub.empty:
        return {}, None
    tot = sub.pens_home + sub.pens_away
    rates = {r: (int(len(g)), float(tot[g.index].mean())) for r, g in sub.groupby("referee")}
    return rates, float(tot.mean())


@dataclass
class Predictor:
    """Everything needed to predict any fixture as of one date."""
    df: pd.DataFrame
    as_of: date
    elo_book: object | None = None                  # ingest.clubelo.EloBook
    elo_params: dict | None = None
    factors: dict = field(default_factory=dict)     # factor -> bool (missing = config default)
    xg_weight: float | None = None
    blend_weight: float | None = None               # weight on DC in the DC/Elo blend
    rest_coef: float | None = None
    models: dict | None = None                      # league -> LeagueModel (pre-fitted, optional)
    fixture_teams: dict = field(default_factory=dict)
    with_stats: bool = False                        # also fit corners/cards/shots models

    def __post_init__(self):
        s = config.settings()
        self.factors = {f: bool(self.factors.get(f, s["factors"].get(f, False))) for f in ALL_FACTORS}
        if self.xg_weight is None:
            self.xg_weight = s["model"]["xg_weight"] if self.factors["xg"] else 0.0
        if self.blend_weight is None:
            self.blend_weight = s["elo"]["blend_weight_dc"]
        if self.rest_coef is None:
            self.rest_coef = s["rest"]["coef"]
        if self.elo_params is None:
            self.elo_params = s["elo"]["default_params"]
        if self.models is None:
            self.models = {}
            for lg in config.leagues():
                m = fit_league(self.df, lg, self.as_of, fixture_teams=self.fixture_teams.get(lg),
                               xg_weight=self.xg_weight if self.factors["xg"] else 0.0, with_stats=self.with_stats)
                if m is not None:
                    self.models[lg] = m
        # each team's current domestic league = the league it most recently played in
        lg_rows = self.df[self.df.league.isin(list(config.leagues())) & (self.df.date < pd.Timestamp(self.as_of))]
        last = lg_rows.sort_values("date").groupby("home").league.last().to_dict()
        for lg, teams in self.fixture_teams.items():
            if lg in config.leagues():
                for t in teams:
                    last.setdefault(t, lg)
        self.team_league = last
        self.ref_rates, self.ref_league_rate = referee_penalty_rates(self.df[self.df.date < pd.Timestamp(self.as_of)])
        self._elo_maps = {}
        self._joint = None

    @property
    def joint(self) -> JointModel | None:
        if self._joint is None:
            self._joint = fit_joint(self.df, self.as_of, self.xg_weight if self.factors["xg"] else 0.0) or False
        return self._joint or None

    # ---------------- strength sources

    def _country(self, team: str) -> str | None:
        lg = self.team_league.get(team)
        return config.competitions().get(lg, {}).get("country") if lg else None

    def elo(self, team: str, day: str) -> float | None:
        if not self.elo_book:
            return None
        return self.elo_book.team_rating(team, day, self._country(team))

    def rest_days(self, team: str, day: str) -> float | None:
        if self.elo_book:
            club = self.elo_book.club_for(team, self._country(team))
            last = self.elo_book.last_match(club, day) if club else None
            if last:
                return float((date.fromisoformat(day) - date.fromisoformat(last)).days)
        prev = self.df[((self.df.home == team) | (self.df.away == team)) & (self.df.date < pd.Timestamp(day))]
        if prev.empty:
            return None
        return float((pd.Timestamp(day) - prev.date.max()).days)

    def _elo_map(self, lg: str, day: str):
        if lg not in self._elo_maps:
            m = self.models.get(lg)
            elos = {t: self.elo(t, day) for t in (m.goals.index if m else [])}
            self._elo_maps[lg] = elo_model.dc_to_elo_map(m.strength(), {t: e for t, e in elos.items() if e}) if m else None
        return self._elo_maps[lg]

    def components(self, comp: str, home: str, away: str, day: str) -> dict:
        """Raw ingredients (no blending/adjustment) for one fixture."""
        c = config.competitions().get(comp, {})
        lg_h, lg_a = self.team_league.get(home), self.team_league.get(away)
        same = None
        if c.get("type") == "league" and comp in self.models:
            same = comp
        elif c.get("type") == "cup" and lg_h and lg_h == lg_a and lg_h in self.models:
            same = lg_h
        dc_ga = None
        if same and self.models[same].knows(home) and self.models[same].knows(away):
            dc_ga = self.models[same].expected_goals(home, away)
        eh, ea = self.elo(home, day), self.elo(away, day)
        hfa_key = comp if c.get("type") in ("league", "europe") else None
        elo_ga = elo_model.expected_goals(self.elo_params, eh, ea, hfa_key) if eh and ea else None
        # cross-league: DC strength mapped onto the Elo scale
        dc_elo = None
        if not dc_ga and eh and ea and lg_h in self.models and lg_a in self.models:
            mh, ma = self._elo_map(lg_h, day), self._elo_map(lg_a, day)
            sh, sa = self.models[lg_h].strength().get(home), self.models[lg_a].strength().get(away)
            if mh and ma and sh is not None and sa is not None:
                dc_elo = (mh[0] + mh[1] * sh, ma[0] + ma[1] * sa)
        joint_ga, unknown = None, []
        if not dc_ga and c.get("type") in ("europe", "cup") and self.joint:
            joint_ga = self.joint.expected_goals(home, away)
            unknown = [t for t in (home, away) if not self.joint.knows(t)]
        big = (lg_h in self.models, lg_a in self.models)
        category = "top" if all(big) else "mixed" if any(big) else "outside"
        rhos = [m.goals.rho for m in self.models.values()]
        rho = self.models[same].goals.rho if same else (float(np.mean(rhos)) if rhos else 0.0)
        return {"dc": dc_ga, "elo": elo_ga, "elo_ratings": (eh, ea), "dc_elo": dc_elo, "hfa_key": hfa_key,
                "rho": rho, "rest": (self.rest_days(home, day), self.rest_days(away, day)),
                "joint": joint_ga, "unknown_teams": unknown, "category": category,
                "same_league": same}

    def referee_delta(self, referee: str | None) -> float | None:
        """Per-team expected-goal shift from a referee's (shrunk) penalty rate; None without data."""
        if not referee or self.ref_league_rate is None or referee not in self.ref_rates:
            return None
        n, rate = self.ref_rates[referee]
        cfg = config.settings()["referee"]
        k = cfg["shrink_games"]
        shrunk = (n * rate + k * self.ref_league_rate) / (n + k)
        return (shrunk - self.ref_league_rate) / 2 * cfg["penalty_goal_value"]

    def combine(self, comp_info: dict, absences: dict | None = None, referee: str | None = None) -> dict | None:
        return combine(comp_info, factors=self.factors, blend_weight=self.blend_weight, rest_coef=self.rest_coef,
                       elo_params=self.elo_params, absences=absences, referee_delta=self.referee_delta(referee))

    def predict(self, comp: str, home: str, away: str, day: str, absences: dict | None = None,
                referee: str | None = None) -> dict | None:
        info = self.components(comp, home, away, day)
        g = self.combine(info, absences, referee)
        if g is None:
            return None
        payload = scoreline_payload(g["lam"], g["mu"], g["rho"])
        rest = info["rest"]
        payload.update({
            "source": g["source"], "factors": g["applied"],
            "elo": {"home": info["elo_ratings"][0], "away": info["elo_ratings"][1]},
            "rest_days": {"home": rest[0], "away": rest[1]},
            "thin_data": info.get("unknown_teams") or [],
        })
        return payload


def shrink(lam: float, mu: float, s: float) -> tuple[float, float]:
    """Pull (s > 0) or push (s < 0) the two teams' expected goals toward/away from their geometric
    mean: a calibration knob for cross-league games, tuned per matchup category on past seasons."""
    if not s:
        return lam, mu
    g = float(np.sqrt(lam * mu))
    return float(g ** s * lam ** (1 - s)), float(g ** s * mu ** (1 - s))


def combine(info: dict, *, factors: dict, blend_weight: float, rest_coef: float, elo_params: dict,
            absences: dict | None = None, referee_delta: float | None = None,
            cross_shrink: dict | None = None) -> dict | None:
    """Blend strength sources and apply context factors (shared by the live pipeline and the backtest).

    info: Predictor.components() output. Returns {"lam", "mu", "rho", "source", "applied"} or None when
    no strength source is available for the fixture.
    """
    w = blend_weight
    dc_ga, elo_ga = info["dc"], info["elo"]
    if dc_ga and elo_ga and factors["elo"]:
        lam, mu = elo_model.blend(dc_ga[0], elo_ga[0], w), elo_model.blend(dc_ga[1], elo_ga[1], w)
        source = "Dixon-Coles + Elo"
    elif dc_ga:
        lam, mu, source = dc_ga[0], dc_ga[1], "Dixon-Coles"
    elif info.get("joint"):
        lam, mu = info["joint"]
        source = "Joint leagues + Europe"
        if elo_ga and factors["elo"]:
            lam, mu = elo_model.blend(lam, elo_ga[0], w), elo_model.blend(mu, elo_ga[1], w)
            source += " + Elo"
        cs = config.settings()["model"].get("cross_league_shrink", {}) if cross_shrink is None else cross_shrink
        lam, mu = shrink(lam, mu, cs.get(info.get("category"), 0.0))
    elif elo_ga:
        eh, ea = info["elo_ratings"]
        if info.get("dc_elo") and factors["elo"]:
            eh = w * info["dc_elo"][0] + (1 - w) * eh
            ea = w * info["dc_elo"][1] + (1 - w) * ea
            source = "Elo + league ratings"
        else:
            source = "Elo"
        lam, mu = elo_model.expected_goals(elo_params, eh, ea, info.get("hfa_key"))
    else:
        return None
    applied = {}
    if factors["rest"] and rest_coef:
        s = config.settings()["rest"]
        x = elo_model.rest_feature(*info["rest"], floor=s["floor_days"], cap=s["cap_days"])
        if x:
            lam, mu = lam * np.exp(rest_coef * x), mu * np.exp(-rest_coef * x)
            applied["rest"] = x
    if factors["injuries"] and absences:
        h, a = absences.get("home"), absences.get("away")
        if h:
            lam, mu = lam * h["attack"], mu * h["defence"]
        if a:
            mu, lam = mu * a["attack"], lam * a["defence"]
        if h or a:
            applied["absences"] = True
    if factors["referee_penalties"] and referee_delta:
        lam, mu = max(0.05, lam + referee_delta), max(0.05, mu + referee_delta)
        applied["referee_penalty_delta"] = round(referee_delta, 4)
    return {"lam": float(lam), "mu": float(mu), "rho": info["rho"], "source": source, "applied": applied}


def scoreline_payload(lam: float, mu: float, rho: float) -> dict:
    s = config.settings()
    m = dc.score_matrix(lam, mu, rho, s["model"]["max_goals"])
    mk = markets.goal_markets(m, s["lines"]["goals_total"], s["lines"]["team_goals"])
    return _round_tree({
        "xg": {"home": lam, "away": mu},
        "predicted_score": markets.most_likely_score(m),
        "top_scores": markets.correct_scores(m, 6),
        "markets": mk,
        "fair_odds": {"1x2": {k: (1 / v if v > 0 else None) for k, v in mk["1x2"].items()}},
    })


def _round_tree(x, n=4):
    if isinstance(x, dict):
        return {k: _round_tree(v, n) for k, v in x.items()}
    if isinstance(x, list):
        return [_round_tree(v, n) for v in x]
    if isinstance(x, (float, np.floating)):
        return round(float(x), n)
    return x


def predict_match(model: LeagueModel, home: str, away: str, referee: str | None = None, **_) -> dict:
    """League-only prediction (Dixon-Coles), kept for simple callers and tests."""
    lam, mu = model.expected_goals(home, away)
    p = scoreline_payload(lam, mu, model.goals.rho)
    p["promoted"] = {"home": home in model.promoted, "away": away in model.promoted}
    return p


def as_day(d) -> str:
    return (d if isinstance(d, str) else pd.Timestamp(d).strftime("%Y-%m-%d"))[:10]


