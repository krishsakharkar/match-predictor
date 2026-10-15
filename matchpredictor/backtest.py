"""Phase A accuracy backtest: walk-forward, weekly refits, ablation, bookmaker benchmark.

1. Collect: for every league match of the tuning season (default 2024-25) and the test season
   (2025-26), refit the league models each week on earlier data only and record the raw ingredients
   (Dixon-Coles expected goals for each xG weight, Elo expected goals, rest days, absences).
2. Tune on the tuning season, pooled over leagues: xG weight, DC/Elo blend weight, rest coefficient,
   and whether injuries help. A factor is kept only if it improves log loss by > KEEP_THRESHOLD.
3. Test on the test season: base model, base + each factor, the selected model, and the selected
   model minus each factor. Benchmark: closing bookmaker odds (accuracy yardstick, not betting).
4. Champions League: walk-forward on the latest season with results (Elo vs Elo + league ratings).
"""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from . import config, tracking, value
from .absences import match_absences
from .history import fit_elo_params
from .models import dixon_coles as dc, elo as elo_model, markets
from .predict import ALL_FACTORS, Predictor, combine, fit_league, load_matches

log = logging.getLogger(__name__)
KEEP_THRESHOLD = 0.001          # minimum log-loss improvement for a factor to stay in the model
MIN_EUROPE_SEASON = 100        # finished UCL games for a season to be backtested / used for calibration
FRESH_GRID = (0, 1, 3, 6)       # injuries: ignore absences older than k straight matches (0 = count all)


# ---------------------------------------------------------------- collection

def _blocks(games: pd.DataFrame, refit_days: int):
    block, end = games.date.min(), games.date.max()
    while block <= end:
        nxt = block + timedelta(days=refit_days)
        chunk = games[(games.date >= block) & (games.date < nxt)]
        if not chunk.empty:
            yield block, chunk
        block = nxt


def _meta(g) -> dict:
    r = {"id": g.id, "league": g.league, "date": g.date.strftime("%Y-%m-%d"), "home": g.home, "away": g.away,
         "fthg": int(g.fthg), "ftag": int(g.ftag)}
    for c in ("close_h", "close_d", "close_a"):
        r[c] = None if c not in g or pd.isna(g[c]) else float(g[c])
    return r


def collect_league(df, conn, book, league: str, season: str | None, xg_weights, elo_params, refit_days=7,
                   start=None, end=None) -> list[dict]:
    """Walk-forward rows for one league: a whole season, or a date window [start, end)."""
    games = df[(df.league == league) & (df.status == "FT")].dropna(subset=["fthg", "ftag"])
    if season is not None:
        games = games[games.season == season]
    if start is not None:
        games = games[(games.date >= pd.Timestamp(start)) & (games.date < pd.Timestamp(end))]
    games = games.sort_values("date")
    rows = []
    for block, chunk in _blocks(games, refit_days):
        models = {xw: fit_league(df, league, block.date(), xg_weight=xw) for xw in xg_weights}
        if any(m is None for m in models.values()):
            continue
        pred = Predictor(df, block.date(), elo_book=book, elo_params=elo_params,
                         factors={f: True for f in ALL_FACTORS}, models={league: models[xg_weights[0]]})
        for g in chunk.itertuples():
            day = g.date.strftime("%Y-%m-%d")
            info = pred.components(league, g.home, g.away, day)
            if not (models[xg_weights[0]].knows(g.home) and models[xg_weights[0]].knows(g.away)):
                continue          # keep every variant on the same set of matches
            sea = season or g.season
            abs_eff = {k: match_absences(conn, g.id, g.home, g.away, league, league, sea, allow_current=False,
                                         fresh_k=k)[0] for k in FRESH_GRID} if conn is not None else {}
            abs_eff = {k: v for k, v in abs_eff.items() if v}
            rows.append({**_meta(pd.Series(g._asdict())), "info": {k: v for k, v in info.items() if k != "dc"},
                         "dc": {xw: m.expected_goals(g.home, g.away) for xw, m in models.items()},
                         "rho": {xw: m.goals.rho for xw, m in models.items()}, "abs": abs_eff})
    return rows


