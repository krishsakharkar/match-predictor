"""Derive betting-market probabilities from a score matrix, plus odds helpers."""
from __future__ import annotations

import numpy as np


def fair_odds(p: float) -> float | None:
    """Decimal fair odds for probability p (None when p == 0)."""
    if p <= 0:
        return None
    return 1.0 / p


def implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


def result_probs(m: np.ndarray) -> dict[str, float]:
    return {
        "home": float(np.tril(m, -1).sum()),
        "draw": float(np.trace(m)),
        "away": float(np.triu(m, 1).sum()),
    }


def total_goals_dist(m: np.ndarray) -> np.ndarray:
    n = m.shape[0] + m.shape[1] - 1
    out = np.zeros(n)
    for i in range(m.shape[0]):
        out[i:i + m.shape[1]] += m[i]
    return out


def over_under(dist: np.ndarray, line: float) -> dict[str, float]:
    """Over/under for a half-goal line given a pmf over 0..N."""
    k = np.arange(len(dist))
    over = float(dist[k > line].sum())
    return {"over": over, "under": float(dist[k < line].sum())}


def btts(m: np.ndarray) -> dict[str, float]:
    yes = float(m[1:, 1:].sum())
    return {"yes": yes, "no": float(m.sum() - yes)}


def correct_scores(m: np.ndarray, top: int = 5) -> list[dict]:
    flat = np.argsort(m, axis=None)[::-1][:top]
    out = []
    for f in flat:
        i, j = np.unravel_index(f, m.shape)
        p = float(m[i, j])
        out.append({"score": f"{i}-{j}", "prob": p, "fair_odds": fair_odds(p)})
    return out


def most_likely_score(m: np.ndarray) -> str:
    i, j = np.unravel_index(np.argmax(m), m.shape)
    return f"{i}-{j}"


def goal_markets(m: np.ndarray, total_lines, team_lines) -> dict:
    home_dist = m.sum(axis=1)
    away_dist = m.sum(axis=0)
    tot = total_goals_dist(m)
    return {
        "1x2": result_probs(m),
        "double_chance": {
            "1x": float(np.tril(m).sum()),
            "x2": float(np.triu(m).sum()),
            "12": float(1 - np.trace(m)),
        },
        "totals": {str(l): over_under(tot, l) for l in total_lines},
        "btts": btts(m),
        "team_totals": {
            "home": {str(l): over_under(home_dist, l) for l in team_lines},
            "away": {str(l): over_under(away_dist, l) for l in team_lines},
        },
        "correct_score": correct_scores(m, 5),
    }


def attach_fair_odds(markets: dict) -> dict:
    """Return a parallel structure of fair decimal odds for every probability leaf."""
    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [dict(item, fair_odds=fair_odds(item["prob"])) if isinstance(item, dict) else item
                    for item in x]
        if isinstance(x, float):
            return fair_odds(x)
        return x
    return walk(markets)
