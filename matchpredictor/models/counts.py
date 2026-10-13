"""Team count models for corners, shots, shots on target and cards.

For each statistic we fit a log-linear rating model with home/away splits:

    E[home stat] = exp(c + home_adv + for[home] + against[away])
    E[away stat] = exp(c +            for[away] + against[home])

`for` is how much of the stat a team generates, `against` how much it
concedes to opponents (opponent adjustment). Time-decay weights and a ridge
penalty mirror the goals model. Over-dispersion is captured by a negative
binomial with a single method-of-moments dispersion per statistic; cards also
get a shrunk referee multiplier when the referee is known.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from scipy.stats import nbinom, poisson

STATS = {
    # name: (home column, away column)
    "shots": ("hs", "as_"),
    "sot": ("hst", "ast"),
    "corners": ("hc", "ac"),
    "cards": ("cards_h", "cards_a"),
}


@dataclass
class CountModel:
    stat: str
    teams: list[str]
    gen: np.ndarray
    conc: np.ndarray
    intercept: float
    home_adv: float
    alpha: float = 0.0                       # NB dispersion: Var = mu + alpha * mu^2
    referee_factor: dict = field(default_factory=dict)

    @property
    def index(self):
        return {t: i for i, t in enumerate(self.teams)}

    def expected(self, home: str, away: str, referee: str | None = None) -> tuple[float, float]:
        idx = self.index
        h, a = idx[home], idx[away]
        eh = float(np.exp(self.intercept + self.home_adv + self.gen[h] + self.conc[a]))
        ea = float(np.exp(self.intercept + self.gen[a] + self.conc[h]))
        f = self.referee_factor.get(referee, 1.0) if referee else 1.0
        return eh * f, ea * f

    def pmf(self, mean: float, max_k: int = 40) -> np.ndarray:
        return count_pmf(mean, self.alpha, max_k)


def count_pmf(mean: float, alpha: float, max_k: int = 40) -> np.ndarray:
    """Poisson (alpha == 0) or negative-binomial pmf over 0..max_k, renormalised."""
    k = np.arange(max_k + 1)
    if alpha <= 1e-6:
        p = poisson.pmf(k, mean)
    else:
        r = 1.0 / alpha
        p = nbinom.pmf(k, r, r / (r + mean))
    return p / p.sum()


def convolve(p1: np.ndarray, p2: np.ndarray) -> np.ndarray:
    out = np.convolve(p1, p2)
    return out / out.sum()


def line_probs(pmf: np.ndarray, lines) -> dict:
    k = np.arange(len(pmf))
    return {str(l): {"over": float(pmf[k > l].sum()), "under": float(pmf[k < l].sum())} for l in lines}


def fit_ratings(home, away, yh, ya, weights=None, ridge: float = 1.0):
    yh = np.asarray(yh, dtype=float)
    ya = np.asarray(ya, dtype=float)
    n = len(yh)
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    teams = sorted(set(home) | set(away))
    idx = {t: i for i, t in enumerate(teams)}
    hi = np.array([idx[t] for t in home])
    ai = np.array([idx[t] for t in away])
    k = len(teams)

    def obj(p):
        g, c_, b, h = p[:k], p[k:2 * k], p[2 * k], p[2 * k + 1]
        eh = b + h + g[hi] + c_[ai]
        ea = b + g[ai] + c_[hi]
        lh, la = np.exp(eh), np.exp(ea)
        ll = (w * (yh * eh - lh + ya * ea - la)).sum()
        rh = w * (yh - lh)
        ra = w * (ya - la)
        gg = np.bincount(hi, rh, k) + np.bincount(ai, ra, k)
        gc = np.bincount(ai, rh, k) + np.bincount(hi, ra, k)
        grad = -np.concatenate([gg, gc, [rh.sum() + ra.sum(), rh.sum()]])
        grad[:2 * k] += 2 * ridge * p[:2 * k]
        return -ll + ridge * (p[:2 * k] ** 2).sum(), grad

    mean = max(np.average(np.concatenate([yh, ya]), weights=np.concatenate([w, w])), 0.05)
    x0 = np.concatenate([np.zeros(2 * k), [np.log(mean), 0.0]])
    res = minimize(obj, x0, jac=True, method="L-BFGS-B")
    p = res.x
    return teams, p[:k], p[k:2 * k], float(p[2 * k]), float(p[2 * k + 1]), hi, ai


def estimate_alpha(y: np.ndarray, mu: np.ndarray, w: np.ndarray) -> float:
    """Method-of-moments NB dispersion: E[(y-mu)^2 - y] = alpha * mu^2."""
    num = (w * ((y - mu) ** 2 - y)).sum()
    den = (w * mu ** 2).sum()
    return float(max(0.0, num / den)) if den > 0 else 0.0


def fit_count_model(stat, home, away, yh, ya, weights=None, ridge=1.0, referees=None,
                    referee_shrink: float = 8.0) -> CountModel:
    yh = np.asarray(yh, dtype=float)
    ya = np.asarray(ya, dtype=float)
    w = np.ones(len(yh)) if weights is None else np.asarray(weights, dtype=float)
    teams, g, c, b, h, hi, ai = fit_ratings(home, away, yh, ya, w, ridge)
    mh = np.exp(b + h + g[hi] + c[ai])
    ma = np.exp(b + g[ai] + c[hi])

    ref_factor = {}
    if referees is not None:
        # Shrunk ratio of actual to model-expected totals, per referee.
        refs = np.asarray(referees, dtype=object)
        exp_tot = mh + ma
        act_tot = yh + ya
        avg = float(np.average(exp_tot, weights=w))
        for r in set(refs):
            if not r or (isinstance(r, float) and np.isnan(r)):
                continue
            sel = refs == r
            a_ = float((w[sel] * act_tot[sel]).sum())
            e_ = float((w[sel] * exp_tot[sel]).sum())
            ref_factor[r] = (a_ + referee_shrink * avg) / (e_ + referee_shrink * avg)

        # Dispersion should be measured after the referee effect is applied.
        f = np.array([ref_factor.get(r, 1.0) for r in refs])
        mh, ma = mh * f, ma * f

    alpha = estimate_alpha(np.concatenate([yh, ya]), np.concatenate([mh, ma]), np.concatenate([w, w]))
    return CountModel(stat=stat, teams=teams, gen=g, conc=c, intercept=b, home_adv=h,
                      alpha=alpha, referee_factor=ref_factor)
