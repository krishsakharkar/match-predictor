"""Elo -> expected goals, plus the small regression helpers used for context factors.

Elo goals model (fitted by Poisson maximum likelihood on league matches with pre-match ClubElo ratings):

    log lambda_home = alpha_home + hfa[comp] + beta * dr
    log mu_away     = alpha_away             - beta * dr          dr = (elo_home - elo_away) / 100

`hfa[comp]` is a per-competition home-advantage offset (ridge-shrunk to 0, i.e. to the pooled value),
so competitions with little data fall back to the pooled home advantage.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize, minimize_scalar


def expected_goals(params: dict, elo_home: float, elo_away: float, comp: str | None = None) -> tuple[float, float]:
    dr = (elo_home - elo_away) / 100.0
    hfa = params.get("hfa", {}).get(comp, 0.0) if comp else 0.0
    lam = float(np.exp(params["alpha_home"] + hfa + params["beta"] * dr))
    mu = float(np.exp(params["alpha_away"] - params["beta"] * dr))
    return lam, mu


def fit(elo_home, elo_away, home_goals, away_goals, comps=None, weights=None, hfa_ridge: float = 20.0) -> dict:
    eh, ea = np.asarray(elo_home, float), np.asarray(elo_away, float)
    yh, ya = np.asarray(home_goals, float), np.asarray(away_goals, float)
    n = len(yh)
    if n < 50:
        raise ValueError("not enough matches to fit the Elo goals model")
    w = np.ones(n) if weights is None else np.asarray(weights, float)
    dr = (eh - ea) / 100.0
    comps = list(comps) if comps is not None else ["_"] * n
    names = sorted(set(comps))
    ci = np.array([names.index(c) for c in comps])
    k = len(names)

    def obj(p):
        ah, aa, b, h = p[0], p[1], p[2], p[3:]
        eta_h = ah + h[ci] + b * dr
        eta_a = aa - b * dr
        lh, la = np.exp(eta_h), np.exp(eta_a)
        ll = (w * (yh * eta_h - lh + ya * eta_a - la)).sum()
        rh, ra = w * (yh - lh), w * (ya - la)
        g = np.concatenate([[rh.sum(), ra.sum(), (rh * dr).sum() - (ra * dr).sum()], np.bincount(ci, rh, k)])
        g = -g
        g[3:] += 2 * hfa_ridge * h
        return -ll + hfa_ridge * (h ** 2).sum(), g

    res = minimize(obj, np.concatenate([[0.35, 0.1, 0.12], np.zeros(k)]), jac=True, method="L-BFGS-B")
    p = res.x
    return {"alpha_home": float(p[0]), "alpha_away": float(p[1]), "beta": float(p[2]),
            "hfa": {c: float(p[3 + i]) for i, c in enumerate(names) if c != "_"}, "n": int(n)}


def blend(a: float, b: float, weight_a: float) -> float:
    """Geometric blend of two expected-goal values (weight on the first)."""
    return float(np.exp(weight_a * np.log(a) + (1 - weight_a) * np.log(b)))


def dc_to_elo_map(strength: dict[str, float], elos: dict[str, float]) -> tuple[float, float] | None:
    """Least-squares map from a league's DC strength (attack - defence) to Elo: elo ~ a + b * strength.

    Lets a team's (xG-informed) league rating be expressed on ClubElo's cross-league scale, so it can
    be blended with Elo for Champions League / cup games between leagues.
    """
    teams = [t for t in strength if t in elos]
    if len(teams) < 8:
        return None
    x = np.array([strength[t] for t in teams])
    y = np.array([elos[t] for t in teams])
    if np.var(x) < 1e-9:
        return None
    b, a = np.polyfit(x, y, 1)
    return float(a), float(b)


def fit_offset_coef(base_home, base_away, feature, home_goals, away_goals, bound: float = 0.5) -> float:
    """1-D Poisson MLE for gamma in  lambda = base_home * e^(gamma*f),  mu = base_away * e^(-gamma*f)."""
    bh, ba = np.asarray(base_home, float), np.asarray(base_away, float)
    f = np.asarray(feature, float)
    yh, ya = np.asarray(home_goals, float), np.asarray(away_goals, float)

    def nll(g):
        lh, la = bh * np.exp(g * f), ba * np.exp(-g * f)
        return -(yh * np.log(lh) - lh + ya * np.log(la) - la).sum()

    return float(minimize_scalar(nll, bounds=(-bound, bound), method="bounded").x)


def rest_feature(rest_home: float | None, rest_away: float | None, floor: float = 2, cap: float = 7) -> float:
    """Clipped rest-day difference; 0 when either side is unknown."""
    if rest_home is None or rest_away is None:
        return 0.0
    return float(np.clip(rest_home, floor, cap) - np.clip(rest_away, floor, cap))
