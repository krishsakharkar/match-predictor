"""Prediction logging, bet settlement and performance metrics."""
from __future__ import annotations

import json
import math

import numpy as np

EPS = 1e-12


def outcome_1x2(hg: int, ag: int) -> str:
    return "home" if hg > ag else "away" if hg < ag else "draw"


# ---------- scoring rules ----------

def brier_multiclass(probs: dict, actual: str) -> float:
    return sum((p - (1.0 if k == actual else 0.0)) ** 2 for k, p in probs.items())


def brier_binary(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss(p_actual: float) -> float:
    return -math.log(max(p_actual, EPS))


def calibration_buckets(pairs: list[tuple[float, int]], n_bins: int = 10) -> list[dict]:
    """pairs of (predicted probability, 0/1 outcome) -> per-bucket mean prediction vs frequency."""
    out = []
    if not pairs:
        return out
    p = np.array([x[0] for x in pairs])
    y = np.array([x[1] for x in pairs])
    edges = np.linspace(0, 1, n_bins + 1)
    for i in range(n_bins):
        sel = (p >= edges[i]) & ((p < edges[i + 1]) if i < n_bins - 1 else (p <= 1))
        if sel.sum() == 0:
            continue
        out.append({"bin": f"{edges[i]:.1f}-{edges[i + 1]:.1f}", "n": int(sel.sum()),
                    "predicted": round(float(p[sel].mean()), 4), "observed": round(float(y[sel].mean()), 4)})
    return out


def score_predictions(rows: list[dict]) -> dict:
    """rows: dicts with p_home/p_draw/p_away/p_over25/p_btts, fthg/ftag, plus id/home/away/date.

    Returns aggregate metrics, calibration and the biggest misses.
    """
    if not rows:
        return {"n": 0}
    b1, l1, bo, lo, bb, lb = [], [], [], [], [], []
    pairs = []
    misses = []
    for r in rows:
        probs = {"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"]}
        act = outcome_1x2(r["fthg"], r["ftag"])
        b1.append(brier_multiclass(probs, act))
        ll = log_loss(probs[act])
        l1.append(ll)
        for k, p in probs.items():
            pairs.append((p, int(k == act)))
        over = int(r["fthg"] + r["ftag"] > 2.5)
        bt = int(r["fthg"] > 0 and r["ftag"] > 0)
        if r.get("p_over25") is not None:
            bo.append(brier_binary(r["p_over25"], over))
            lo.append(log_loss(r["p_over25"] if over else 1 - r["p_over25"]))
            pairs.append((r["p_over25"], over))
        if r.get("p_btts") is not None:
            bb.append(brier_binary(r["p_btts"], bt))
            lb.append(log_loss(r["p_btts"] if bt else 1 - r["p_btts"]))
            pairs.append((r["p_btts"], bt))
        misses.append({
            "id": r.get("id"), "date": str(r.get("date"))[:10], "home": r.get("home"), "away": r.get("away"),
            "score": f"{r['fthg']}-{r['ftag']}", "predicted_score": r.get("predicted_score"),
            "p_home": round(r["p_home"], 3), "p_draw": round(r["p_draw"], 3), "p_away": round(r["p_away"], 3),
            "p_actual": round(probs[act], 3), "log_loss": round(ll, 3),
        })
    misses.sort(key=lambda x: x["log_loss"], reverse=True)
    scored = [r for r in rows if r.get("predicted_score")]
    exact = [r["predicted_score"] == f"{r['fthg']}-{r['ftag']}" for r in scored]
    top3 = [f"{r['fthg']}-{r['ftag']}" in (r.get("top_scores") or [r["predicted_score"]])[:3] for r in scored]

    def mean(x):
        return round(float(np.mean(x)), 4) if x else None

    return {
        "n": len(rows),
        "brier_1x2": mean(b1), "log_loss_1x2": mean(l1),
        "brier_over25": mean(bo), "log_loss_over25": mean(lo),
        "brier_btts": mean(bb), "log_loss_btts": mean(lb),
        "accuracy_1x2": round(float(np.mean([
            max(("home", "draw", "away"), key=lambda k: r[f"p_{k}"]) == outcome_1x2(r["fthg"], r["ftag"])
            for r in rows])), 4),
        "exact_score_hit": round(float(np.mean(exact)), 4) if exact else None,
        "top3_score_hit": round(float(np.mean(top3)), 4) if top3 else None,
        "calibration": calibration_buckets(pairs),
        "calibration_1x2": {k: calibration_buckets([(r[f"p_{k}"], int(outcome_1x2(r["fthg"], r["ftag"]) == k))
                                                    for r in rows]) for k in ("home", "draw", "away")},
        "biggest_misses": misses[:10],
    }


