"""Write the JSON the static site reads: site/data/{predictions,performance}.json."""
from __future__ import annotations

import json
from datetime import timedelta

from . import MODEL_VERSION, __version__, config, tracking
from .ingest.http_cache import iso, last_remaining, now_utc

RECENT_DAYS = 14


def _matches(conn) -> list[dict]:
    now = now_utc()
    lo = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    hi = (now + timedelta(days=11)).strftime("%Y-%m-%d")
    leagues = config.leagues()
    rows = conn.execute(
        "SELECT m.*, p.payload, p.created_at AS predicted_at FROM matches m "
        "LEFT JOIN predictions p ON p.match_id = m.id WHERE m.date >= ? AND m.date <= ? "
        "AND (p.payload IS NOT NULL OR m.kickoff_utc IS NOT NULL) ORDER BY COALESCE(m.kickoff_utc, m.date)",
        (lo, hi)).fetchall()
    out = []
    for r in rows:
        if r["league"] not in leagues:
            continue
        pred = json.loads(r["payload"]) if r["payload"] else None
        out.append({
            "id": r["id"], "league": r["league"], "league_name": leagues[r["league"]]["name"],
            "date": r["date"], "kickoff_utc": r["kickoff_utc"], "home": r["home"], "away": r["away"],
            "status": r["status"],
            "result": {"home": r["fthg"], "away": r["ftag"]} if r["status"] == "FT" else None,
            "actual_stats": {
                "corners": [r["hc"], r["ac"]], "sot": [r["hst"], r["ast"]],
                "cards": [None if r["hy"] is None else r["hy"] + (r["hr"] or 0),
                          None if r["ay"] is None else r["ay"] + (r["ar"] or 0)],
            } if r["status"] == "FT" and r["hc"] is not None else None,
            "predicted_at": r["predicted_at"],
            "prediction": pred,
        })
    return out


def build(conn, notes: list[str] | None = None) -> dict:
    data_dir = config.SITE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    matches = _matches(conn)
    value_bets = []
    for m in matches:
        p = m["prediction"]
        if not p or m["status"] != "SCHEDULED" or not m["kickoff_utc"] or m["kickoff_utc"] < iso(now):
            continue
        for v in p.get("value", []):
            if v["is_value"]:
                value_bets.append({"match_id": m["id"], "league": m["league"], "home": m["home"], "away": m["away"],
                                   "kickoff_utc": m["kickoff_utc"], **v})
    value_bets.sort(key=lambda v: v["edge"], reverse=True)
    meta = {
        "generated_at": iso(now), "version": __version__, "model_version": MODEL_VERSION,
        "notes": notes or [], "value_rules": config.settings()["value"],
        "odds_api_credits_remaining": last_remaining(conn, "odds_api"),
        "leagues": {k: v["name"] for k, v in config.leagues().items()},
    }
    predictions = {**meta, "matches": matches, "value_bets": value_bets}
    (data_dir / "predictions.json").write_text(json.dumps(predictions, separators=(",", ":"), default=str))

    perf = tracking.performance(conn)
    perf["bets_log"] = [dict(b) for b in conn.execute(
        "SELECT b.*, m.home, m.away, m.kickoff_utc FROM bets b JOIN matches m ON m.id=b.match_id "
        "ORDER BY b.taken_at DESC LIMIT 200")]
    perf["generated_at"] = iso(now)
    (data_dir / "performance.json").write_text(json.dumps(perf, separators=(",", ":"), default=str))
    return {"matches": len(matches), "value_bets": len(value_bets)}