def collect_europe(df, book, comp: str, season: str, xg_weight: float, elo_params, refit_days=7) -> list[dict]:
    games = df[(df.league == comp) & (df.season == season) & (df.status == "FT")].dropna(
        subset=["fthg", "ftag"]).sort_values("date")
    rows = []
    for block, chunk in _blocks(games, refit_days):
        pred = Predictor(df, block.date(), elo_book=book, elo_params=elo_params, xg_weight=xg_weight,
                         factors={f: True for f in ALL_FACTORS})
        for g in chunk.itertuples():
            info = pred.components(comp, g.home, g.away, g.date.strftime("%Y-%m-%d"))
            if info["elo"] is None and info.get("joint") is None:
                continue
            rows.append({**_meta(pd.Series(g._asdict())), "info": {k: v for k, v in info.items() if k != "dc"},
                         "dc": {xg_weight: None}, "rho": {xg_weight: info["rho"]}, "abs": {}})
    return rows


# ---------------------------------------------------------------- evaluation

def evaluate(rows: list[dict], cfg: dict) -> list[dict]:
    """Apply one model configuration to collected rows -> scored prediction rows."""
    factors = {f: cfg.get(f, False) for f in ALL_FACTORS}
    out = []
    for r in rows:
        xw = cfg["xg_weight"]
        info = dict(r["info"], dc=r["dc"].get(xw), rho=r["rho"].get(xw, 0.0))
        if not cfg.get("use_dc_elo", True):
            info["dc_elo"] = None
        g = combine(info, factors=factors, blend_weight=cfg.get("blend_weight", 1.0),
                    rest_coef=cfg.get("rest_coef", 0.0), elo_params=cfg["elo_params"],
                    cross_shrink=cfg.get("cross_shrink"), joint_blend_weight=cfg.get("joint_blend_weight", 1.0),
                    absences=r["abs"].get(cfg.get("fresh_k", 0)) if factors["injuries"] else None)
        if g is None:
            continue
        m = dc.score_matrix(g["lam"], g["mu"], g["rho"])
        p = markets.result_probs(m)
        top = markets.correct_scores(m, 3)
        out.append({k: r[k] for k in ("id", "league", "date", "home", "away", "fthg", "ftag",
                                      "close_h", "close_d", "close_a")} |
                   {"p_home": p["home"], "p_draw": p["draw"], "p_away": p["away"],
                    "p_over25": markets.over_under(markets.total_goals_dist(m), 2.5)["over"],
                    "p_btts": markets.btts(m)["yes"], "xg_home": g["lam"], "xg_away": g["mu"],
                    "predicted_score": top[0]["score"], "top_scores": [t["score"] for t in top]})
    return out


def score(rows: list[dict]) -> dict:
    return tracking.score_predictions(rows) if rows else {"n": 0}


def ll(rows) -> float:
    return score(rows).get("log_loss_1x2") or float("inf")


def market_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["close_h"] and r["close_d"] and r["close_a"]:
            ph, pd_, pa = value.remove_vig([r["close_h"], r["close_d"], r["close_a"]])
            out.append(dict(r, p_home=ph, p_draw=pd_, p_away=pa, p_over25=None, p_btts=None,
                            predicted_score=None, top_scores=None))
    return out


def summary_metrics(m: dict) -> dict:
    keys = ("n", "accuracy_1x2", "exact_score_hit", "top3_score_hit", "brier_1x2", "log_loss_1x2")
    return {k: m.get(k) for k in keys}


# ---------------------------------------------------------------- tuning

