"""Write the JSON the static site reads: site/data/predictions.json and site/data/accuracy.json."""
from __future__ import annotations

import json
from datetime import timedelta

from . import MODEL_VERSION, __version__, config, tracking
from .ingest.http_cache import iso, last_remaining, now_utc


def outcome(hg, ag) -> str:
    return "home" if hg > ag else "away" if hg < ag else "draw"


def card(r, pred: dict | None) -> dict:
    comps = config.competitions()
    c = comps.get(r["league"], {})
    m = {"id": r["id"], "comp": r["league"], "badge": c.get("badge", r["league"]), "comp_name": c.get("name"),
         "date": r["date"], "kickoff_utc": r["kickoff_utc"], "home": r["home"], "away": r["away"],
         "status": r["status"], "referee": r["referee"],
         "result": {"home": r["fthg"], "away": r["ftag"]} if r["status"] == "FT" and r["fthg"] is not None else None,
         "prediction": None}
    if pred:
        x = pred["markets"]["1x2"]
        m["prediction"] = {
            "p_home": x["home"], "p_draw": x["draw"], "p_away": x["away"],
            "predicted_score": pred["predicted_score"],
            "top_scores": [{"score": t["score"], "prob": t["prob"]} for t in
                           (pred.get("top_scores") or pred["markets"]["correct_score"])[:3]],
            "xg": pred["xg"], "source": pred.get("source"), "factors": pred.get("factors", {}),
            "absences": pred.get("absences", []), "lineups_confirmed": pred.get("lineups_confirmed", False),
            "thin_data": pred.get("thin_data", []),
            "predicted_at": r["predicted_at"],
        }
        m["referee"] = m["referee"] or pred.get("referee")
        if m["result"]:
            act = f"{m['result']['home']}-{m['result']['away']}"
            fav = max(("home", "draw", "away"), key=lambda k: x[k])
            m["hit"] = {"result": fav == outcome(m["result"]["home"], m["result"]["away"]),
                        "exact": pred["predicted_score"] == act,
                        "top3": act in [t["score"] for t in m["prediction"]["top_scores"]]}
    return m


def _rows(conn, lo: str, hi: str):
    return conn.execute(
        "SELECT m.*, p.payload, p.created_at AS predicted_at FROM matches m LEFT JOIN predictions p ON p.match_id = m.id "
        "WHERE m.date >= ? AND m.date <= ? ORDER BY COALESCE(m.kickoff_utc, m.date)", (lo, hi)).fetchall()


def build(conn, notes: list[str] | None = None) -> dict:
    data_dir = config.SITE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    disp = config.settings()["display"]
    lo = (now - timedelta(days=disp["days_back"])).strftime("%Y-%m-%d")
    hi = (now + timedelta(days=disp["days_ahead"])).strftime("%Y-%m-%d")
    matches = []
    for r in _rows(conn, lo, hi):
        if r["league"] not in config.competitions() or not config.is_displayed(r["league"], r["home"], r["away"]):
            continue
        matches.append(card(r, json.loads(r["payload"]) if r["payload"] else None))
    meta = {"generated_at": iso(now), "version": __version__, "model_version": MODEL_VERSION, "notes": notes or [],
            "factors": config.settings()["factors"], "odds_api_credits_remaining": last_remaining(conn, "odds_api"),
            "competitions": {k: {"name": v["name"], "badge": v["badge"]} for k, v in config.competitions().items()},
            "tracked_teams": config.settings()["tracked_teams"]}
    (data_dir / "predictions.json").write_text(
        json.dumps({**meta, "matches": matches}, separators=(",", ":"), default=str))

    # live accuracy: every finished match we predicted before kickoff
    rows = tracking.tracked_rows(conn)
    live = tracking.score_predictions(rows)
    by_comp = {}
    for comp in sorted({r["league"] for r in rows}):
        sub = tracking.score_predictions([r for r in rows if r["league"] == comp])
        by_comp[comp] = {k: sub.get(k) for k in ("n", "accuracy_1x2", "exact_score_hit", "top3_score_hit",
                                                 "brier_1x2", "log_loss_1x2")}
    table = []
    for r in sorted(rows, key=lambda r: r["date"], reverse=True):
        act = f"{r['fthg']}-{r['ftag']}"
        fav = max(("home", "draw", "away"), key=lambda k: r[f"p_{k}"])
        table.append({"id": r["id"], "comp": r["league"], "date": str(r["date"])[:10], "home": r["home"],
                      "away": r["away"], "p_home": r["p_home"], "p_draw": r["p_draw"], "p_away": r["p_away"],
                      "predicted_score": r["predicted_score"], "top_scores": r.get("top_scores"), "score": act,
                      "result_hit": fav == outcome(r["fthg"], r["ftag"]), "exact_hit": r["predicted_score"] == act,
                      "top3_hit": act in (r.get("top_scores") or [])})
    acc = {"generated_at": iso(now), "live": {k: v for k, v in live.items() if k != "biggest_misses"},
           "live_by_comp": by_comp, "live_matches": table}
    (data_dir / "accuracy.json").write_text(json.dumps(acc, separators=(",", ":"), default=str))
    return {"matches": len(matches), "tracked": len(rows)}
