"""Value-bet helpers (PAUSED in Phase A: not called by the pipeline, kept with their tests).

Re-enable by setting odds_api.fetch_odds = true and wiring these back into pipeline.run().
"""
from __future__ import annotations

from datetime import timedelta

from . import config, value
from .ingest import odds_api
from .ingest.http_cache import iso, now_utc


def sync_odds(conn, notes: list[str], horizon: timedelta) -> int:
    now = now_utc()
    stored = 0
    for key, lg in config.competitions().items():
        upcoming = conn.execute(
            "SELECT id, odds_api_id FROM matches WHERE league=? AND status='SCHEDULED' AND kickoff_utc > ? "
            "AND kickoff_utc < ? AND odds_api_id IS NOT NULL", (key, iso(now), iso(now + horizon))).fetchall()
        if not upcoming:
            continue
        by_event = {r["odds_api_id"]: r["id"] for r in upcoming}
        try:
            evs = odds_api.odds(conn, lg["odds_api_sport"])
        except odds_api.OddsApiUnavailable as e:
            notes.append(f"Odds API odds unavailable for {lg['name']}: {e}")
            continue
        fetched = iso(now)
        for e in evs:
            mid = by_event.get(e["id"])
            if not mid:
                continue
            for row in odds_api.parse_odds(e, e["home_team"], e["away_team"]):
                last = conn.execute(
                    "SELECT price FROM odds_snapshots WHERE match_id=? AND bookmaker=? AND market=? AND selection=? "
                    "AND COALESCE(line,-1)=COALESCE(?,-1) ORDER BY fetched_at DESC LIMIT 1",
                    (mid, row["bookmaker"], row["market"], row["selection"], row["line"])).fetchone()
                if last and last["price"] == row["price"]:
                    continue   # store only price changes; the latest row is still the current price
                conn.execute("INSERT INTO odds_snapshots (match_id, fetched_at, bookmaker, market, selection, line, price) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (mid, fetched, row["bookmaker"], row["market"], row["selection"], row["line"], row["price"]))
                stored += 1
    return stored


def latest_odds(conn, match_id: str, before: str | None = None) -> dict:
    q = ("SELECT market, selection, line, price, fetched_at FROM odds_snapshots WHERE match_id=?"
         + (" AND fetched_at <= ?" if before else "") + " ORDER BY fetched_at")
    out: dict = {}
    for r in conn.execute(q, (match_id, before) if before else (match_id,)):
        if r["market"] == "totals":
            out.setdefault("totals", {}).setdefault(str(r["line"]), {})[r["selection"]] = r["price"]
        else:
            out.setdefault(r["market"], {})[r["selection"]] = r["price"]
        out["updated"] = r["fetched_at"]
    return out


def evaluate_value(payload: dict, odds: dict) -> list[dict]:
    cfg = config.settings()["value"]
    mk = payload["markets"]
    out = []

    def add(market, sel, line, p, price, fair):
        a = value.assess(p, price, fair, cfg)
        out.append({"market": market, "selection": sel, "line": line, "model_prob": round(p, 4),
                    "fair_odds": round(1 / p, 2) if p > 0 else None, "price": price,
                    "market_prob": round(fair, 4), "edge": round(a["edge"], 4), "ev": round(a["ev"], 4),
                    "stake": round(a["stake"], 4), "is_value": a["is_value"], "suspect": a["suspect"]})

    h2h = odds.get("h2h", {})
    if all(k in h2h for k in ("home", "draw", "away")):
        prices = [h2h["home"], h2h["draw"], h2h["away"]]
        for sel, price, fair in zip(("home", "draw", "away"), prices, value.remove_vig(prices)):
            add("h2h", sel, None, mk["1x2"][sel], price, fair)
    for line, sides in odds.get("totals", {}).items():
        if "over" in sides and "under" in sides and line in mk["totals"]:
            prices = [sides["over"], sides["under"]]
            for sel, price, fair in zip(("over", "under"), prices, value.remove_vig(prices)):
                add("totals", sel, float(line), mk["totals"][line][sel], price, fair)
    bt = odds.get("btts", {})
    if "yes" in bt and "no" in bt:
        prices = [bt["yes"], bt["no"]]
        for sel, price, fair in zip(("yes", "no"), prices, value.remove_vig(prices)):
            add("btts", sel, None, mk["btts"][sel], price, fair)
    return out


def log_bets(conn, match_id: str, assessments: list[dict], taken_at: str) -> None:
    for a in assessments:
        if a["is_value"]:
            conn.execute(
                "INSERT OR IGNORE INTO bets (match_id, market, selection, line, model_prob, market_prob, odds_taken, "
                "edge, ev, stake, taken_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (match_id, a["market"], a["selection"], a["line"], a["model_prob"], a["market_prob"], a["price"],
                 a["edge"], a["ev"], a["stake"], taken_at))