def tune(rows: list[dict], base: dict, notes: list[str]) -> tuple[dict, dict]:
    """Choose xG weight, blend weight, rest coefficient and injuries on the tuning season."""
    log_ = {}
    cfg = dict(base)
    # xG weight (DC only)
    xws = sorted({xw for r in rows for xw in r["dc"]})
    xg_scores = {xw: ll(evaluate(rows, dict(cfg, xg_weight=xw))) for xw in xws}
    best_xw = min(xg_scores, key=xg_scores.get)
    log_["xg_weight_log_loss"] = {str(k): round(v, 5) for k, v in xg_scores.items()}
    has_xg = len(xws) > 1
    gain = xg_scores.get(0.0, xg_scores[best_xw]) - xg_scores[best_xw]
    cfg["xg"] = has_xg and best_xw > 0 and gain > KEEP_THRESHOLD
    cfg["xg_weight"] = best_xw if cfg["xg"] else 0.0
    log_["xg"] = {"available": has_xg, "gain": round(gain, 5), "kept": cfg["xg"], "weight": cfg["xg_weight"]}
    if not has_xg:
        notes.append("Backtest: no Understat xG in the data - xG factor not evaluated")
    # Elo blend
    elo_cov = np.mean([r["info"]["elo"] is not None for r in rows]) if rows else 0
    log_["elo"] = {"available": bool(elo_cov > 0.8), "coverage": round(float(elo_cov), 3)}
    if elo_cov > 0.8:
        grid = config.settings()["elo"]["blend_weight_grid"]
        bl = {w: ll(evaluate(rows, dict(cfg, elo=True, blend_weight=w))) for w in grid}
        best_w = min(bl, key=bl.get)
        gain = bl.get(1.0, ll(evaluate(rows, cfg))) - bl[best_w]
        keep = best_w < 1.0 and gain > KEEP_THRESHOLD
        log_["elo"].update({"blend_log_loss": {str(k): round(v, 5) for k, v in bl.items()},
                            "gain": round(gain, 5), "kept": keep, "blend_weight_dc": best_w})
        cfg["elo"], cfg["blend_weight"] = keep, (best_w if keep else 1.0)
    else:
        notes.append("Backtest: Elo ratings missing for most matches - Elo factor not evaluated")
    # rest days: fit the coefficient on the current best model's expected goals
    cur = evaluate(rows, cfg)
    s = config.settings()["rest"]
    by_id = {r["id"]: r for r in rows}
    feats = [elo_model.rest_feature(*by_id[r["id"]]["info"]["rest"], floor=s["floor_days"], cap=s["cap_days"])
             for r in cur]
    log_["rest"] = {"available": bool(np.mean([f != 0 for f in feats]) > 0.3) if feats else False}
    if log_["rest"]["available"]:
        gamma = elo_model.fit_offset_coef([r["xg_home"] for r in cur], [r["xg_away"] for r in cur], feats,
                                          [r["fthg"] for r in cur], [r["ftag"] for r in cur])
        with_rest = ll(evaluate(rows, dict(cfg, rest=True, rest_coef=gamma)))
        gain = ll(cur) - with_rest
        keep = gain > KEEP_THRESHOLD
        log_["rest"].update({"coef": round(gamma, 5), "gain": round(gain, 5), "kept": keep})
        cfg["rest"], cfg["rest_coef"] = keep, (gamma if keep else 0.0)
    # injuries (only rows with absence data). Choose fresh_k on the first half of the season, judge the
    # gain on the second half, so the decision is not made on the data that picked the parameter.
    sub = sorted([r for r in rows if r["abs"]], key=lambda r: r["date"])
    log_["injuries"] = {"available": len(sub) >= 200, "matches_with_data": len(sub)}
    if len(sub) >= 200:
        first, second = sub[: len(sub) // 2], sub[len(sub) // 2:]
        by_k = {k: ll(evaluate(first, dict(cfg, injuries=True, fresh_k=k))) for k in FRESH_GRID}
        k = min(by_k, key=by_k.get)
        gain = ll(evaluate(second, cfg)) - ll(evaluate(second, dict(cfg, injuries=True, fresh_k=k)))
        keep = gain > KEEP_THRESHOLD
        log_["injuries"].update({"fresh_k_log_loss_first_half": {str(a): round(b, 5) for a, b in by_k.items()},
                                 "fresh_k": k, "gain_second_half": round(gain, 5), "kept": keep})
        cfg["injuries"], cfg["fresh_k"] = keep, k
    else:
        notes.append("Backtest: fewer than 100 matches with injury data - injuries factor not evaluated "
                     "(needs API-Football history)")
    log_["referee_penalties"] = {"available": False,
                                 "reason": "no historical penalty-by-referee data yet (collected live from now on)"}
    return cfg, log_


# ---------------------------------------------------------------- report

FACTOR_LABELS = {"xg": "xG (Understat)", "elo": "Elo blend (own ratings)", "rest": "Rest days", "injuries": "Injuries"}


def ablation(rows: list[dict], selected: dict, base: dict, tuned: dict) -> list[dict]:
    b = score(evaluate(rows, base))
    variants = [("Base: Dixon-Coles on goals", base)]
    for f in ("xg", "elo", "rest", "injuries"):
        if f == "xg" and tuned["xg_weight"]:
            variants.append(("+ " + FACTOR_LABELS[f], dict(base, xg=True, xg_weight=tuned["xg_weight"])))
        elif f == "elo" and tuned.get("blend_weight", 1.0) < 1.0:
            variants.append(("+ " + FACTOR_LABELS[f], dict(base, elo=True, blend_weight=tuned["blend_weight"])))
        elif f == "rest" and tuned.get("rest_coef"):
            variants.append(("+ " + FACTOR_LABELS[f], dict(base, rest=True, rest_coef=tuned["rest_coef"])))
        elif f == "injuries" and any(r["abs"] for r in rows):
            variants.append(("+ " + FACTOR_LABELS[f], dict(base, injuries=True, fresh_k=tuned.get("fresh_k", 0))))
    variants.append(("Selected model", selected))
    for f in ("xg", "elo", "rest", "injuries"):
        if selected.get(f):
            drop = dict(selected, **{f: False})
            if f == "xg":
                drop["xg_weight"] = 0.0
            variants.append((f"Selected minus {FACTOR_LABELS[f]}", drop))
    out = []
    for name, cfg in variants:
        m = score(evaluate(rows, cfg))
        out.append({"variant": name, **summary_metrics(m),
                    "delta_log_loss_vs_base": round(m["log_loss_1x2"] - b["log_loss_1x2"], 5) if m.get("n") else None,
                    "delta_brier_vs_base": round(m["brier_1x2"] - b["brier_1x2"], 5) if m.get("n") else None})
    return out


def plain_summary(rep: dict) -> list[str]:
    t, mk = rep["test"]["selected"], rep["test"].get("market")
    if not t.get("n"):
        return ["Not enough historical data to backtest yet."]
    same = rep["test"].get("selected_same_games_as_market") or t
    lines = [f"Across {t['n']:,} league matches in {rep['test_season_label']}, the model's favourite won "
             f"{t['accuracy_1x2']:.0%} of the time"
             + (f" (bookmakers' closing-odds favourite: {mk['accuracy_1x2']:.0%})." if mk and mk.get('n') else ".")]
    lines.append(f"Its single most likely scoreline was exactly right in {t['exact_score_hit']:.0%} of matches; "
                 f"the real score was in its top 3 in {t['top3_score_hit']:.0%}.")
    if mk and mk.get("n"):
        diff = same["log_loss_1x2"] - mk["log_loss_1x2"]
        verdict = "slightly better than" if diff < -0.002 else "about level with" if abs(diff) <= 0.002 else \
            "a little worse than" if diff < 0.02 else "clearly worse than"
        lines.append(f"On probability quality (log loss {same['log_loss_1x2']:.3f} vs {mk['log_loss_1x2']:.3f}, "
                     "lower is better) it is "
                     f"{verdict} the closing bookmaker prices, which are the toughest public benchmark.")
    kept = [FACTOR_LABELS[f] for f in ("xg", "elo", "rest", "injuries") if rep["selected"].get(f)]
    dropped = [FACTOR_LABELS[f] for f, v in rep["tuning"].items() if f in FACTOR_LABELS and v.get("available")
               and not v.get("kept")]
    missing = [FACTOR_LABELS.get(f, f) for f, v in rep["tuning"].items() if isinstance(v, dict) and not v.get("available")
               and f in FACTOR_LABELS]
    if kept:
        lines.append("Factors that improved accuracy and are switched on: " + ", ".join(kept) + ".")
    if dropped:
        lines.append("Tested but dropped (no measurable gain): " + ", ".join(dropped) + ".")
    if missing:
        lines.append("Not testable yet (data unavailable): " + ", ".join(missing) + ".")
    ucl = rep.get("ucl")
    if ucl and ucl.get("selected", {}).get("n"):
        u = ucl["selected"]
        lines.append(f"Champions League ({ucl['season_label']}, {u['n']} matches): favourite won {u['accuracy_1x2']:.0%}, "
                     f"exact score {u['exact_score_hit']:.0%}, top-3 score {u['top3_score_hit']:.0%}.")
    return lines


def season_label(s: str) -> str:
    return f"20{s[:2]}-{s[2:]}"


def run(conn, season: str | None = None, notes: list[str] | None = None, refit_days: int = 7) -> dict:
    from .models import team_elo
    notes = notes if notes is not None else []
    df = load_matches(conn)
    if season is None:
        season = config.previous_seasons(config.season_code(pd.Timestamp.now()), 1)[0]
    tune_season = config.previous_seasons(season, 1)[0]
    book = team_elo.build(df)          # point-in-time: a rating on day d uses only matches before d
    book = book if book else None
    s = config.settings()
    has_xg = "xg_home" in df and df.xg_home.notna().any()
    xws = s["model"]["xg_weight_grid"] if has_xg else [0.0]
    default_elo = s["elo"]["default_params"]

    def elo_params_before(sea):
        p = fit_elo_params(df, book, before=f"20{sea[:2]}-07-01") if book else None
        return p or default_elo

    base = {"xg_weight": 0.0, "blend_weight": 1.0, "rest_coef": 0.0, **{f: False for f in ALL_FACTORS}}
    ep_tune, ep_test = elo_params_before(tune_season), elo_params_before(season)
    tune_rows, test_rows = [], []
    for lg in config.leagues():
        tune_rows += collect_league(df, conn, book, lg, tune_season, xws, ep_tune, refit_days)
    selected, tuning = tune(tune_rows, dict(base, elo_params=ep_tune), notes)
    test_xws = sorted({0.0, selected["xg_weight"]})
    for lg in config.leagues():
        test_rows += collect_league(df, conn, book, lg, season, test_xws, ep_test, refit_days)
    sel_test = dict(selected, elo_params=ep_test)
    base_test = dict(base, elo_params=ep_test)
    sel_rows = evaluate(test_rows, sel_test)
    mrows = market_rows(sel_rows)
    sel_m = score(sel_rows)
    rep = {
        "season": season, "test_season_label": season_label(season), "tune_season": tune_season,
        "tune_season_label": season_label(tune_season), "keep_threshold": KEEP_THRESHOLD,
        "selected": {k: v for k, v in selected.items() if k != "elo_params"},
        "elo_params": {"tune": ep_tune, "test": ep_test}, "tuning": tuning,
        "test": {
            "selected": summary_metrics(sel_m) | {"brier_over25": sel_m.get("brier_over25")},
            "market": summary_metrics(score(mrows)) if mrows else None,
            "selected_same_games_as_market": summary_metrics(score([r for r in sel_rows if r["close_h"]]))
            if mrows else None,
            "base": summary_metrics(score(evaluate(test_rows, base_test))),
            "ablation": ablation(test_rows, sel_test, base_test, selected),
            "calibration_1x2": sel_m.get("calibration_1x2"),
            "calibration": sel_m.get("calibration"),
            "by_league": {lg: {"model": summary_metrics(score([r for r in sel_rows if r["league"] == lg])),
                               "market": summary_metrics(score([r for r in mrows if r["league"] == lg]))
                               if mrows else None}
                          for lg in config.leagues()},
            "biggest_misses": sel_m.get("biggest_misses"),
        },
        "matches": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
                     if k not in ("close_h", "close_d", "close_a", "p_over25", "p_btts")} for r in sel_rows],
    }
    # Champions League
    counts = df[(df.league == "UCL") & (df.status == "FT")].season.value_counts()
    # evaluate a season with a real sample: a just-started live season (a handful of games) says nothing
    ucl_seasons = sorted(s for s, n in counts.items() if n >= MIN_EUROPE_SEASON) or sorted(counts.index)
    if ucl_seasons:
        us = season if season in ucl_seasons else ucl_seasons[-1]
        ep = elo_params_before(us)
        urows = collect_europe(df, book, "UCL", us, selected["xg_weight"], ep, refit_days)
        if urows:
            ucfg = dict(selected, elo_params=ep)
            prior = [x for x in ucl_seasons if x < us]
            shrink, jlog, wj = {}, {}, 1.0
            if prior:     # tune the joint/Elo blend and cross-league confidence on the previous European season
                prow = collect_europe(df, book, "UCL", prior[-1], selected["xg_weight"],
                                      elo_params_before(prior[-1]), refit_days)
                pcfg = dict(selected, elo_params=elo_params_before(prior[-1]))
                if book and np.mean([r["info"]["elo"] is not None for r in prow]) > 0.8:
                    jl = {w: ll(evaluate(prow, dict(pcfg, joint_blend_weight=w)))
                          for w in config.settings()["elo"]["blend_weight_grid"]}
                    best = min(jl, key=jl.get)
                    keep = jl[1.0] - jl[best] > KEEP_THRESHOLD if 1.0 in jl else False
                    wj = best if keep else 1.0
                    jlog = {"season": prior[-1], "log_loss": {str(k): round(v, 5) for k, v in jl.items()},
                            "gain": round(jl.get(1.0, jl[best]) - jl[best], 5), "kept": bool(keep),
                            "blend_weight_joint": wj}
                shrink = tune_shrink(prow, dict(pcfg, joint_blend_weight=wj))
            ucfg["cross_shrink"], ucfg["joint_blend_weight"] = shrink, wj
            raw = summary_metrics(score(evaluate(urows, dict(ucfg, cross_shrink={}))))
            rep["ucl"] = {
                "season": us, "season_label": season_label(us),
                "method": "Joint leagues + Europe model" + (" blended with Elo" if wj < 1.0 else ""),
                "joint_elo_blend": jlog, "cross_league_shrink": shrink, "uncalibrated": raw,
                "joint_only": summary_metrics(score(evaluate(urows, dict(ucfg, joint_blend_weight=1.0)))),
                "by_category": {c: summary_metrics(score(evaluate([r for r in urows if r["info"].get("category") == c], ucfg)))
                                for c in ("top", "mixed", "outside")},
                "selected": summary_metrics(score(evaluate(urows, ucfg))),
                "elo_only": summary_metrics(score(evaluate([dict(r, info=dict(r["info"], joint=None)) for r in urows
                                                            if r["info"]["elo"]], dict(ucfg, use_dc_elo=False))))
                if book else None,
                "matches": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
                             if k not in ("close_h", "close_d", "close_a", "p_over25", "p_btts")}
                            for r in evaluate(urows, ucfg)],
            }
    else:
        notes.append("Backtest: no Champions League results in the data - UCL not evaluated")
    rep["bankers"] = bankers(sel_rows)
    rep["notes"] = notes
    rep["summary"] = plain_summary(rep)
    rep["recommended_settings"] = {
        "factors": {f: bool(selected.get(f)) for f in ALL_FACTORS},
        "model.xg_weight": selected["xg_weight"], "elo.blend_weight_dc": selected.get("blend_weight", 1.0),
        "rest.coef": selected.get("rest_coef", 0.0), "elo.default_params": ep_test,
        "injuries.fresh_absence_matches": selected.get("fresh_k", 0),
        "model.cross_league_shrink": (rep.get("ucl") or {}).get("cross_league_shrink"),
        "elo.blend_weight_joint": ((rep.get("ucl") or {}).get("joint_elo_blend") or {}).get("blend_weight_joint", 1.0),
    }
    return rep


