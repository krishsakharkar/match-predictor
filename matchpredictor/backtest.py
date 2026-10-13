"""Walk-forward backtest: refit weekly using only data available before each block."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from . import config, tracking, value
from .predict import fit_league, load_matches, predict_match

log = logging.getLogger(__name__)


def walk_forward(df: pd.DataFrame, league: str, season: str, xi: float | None = None,
                 refit_days: int = 7) -> list[dict]:
    games = df[(df.league == league) & (df.season == season) & (df.status == "FT")].sort_values("date")
    if games.empty:
        return []
    rows = []
    start = games.date.min()
    end = games.date.max()
    block = start
    while block <= end:
        nxt = block + timedelta(days=refit_days)
        chunk = games[(games.date >= block) & (games.date < nxt)]
        if not chunk.empty:
            model = fit_league(df, league, block.date(), xi=xi)
            if model is not None:
                for _, g in chunk.iterrows():
                    p = predict_match(model, g.home, g.away, referee=g.referee)
                    rows.append(_row(g, p))
        block = nxt
    return rows


def _row(g, p) -> dict:
    mk = p["markets"]
    st = p["stats"]
    r = {"id": g.id, "league": g.league, "date": g.date, "home": g.home, "away": g.away,
         "fthg": int(g.fthg), "ftag": int(g.ftag), "predicted_score": p["predicted_score"],
         "p_home": mk["1x2"]["home"], "p_draw": mk["1x2"]["draw"], "p_away": mk["1x2"]["away"],
         "p_over25": mk["totals"]["2.5"]["over"], "p_btts": mk["btts"]["yes"],
         "xg_home": p["xg"]["home"], "xg_away": p["xg"]["away"]}
    for stat, col_h, col_a, line in (("corners", "hc", "ac", "9.5"), ("cards", "cards_h", "cards_a", "3.5"),
                                     ("sot", "hst", "ast", "8.5")):
        if stat in st and pd.notna(g[col_h]) and pd.notna(g[col_a]):
            r[f"{stat}_exp"] = st[stat]["total"]
            r[f"{stat}_act"] = int(g[col_h] + g[col_a])
            r[f"{stat}_p_over"] = st[stat]["total_lines"][line]["over"]
            r[f"{stat}_line"] = float(line)
    for c in ("close_h", "close_d", "close_a", "close_o25", "close_u25"):
        r[c] = None if pd.isna(g[c]) else float(g[c])
    return r


def baseline_rows(df: pd.DataFrame, league: str, season: str, rows: list[dict]) -> list[dict]:
    """Naive benchmark: home/draw/away frequencies from the previous seasons."""
    prev = df[(df.league == league) & (df.season < season) & (df.status == "FT")]
    hg, ag = prev.fthg, prev.ftag
    ph, pd_, pa = (hg > ag).mean(), (hg == ag).mean(), (hg < ag).mean()
    po = ((hg + ag) > 2.5).mean()
    pb = ((hg > 0) & (ag > 0)).mean()
    return [dict(r, p_home=ph, p_draw=pd_, p_away=pa, p_over25=po, p_btts=pb) for r in rows]


def market_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not (r["close_h"] and r["close_d"] and r["close_a"]):
            continue
        ph, pd_, pa = value.remove_vig([r["close_h"], r["close_d"], r["close_a"]])
        po = None
        if r["close_o25"] and r["close_u25"]:
            po = value.remove_vig([r["close_o25"], r["close_u25"]])[0]
        out.append(dict(r, p_home=ph, p_draw=pd_, p_away=pa, p_over25=po, p_btts=None))
    return out


def simulate_bets(rows: list[dict]) -> dict:
    """Value-bet simulation at closing odds (a harsh test: closing lines are the sharpest prices)."""
    cfg = config.settings()["value"]
    bets = []
    for r in rows:
        markets = []
        if r["close_h"] and r["close_d"] and r["close_a"]:
            odds = [r["close_h"], r["close_d"], r["close_a"]]
            fair = value.remove_vig(odds)
            for sel, o, f in zip(("home", "draw", "away"), odds, fair):
                markets.append(("h2h", sel, None, r[f"p_{sel}"], o, f))
        if r["close_o25"] and r["close_u25"]:
            odds = [r["close_o25"], r["close_u25"]]
            fair = value.remove_vig(odds)
            markets.append(("totals", "over", 2.5, r["p_over25"], odds[0], fair[0]))
            markets.append(("totals", "under", 2.5, 1 - r["p_over25"], odds[1], fair[1]))
        for mkt, sel, line, p, o, f in markets:
            a = value.assess(p, o, f, cfg)
            if a["is_value"]:
                res = tracking.settle_selection(mkt, sel, line, r["fthg"], r["ftag"])
                bets.append({"stake": a["stake"], "odds_taken": o, "result": res, "closing_odds": o,
                             "market": mkt, "edge": a["edge"]})
    out = tracking.score_bets(bets)
    out.pop("avg_clv", None)          # CLV is zero by construction when betting the close
    out.pop("clv_positive_rate", None)
    out.pop("n_clv", None)
    for mkt in ("h2h", "totals"):
        sub = [b for b in bets if b["market"] == mkt]
        out[f"{mkt}_roi_flat"] = tracking.score_bets(sub)["roi_flat"]
        out[f"{mkt}_n"] = len(sub)
    return out


def count_metrics(rows: list[dict]) -> dict:
    out = {}
    for stat in ("corners", "cards", "sot"):
        sub = [r for r in rows if f"{stat}_exp" in r]
        if not sub:
            continue
        exp = np.array([r[f"{stat}_exp"] for r in sub])
        act = np.array([r[f"{stat}_act"] for r in sub])
        p = np.array([r[f"{stat}_p_over"] for r in sub])
        y = np.array([r[f"{stat}_act"] > r[f"{stat}_line"] for r in sub]).astype(float)
        base = np.full_like(p, y.mean())
        out[stat] = {
            "n": len(sub), "mae_total": round(float(np.abs(exp - act).mean()), 3),
            "mean_pred": round(float(exp.mean()), 2), "mean_actual": round(float(act.mean()), 2),
            "line": sub[0][f"{stat}_line"],
            "brier_over": round(float(((p - y) ** 2).mean()), 4),
            "brier_over_constant": round(float(((base - y) ** 2).mean()), 4),
        }
    return out


def tune_xi(df, league, season, grid=None) -> tuple[float, dict]:
    grid = grid or config.settings()["model"]["xi_grid"]
    scores = {}
    for xi in grid:
        rows = walk_forward(df, league, season, xi=xi, refit_days=14)
        scores[xi] = tracking.score_predictions(rows)["log_loss_1x2"] if rows else float("inf")
    best = min(scores, key=scores.get)
    return best, scores


def run(conn, season: str | None = None, tune: bool = True) -> dict:
    df = load_matches(conn)
    if season is None:
        season = config.previous_seasons(config.season_code(pd.Timestamp.now()), 1)[0]
    tune_season = config.previous_seasons(season, 1)[0]
    report = {"season": season, "tune_season": tune_season, "leagues": {}}
    all_rows, all_market = [], []
    for lg in config.leagues():
        xi = None
        xi_scores = None
        if tune:
            xi, xi_scores = tune_xi(df, lg, tune_season)
        rows = walk_forward(df, lg, season, xi=xi)
        if not rows:
            continue
        mrows = market_rows(rows)
        all_rows += rows
        all_market += mrows
        model = tracking.score_predictions(rows)
        report["leagues"][lg] = {
            "xi": xi if xi is not None else config.leagues()[lg].get(
                "time_decay_xi", config.settings()["model"]["time_decay_xi"]),
            "xi_tuning_log_loss": {str(k): round(v, 4) for k, v in (xi_scores or {}).items()},
            "model": model,
            "baseline": _strip(tracking.score_predictions(baseline_rows(df, lg, season, rows))),
            "market_closing": _strip(tracking.score_predictions(mrows)) if mrows else None,
            "market_closing_model_same_games": _strip(tracking.score_predictions(
                [r for r in rows if r["close_h"]])) if mrows else None,
            "value_bets_at_close": simulate_bets(rows) if mrows else None,
            "counts": count_metrics(rows),
            "data_source": "football-data" if mrows else "mirror (no odds)",
        }
    report["overall"] = _strip(tracking.score_predictions(all_rows), keep_calibration=True)
    report["overall_value_bets_at_close"] = simulate_bets(all_rows) if all_market else None
    return report


def _strip(d: dict, keep_calibration=False) -> dict:
    return {k: v for k, v in d.items() if k != "biggest_misses" and (keep_calibration or k != "calibration")}


def to_markdown(rep: dict) -> str:
    lines = [f"Walk-forward backtest, season {rep['season']} (time decay tuned on {rep['tune_season']}), "
             "weekly refits using only data available before each match week.", "",
             "| League | n | Brier 1X2 | Log loss 1X2 | Acc 1X2 | Baseline Brier | Market Brier | Brier O2.5 | Brier BTTS |",
             "|---|---|---|---|---|---|---|---|---|"]
    for lg, r in rep["leagues"].items():
        m, b, mk = r["model"], r["baseline"], r["market_closing"]
        lines.append(f"| {lg} | {m['n']} | {m['brier_1x2']} | {m['log_loss_1x2']} | {m['accuracy_1x2']} | "
                     f"{b['brier_1x2']} | {mk['brier_1x2'] if mk else 'n/a'} | {m['brier_over25']} | {m['brier_btts']} |")
    lines += ["", "Count models (total per match):", "",
              "| League | Stat | MAE | Mean pred | Mean actual | Line | Brier over | Brier constant |",
              "|---|---|---|---|---|---|---|---|"]
    for lg, r in rep["leagues"].items():
        for stat, c in r["counts"].items():
            lines.append(f"| {lg} | {stat} | {c['mae_total']} | {c['mean_pred']} | {c['mean_actual']} | "
                         f"{c['line']} | {c['brier_over']} | {c['brier_over_constant']} |")
    return "\n".join(lines)


def main(conn, out_path=None):
    rep = run(conn)
    if out_path:
        with open(out_path, "w") as f:
            json.dump(rep, f, indent=1, default=str)
    return rep
