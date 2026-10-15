"""Walk-forward accuracy of the corners, cards, shots and shots-on-target models.

Same protocol as the result backtest: before each block (default 14 days) the league models are
refitted on earlier matches only. For each market we report the error on the expected total, and
for the main over/under line: how often the more likely side won, and the Brier score versus a
constant baseline (the previous season's over rate, which the model could have known).
Player markets are not backtested: that needs per-match player data we don't store.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import config
from .models import cards, corners, shots
from .predict import fit_league

MAIN_LINE = {"corners": 9.5, "cards": 4.5, "shots": 24.5, "sot": 8.5}
ACTUAL = {"corners": ("hc", "ac"), "cards": ("cards_h", "cards_a"), "shots": ("hs", "as_"), "sot": ("hst", "ast")}


def _predict_all(lm, comp, home, away, referee):
    models = {k: {comp: v} for k, v in lm.stats.items()}
    tl = {home: comp, away: comp}
    out = {}
    c = corners.predict(models.get("corners", {}), comp, home, away, tl)
    if c:
        out["corners"] = c
    k = cards.predict(models.get("cards", {}), comp, home, away, tl, referee)
    if k:
        out["cards"] = k
    out.update(shots.predict(models.get("shots", {}), models.get("sot", {}), comp, home, away, tl) or {})
    return out


def collect(df, league: str, season: str, refit_days: int = 14, xg_weight: float = 0.75) -> list[dict]:
    games = df[(df.league == league) & (df.season == season) & (df.status == "FT")].sort_values("date")
    rows = []
    if games.empty:
        return rows
    block, end = games.date.min(), games.date.max()
    while block <= end:
        nxt = block + timedelta(days=refit_days)
        chunk = games[(games.date >= block) & (games.date < nxt)]
        if not chunk.empty:
            lm = fit_league(df, league, block.date(), xg_weight=xg_weight, with_stats=True)
            if lm is not None:
                for g in chunk.itertuples():
                    ref = g.referee if isinstance(g.referee, str) else None
                    pred = _predict_all(lm, league, g.home, g.away, ref)
                    for mk, (ch, ca) in ACTUAL.items():
                        if mk not in pred or pd.isna(getattr(g, ch)) or pd.isna(getattr(g, ca)):
                            continue
                        line = str(MAIN_LINE[mk])
                        p_over = pred[mk]["total_lines"].get(line, {}).get("over")
                        if p_over is None:
                            continue
                        rows.append({"market": mk, "league": league, "exp": pred[mk]["total"],
                                     "act": float(getattr(g, ch) + getattr(g, ca)), "p_over": p_over,
                                     "line": MAIN_LINE[mk]})
        block = nxt
    return rows


def summarise(rows: list[dict], df, season: str) -> dict:
    prev = config.previous_seasons(season, 1)[0]
    out = {}
    for mk in MAIN_LINE:
        sub = [r for r in rows if r["market"] == mk]
        if not sub:
            continue
        exp = np.array([r["exp"] for r in sub])
        act = np.array([r["act"] for r in sub])
        p = np.array([r["p_over"] for r in sub])
        y = (act > MAIN_LINE[mk]).astype(float)
        ch, ca = ACTUAL[mk]
        base_df = df[(df.season == prev) & df.league.isin(list(config.leagues()))].dropna(subset=[ch, ca])
        base = float(((base_df[ch] + base_df[ca]) > MAIN_LINE[mk]).mean()) if len(base_df) else float(y.mean())
        out[mk] = {
            "n": len(sub), "line": MAIN_LINE[mk], "mae_total": round(float(np.abs(exp - act).mean()), 3),
            "mean_pred": round(float(exp.mean()), 2), "mean_actual": round(float(act.mean()), 2),
            "side_hit": round(float(np.mean((p >= 0.5) == (y == 1))), 4),
            "brier": round(float(((p - y) ** 2).mean()), 4),
            "brier_baseline": round(float(((base - y) ** 2).mean()), 4),
            "over_rate": round(float(y.mean()), 4),
        }
    return out


def run(conn, season: str | None = None, refit_days: int = 14, df=None) -> dict:
    if df is None:
        from . import model_data
        df = model_data.load_frame()
    if season is None:
        season = config.previous_seasons(config.season_code(pd.Timestamp.now()), 1)[0]
    xw = config.settings()["model"]["xg_weight"] if config.settings()["factors"].get("xg") else 0.0
    rows = []
    for lg in config.leagues():
        rows += collect(df, lg, season, refit_days, xw)
    return {"season": season, "season_label": f"20{season[:2]}-{season[2:]}", "refit_days": refit_days,
            "markets": summarise(rows, df, season),
            "by_league": {lg: summarise([r for r in rows if r["league"] == lg], df, season) for lg in config.leagues()},
            "players": "not backtested (needs per-match player data)"}