def to_markdown(rep: dict) -> str:
    out = ["## Summary", ""] + [f"- {x}" for x in rep["summary"]] + ["", "## Ablation (test season)", "",
           "| Variant | n | Result hit | Exact score | Top-3 score | Brier | Log loss | Δ log loss vs base |",
           "|---|---|---|---|---|---|---|---|"]
    for a in rep["test"]["ablation"]:
        out.append(f"| {a['variant']} | {a['n']} | {a['accuracy_1x2']} | {a['exact_score_hit']} | {a['top3_score_hit']} "
                   f"| {a['brier_1x2']} | {a['log_loss_1x2']} | {a['delta_log_loss_vs_base']} |")
    mk = rep["test"].get("market")
    if mk:
        out.append(f"| Closing bookmaker odds | {mk['n']} | {mk['accuracy_1x2']} | – | – | {mk['brier_1x2']} "
                   f"| {mk['log_loss_1x2']} | |")
    out += ["", "## By league (selected model / market)", "", "| League | n | Hit | Exact | Top-3 | Brier | Log loss | Market log loss |",
            "|---|---|---|---|---|---|---|---|"]
    for lg, v in rep["test"]["by_league"].items():
        m, k = v["model"], v["market"]
        if not m.get("n"):
            continue
        out.append(f"| {lg} | {m['n']} | {m['accuracy_1x2']} | {m['exact_score_hit']} | {m['top3_score_hit']} | "
                   f"{m['brier_1x2']} | {m['log_loss_1x2']} | {k['log_loss_1x2'] if k and k.get('n') else 'n/a'} |")
    if rep.get("ucl"):
        u = rep["ucl"]
        out += ["", f"## Champions League {u['season_label']}", "", "| Variant | n | Hit | Exact | Top-3 | Brier | Log loss |",
                "|---|---|---|---|---|---|---|"]
        for name, m in ((u["method"] + " (calibrated)", u["selected"]), ("Uncalibrated", u.get("uncalibrated")),
                        ("Elo only", u["elo_only"])):
            if not m:
                continue
            out.append(f"| {name} | {m['n']} | {m['accuracy_1x2']} | {m['exact_score_hit']} | {m['top3_score_hit']} | "
                       f"{m['brier_1x2']} | {m['log_loss_1x2']} |")
    out += ["", "## Tuning (" + rep["tune_season_label"] + ")", "", "```", str(rep["tuning"]), "```",
            "", "Recommended settings: `" + str(rep["recommended_settings"]) + "`"]
    if rep.get("notes"):
        out += ["", "Notes:"] + [f"- {n}" for n in rep["notes"]]
    return "\n".join(out)