def score_bets(bets: list[dict]) -> dict:
    """bets: dicts with stake, odds_taken, result, closing_odds."""
    settled = [b for b in bets if b.get("result") in ("win", "loss")]
    staked = sum(b["stake"] for b in settled)
    pnl = sum(bet_pnl(b) for b in settled)
    flat = [(b["odds_taken"] - 1) if b["result"] == "win" else -1.0 for b in settled]
    clv = [b["odds_taken"] / b["closing_odds"] - 1 for b in bets if b.get("closing_odds")]
    return {
        "n_bets": len(bets), "n_settled": len(settled),
        "hit_rate": round(sum(b["result"] == "win" for b in settled) / len(settled), 4) if settled else None,
        "staked": round(staked, 4), "pnl": round(pnl, 4),
        "roi_kelly": round(pnl / staked, 4) if staked else None,
        "roi_flat": round(float(np.mean(flat)), 4) if flat else None,
        "avg_clv": round(float(np.mean(clv)), 4) if clv else None,
        "clv_positive_rate": round(float(np.mean([c > 0 for c in clv])), 4) if clv else None,
        "n_clv": len(clv),
    }


def bet_pnl(b: dict) -> float:
    if b["result"] == "win":
        return b["stake"] * (b["odds_taken"] - 1)
    if b["result"] == "loss":
        return -b["stake"]
    return 0.0


def settle_selection(market: str, selection: str, line, hg: int, ag: int) -> str:
    if market == "h2h":
        return "win" if outcome_1x2(hg, ag) == selection else "loss"
    if market == "totals":
        tot = hg + ag
        if tot == line:
            return "void"
        return "win" if (tot > line) == (selection == "over") else "loss"
    if market == "btts":
        yes = hg > 0 and ag > 0
        return "win" if yes == (selection == "yes") else "loss"
    raise ValueError(f"unknown market {market}")


# ---------- DB-backed ----------

def settle_bets(conn) -> int:
    rows = conn.execute(
        "SELECT b.id, b.market, b.selection, b.line, b.stake, b.odds_taken, m.fthg, m.ftag FROM bets b "
        "JOIN matches m ON m.id = b.match_id WHERE b.result IS NULL AND m.status = 'FT'").fetchall()
    for r in rows:
        res = settle_selection(r["market"], r["selection"], r["line"], r["fthg"], r["ftag"])
        pnl = bet_pnl({"result": res, "stake": r["stake"], "odds_taken": r["odds_taken"]})
        conn.execute("UPDATE bets SET result=?, pnl=? WHERE id=?", (res, pnl, r["id"]))
    return len(rows)


def update_closing_odds(conn) -> None:
    """Closing odds = last FanDuel snapshot before kickoff (fallback: football-data closing average)."""
    rows = conn.execute(
        "SELECT b.id, b.match_id, b.market, b.selection, b.line, m.kickoff_utc, m.close_h, m.close_d, "
        "m.close_a, m.close_o25, m.close_u25 FROM bets b JOIN matches m ON m.id=b.match_id "
        "WHERE m.kickoff_utc IS NOT NULL AND m.kickoff_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchall()
    for r in rows:
        snap = conn.execute(
            "SELECT price FROM odds_snapshots WHERE match_id=? AND market=? AND selection=? "
            "AND COALESCE(line, -1) = COALESCE(?, -1) AND fetched_at <= ? ORDER BY fetched_at DESC LIMIT 1",
            (r["match_id"], r["market"], r["selection"], r["line"], r["kickoff_utc"])).fetchone()
        price = snap["price"] if snap else None
        if price is None and r["market"] == "h2h":
            price = {"home": r["close_h"], "draw": r["close_d"], "away": r["close_a"]}[r["selection"]]
        if price is None and r["market"] == "totals" and r["line"] == 2.5:
            price = r["close_o25"] if r["selection"] == "over" else r["close_u25"]
        if price:
            conn.execute("UPDATE bets SET closing_odds=? WHERE id=?", (price, r["id"]))


def tracked_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT p.match_id, p.payload, m.fthg, m.ftag, m.home, m.away, m.date, m.league FROM predictions p "
        "JOIN matches m ON m.id = p.match_id WHERE m.status='FT' AND m.fthg IS NOT NULL").fetchall()
    out = []
    for r in rows:
        pl = json.loads(r["payload"])
        if pl.get("retro"):
            continue                     # backfilled after kickoff: not a live prediction
        mk = pl["markets"]
        out.append({"id": r["match_id"], "league": r["league"], "date": r["date"], "home": r["home"],
                    "away": r["away"], "fthg": r["fthg"], "ftag": r["ftag"],
                    "p_home": mk["1x2"]["home"], "p_draw": mk["1x2"]["draw"], "p_away": mk["1x2"]["away"],
                    "p_over25": mk["totals"]["2.5"]["over"], "p_btts": mk["btts"]["yes"],
                    "predicted_score": pl.get("predicted_score"),
                    "top_scores": [c["score"] for c in pl.get("top_scores") or pl["markets"].get("correct_score", [])][:3]})
    return out


def performance(conn) -> dict:
    rows = tracked_rows(conn)
    bets = [dict(b) for b in conn.execute("SELECT * FROM bets").fetchall()]
    by_league = {}
    for lg in sorted({r["league"] for r in rows}):
        sub = score_predictions([r for r in rows if r["league"] == lg])
        by_league[lg] = {k: v for k, v in sub.items() if k not in ("calibration", "calibration_1x2", "biggest_misses")}
    return {"predictions": score_predictions(rows), "by_league": by_league, "bets": score_bets(bets)}
