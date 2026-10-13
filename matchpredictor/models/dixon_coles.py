"""Dixon-Coles bivariate Poisson goals model with time-decay weighting.

    lambda_home = exp(intercept + home_adv + attack[home] + defence[away])
    mu_away     = exp(intercept + attack[away] + defence[home])

`defence` is a *weakness* rating: higher means the team concedes more.
The low-score dependence correction tau(x, y) (Dixon & Coles 1997) adjusts
P(0-0), P(1-0), P(0-1), P(1-1) via the parameter rho.

Matches are weighted by exp(-xi * days_ago). A small ridge penalty keeps
ratings identifiable and shrinks teams with little data toward a prior
(0 for established teams, a weaker prior for newly promoted sides).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


def time_weights(days_ago: np.ndarray, xi: float) -> np.ndarray:
    return np.exp(-xi * np.asarray(days_ago, dtype=float))


def tau(x, y, lam, mu, rho):
    """Dixon-Coles low-score correction factor (vectorised)."""
    x = np.asarray(x)
    y = np.asarray(y)
    out = np.ones(np.broadcast(x, y, lam, mu).shape, dtype=float)
    out = np.where((x == 0) & (y == 0), 1.0 - lam * mu * rho, out)
    out = np.where((x == 0) & (y == 1), 1.0 + lam * rho, out)
    out = np.where((x == 1) & (y == 0), 1.0 + mu * rho, out)
    out = np.where((x == 1) & (y == 1), 1.0 - rho, out)
    return out


@dataclass
class DixonColes:
    teams: list[str]
    attack: np.ndarray
    defence: np.ndarray
    home_adv: float
    rho: float
    intercept: float = 0.0
    xi: float = 0.0
    converged: bool = True
    n_matches: int = 0
    effective_matches: dict = field(default_factory=dict)

    @property
    def index(self) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.teams)}

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        idx = self.index
        if home not in idx or away not in idx:
            raise KeyError(f"unknown team: {home if home not in idx else away}")
        h, a = idx[home], idx[away]
        lam = float(np.exp(self.intercept + self.home_adv + self.attack[h] + self.defence[a]))
        mu = float(np.exp(self.intercept + self.attack[a] + self.defence[h]))
        return lam, mu

    def ratings(self) -> list[dict]:
        return sorted(
            (
                {"team": t, "attack": round(float(self.attack[i]), 3),
                 "defence": round(float(self.defence[i]), 3)}
                for i, t in enumerate(self.teams)
            ),
            key=lambda r: r["attack"] - r["defence"],
            reverse=True,
        )


def score_matrix(lam: float, mu: float, rho: float = 0.0, max_goals: int = 10) -> np.ndarray:
    """P(home=i, away=j) for i, j in 0..max_goals, DC-corrected and renormalised to sum to 1."""
    goals = np.arange(max_goals + 1)
    m = np.outer(poisson.pmf(goals, lam), poisson.pmf(goals, mu))
    x, y = np.meshgrid(goals, goals, indexing="ij")
    m = m * tau(x, y, lam, mu, rho)
    m = np.clip(m, 0.0, None)
    return m / m.sum()


def rho_bounds(lam: np.ndarray, mu: np.ndarray) -> tuple[float, float]:
    """rho range for which every tau stays positive."""
    lo = max(-1.0 / np.max(lam), -1.0 / np.max(mu))
    hi = min(1.0 / np.max(lam * mu), 1.0)
    return lo, hi


def make_objective(home, away, home_goals, away_goals, weights=None, ridge=0.5,
                   prior_attack=None, prior_defence=None, score_home=None, score_away=None):
    """Build the penalised negative log-likelihood and its analytic gradient.

    Parameter vector layout: [attack(k), defence(k), intercept, home_adv, rho].
    `home_goals/away_goals` are the Poisson targets (may be non-integer, e.g. a goals/xG blend);
    `score_home/score_away` are the actual scorelines used for the low-score tau correction
    (default: the targets). Returns (objective, context).
    """
    home_goals = np.asarray(home_goals, dtype=float)
    away_goals = np.asarray(away_goals, dtype=float)
    n = len(home_goals)
    if n == 0:
        raise ValueError("no matches to fit")
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)

    teams = sorted(set(home) | set(away))
    idx = {t: i for i, t in enumerate(teams)}
    hi = np.array([idx[t] for t in home])
    ai = np.array([idx[t] for t in away])
    k = len(teams)
    pa = np.array([(prior_attack or {}).get(t, 0.0) for t in teams])
    pd = np.array([(prior_defence or {}).get(t, 0.0) for t in teams])

    sh = home_goals if score_home is None else np.asarray(score_home, dtype=float)
    sa = away_goals if score_away is None else np.asarray(score_away, dtype=float)
    x00 = (sh == 0) & (sa == 0)
    x01 = (sh == 0) & (sa == 1)
    x10 = (sh == 1) & (sa == 0)
    x11 = (sh == 1) & (sa == 1)

    def negll(p):
        att, dfn, c, h, rho = p[:k], p[k:2 * k], p[2 * k], p[2 * k + 1], p[2 * k + 2]
        eta_h = c + h + att[hi] + dfn[ai]
        eta_a = c + att[ai] + dfn[hi]
        lam, mu = np.exp(eta_h), np.exp(eta_a)

        t = np.ones(n)
        t[x00] = 1.0 - lam[x00] * mu[x00] * rho
        t[x01] = 1.0 + lam[x01] * rho
        t[x10] = 1.0 + mu[x10] * rho
        t[x11] = 1.0 - rho
        if np.any(t <= 0):
            return 1e12, np.zeros_like(p)

        ll = np.log(t) + home_goals * eta_h - lam + away_goals * eta_a - mu
        # d(log tau)/d(eta_h), d/d(eta_a), d/d(rho)
        dth = np.zeros(n)
        dta = np.zeros(n)
        dtr = np.zeros(n)
        dth[x00] = -lam[x00] * mu[x00] * rho / t[x00]
        dta[x00] = dth[x00]
        dtr[x00] = -lam[x00] * mu[x00] / t[x00]
        dth[x01] = lam[x01] * rho / t[x01]
        dtr[x01] = lam[x01] / t[x01]
        dta[x10] = mu[x10] * rho / t[x10]
        dtr[x10] = mu[x10] / t[x10]
        dtr[x11] = -1.0 / t[x11]

        gh = w * (home_goals - lam + dth)   # d ll / d eta_h
        ga = w * (away_goals - mu + dta)    # d ll / d eta_a

        g_att = np.bincount(hi, gh, k) + np.bincount(ai, ga, k)
        g_def = np.bincount(ai, gh, k) + np.bincount(hi, ga, k)

        obj = -(w * ll).sum() + ridge * (((att - pa) ** 2).sum() + ((dfn - pd) ** 2).sum())
        grad = -np.concatenate([g_att, g_def, [gh.sum() + ga.sum(), gh.sum(), (w * dtr).sum()]])
        grad[:k] += 2 * ridge * (att - pa)
        grad[k:2 * k] += 2 * ridge * (dfn - pd)
        return obj, grad

    mean_goals = max(np.average(np.concatenate([home_goals, away_goals]),
                                weights=np.concatenate([w, w])), 0.1)
    ctx = {"teams": teams, "idx": idx, "hi": hi, "ai": ai, "w": w, "k": k, "n": n,
           "x0": np.concatenate([pa, pd, [np.log(mean_goals), 0.25, -0.05]])}
    return negll, ctx


def fit(
    home: list[str],
    away: list[str],
    home_goals: np.ndarray,
    away_goals: np.ndarray,
    weights: np.ndarray | None = None,
    ridge: float = 0.5,
    prior_attack: dict[str, float] | None = None,
    prior_defence: dict[str, float] | None = None,
    xi: float = 0.0,
    score_home: np.ndarray | None = None,
    score_away: np.ndarray | None = None,
) -> DixonColes:
    negll, ctx = make_objective(home, away, home_goals, away_goals, weights, ridge,
                                prior_attack, prior_defence, score_home, score_away)
    k, w, hi, ai = ctx["k"], ctx["w"], ctx["hi"], ctx["ai"]
    bounds = [(-4, 4)] * (2 * k) + [(-3, 3), (-1, 1), (-0.25, 0.25)]
    res = minimize(negll, ctx["x0"], jac=True, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 1000})
    p = res.x
    eff = {t: float(w[hi == i].sum() + w[ai == i].sum()) for t, i in ctx["idx"].items()}
    return DixonColes(teams=ctx["teams"], attack=p[:k], defence=p[k:2 * k],
                      intercept=float(p[2 * k]), home_adv=float(p[2 * k + 1]), rho=float(p[2 * k + 2]),
                      xi=xi, converged=bool(res.success), n_matches=ctx["n"], effective_matches=eff)