# ---------------------------------------------------------------- daily auto-tune

def current_config(conn) -> dict:
    """The model configuration production uses: settings.json, overridden by the last adopted auto-tune."""
    from . import db
    s = config.settings()
    cfg = {"xg_weight": s["model"]["xg_weight"] if s["factors"].get("xg") else 0.0,
           "blend_weight": s["elo"]["blend_weight_dc"], "joint_blend_weight": s["elo"].get("blend_weight_joint", 1.0),
           "rest_coef": s["rest"]["coef"],
           "fresh_k": s["injuries"].get("fresh_absence_matches") or 0,
           **{f: bool(s["factors"].get(f)) for f in ALL_FACTORS}}
    tuned = db.get_param(conn, "tuned")
    if tuned:
        cfg.update(tuned["config"])
    return cfg


def rolling_tune(conn, notes: list[str], days: int = 365, refit_days: int = 14, today=None) -> dict:
    """Re-tune xG weight, Elo blend, rest coefficient and injuries on the last `days` of league matches
    (walk-forward, refit every `refit_days`), and adopt the result only if it beats the configuration
    in use on that same window by more than KEEP_THRESHOLD. Ratings themselves are refitted on every run."""
    from . import db
    from .ingest.http_cache import iso, now_utc
    from .models import team_elo
    today = pd.Timestamp(today or now_utc().date())
    start = today - pd.Timedelta(days=days)
    df = load_matches(conn)
    book = team_elo.build(df)
    book = book if book else None
    s = config.settings()
    has_xg = "xg_home" in df and df[df.date >= start].xg_home.notna().any()
    xws = sorted(set(s["model"]["xg_weight_grid"]) if has_xg else {0.0})
    ep = (fit_elo_params(df, book, before=start.strftime("%Y-%m-%d")) if book else None) or s["elo"]["default_params"]
    rows = []
    for lg in config.leagues():
        rows += collect_league(df, conn, book, lg, None, xws, ep, refit_days, start=start, end=today)
    if len(rows) < 300:
        notes.append(f"Auto-tune skipped: only {len(rows)} recent matches")
        return {"adopted": False, "n": len(rows)}
    base = {"xg_weight": 0.0, "blend_weight": 1.0, "rest_coef": 0.0, "elo_params": ep,
            **{f: False for f in ALL_FACTORS}}
    new_cfg, log_ = tune(rows, base, [])
    cur = dict(current_config(conn), elo_params=ep)
    if cur["xg_weight"] not in xws:
        cur["xg_weight"] = min(xws, key=lambda x: abs(x - cur["xg_weight"]))
    ll_cur, ll_new = ll(evaluate(rows, cur)), ll(evaluate(rows, new_cfg))
    adopt = ll_new < ll_cur - KEEP_THRESHOLD
    keep = {k: new_cfg.get(k) for k in ("xg_weight", "blend_weight", "rest_coef", "fresh_k", *ALL_FACTORS)}
    entry = {"date": today.strftime("%Y-%m-%d"), "n": len(rows), "window_days": days,
             "log_loss_current": round(ll_cur, 5), "log_loss_candidate": round(ll_new, 5), "adopted": adopt,
             "candidate": keep, "tuning": log_}
    history_ = (db.get_param(conn, "tuning_log") or [])[-59:] + [entry]
    db.set_param(conn, "tuning_log", history_, iso(now_utc()))
    if adopt:
        db.set_param(conn, "tuned", {"config": keep, "elo_params": ep, "date": entry["date"],
                                     "log_loss": round(ll_new, 5)}, iso(now_utc()))
        notes.append(f"Auto-tune adopted new settings (log loss {ll_cur:.4f} -> {ll_new:.4f} on {len(rows)} "
                     f"recent matches): {keep}")
    conn.commit()
    return entry


SHRINK_GRID = (-0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5)


def tune_shrink(rows: list[dict], cfg: dict, min_n: int = 20) -> dict:
    """Per matchup category (top-5 v top-5, top-5 v outside, outside v outside), the cross-league shrink
    that minimises log loss. Categories with fewer than min_n games stay at 0 (no adjustment)."""
    out = {}
    for c in ("top", "mixed", "outside"):
        sub = [r for r in rows if r["info"].get("category") == c]
        if len(sub) < min_n:
            out[c] = 0.0
            continue
        lls = {s: ll(evaluate(sub, dict(cfg, cross_shrink={c: s}))) for s in SHRINK_GRID}
        out[c] = min(lls, key=lls.get)
    return out


def bankers(rows: list[dict], thresholds=(0.6, 0.65, 0.7, 0.75, 0.8)) -> dict:
    """Hit rate when only predicting matches whose favourite clears a probability threshold."""
    out = {}
    for th in thresholds:
        sub = [r for r in rows if max(r["p_home"], r["p_draw"], r["p_away"]) >= th]
        hits = [max(("home", "draw", "away"), key=lambda k: r[f"p_{k}"]) == tracking.outcome_1x2(r["fthg"], r["ftag"])
                for r in sub]
        out[str(th)] = {"n": len(sub), "share": round(len(sub) / len(rows), 4) if rows else 0,
                        "hit": round(float(np.mean(hits)), 4) if hits else None}
    return out
