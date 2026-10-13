import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from matchpredictor import config, db, schedule, tracking
from matchpredictor.ingest import football_data, odds_api
from matchpredictor.pipeline import evaluate_value, latest_odds, normalise_referee
from matchpredictor.predict import fit_league, load_matches, predict_match

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# ---------- config helpers ----------

def test_season_code():
    assert config.season_code(date(2026, 10, 13)) == "2627"
    assert config.season_code(date(2026, 5, 24)) == "2526"
    assert config.season_code("2026-07-01T12:00:00Z") == "2627"
    assert config.previous_seasons("2627", 3) == ["2324", "2425", "2526"]
    assert config.previous_seasons("0001", 1) == ["9900"]


def test_canonical_team():
    assert config.canonical_team("Manchester United") == "Man United"
    assert config.canonical_team("Atlético Madrid") == "Ath Madrid"
    assert config.canonical_team("Chelsea", {"Chelsea"}) == "Chelsea"
    assert config.canonical_team("Crystal Palce", {"Crystal Palace"}) == "Crystal Palace"   # fuzzy
    assert config.canonical_team("Nowhere FC", {"Chelsea"}) == "Nowhere FC"


def test_normalise_referee():
    assert normalise_referee("M. Oliver, England") == "M Oliver"
    assert normalise_referee(None) is None


# ---------- ingest ----------

def test_parse_odds_fixture():
    events = json.loads((FIXTURES / "odds_epl_h2h.json").read_text())
    rows = odds_api.parse_odds(events[0], events[0]["home_team"], events[0]["away_team"])
    sels = {r["selection"]: r["price"] for r in rows if r["market"] == "h2h"}
    assert set(sels) == {"home", "draw", "away"}
    assert sels["home"] == 3.2 and sels["away"] == 2.1     # Everton (home) v Chelsea


def test_parse_odds_totals():
    ev = {"home_team": "A", "away_team": "B", "bookmakers": [{"key": "fanduel", "markets": [
        {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5},
                                       {"name": "Under", "price": 1.9, "point": 2.5}]}]}]}
    rows = odds_api.parse_odds(ev, "A", "B")
    assert {(r["selection"], r["line"]) for r in rows} == {("over", 2.5), ("under", 2.5)}


CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA,PC>2.5,PC<2.5
E0,16/08/2025,20:00,Liverpool,Bournemouth,4,2,H,A Taylor,19,10,10,3,7,10,6,7,1,2,0,0,1.30,6.0,9.5,1.29,5.8,9.0,1.6,2.4
E0,17/08/2025,14:00,Chelsea,Crystal Palace,0,0,D,S Barrott,20,13,3,3,10,12,7,6,2,1,0,0,,,,1.55,4.2,6.0,,
"""


def test_parse_football_data_csv():
    rows = football_data.parse_csv(CSV, "EPL", "2526", "football-data")
    assert len(rows) == 2
    a, b = rows
    assert a["date"] == "2025-08-16" and a["status"] == "FT" and a["fthg"] == 4 and a["hc"] == 6
    assert (a["close_h"], a["close_d"], a["close_a"]) == (1.30, 6.0, 9.5)      # Pinnacle closing preferred
    assert a["close_o25"] == 1.6 and a["referee"] == "A Taylor"
    assert (b["close_h"], b["close_d"], b["close_a"]) == (1.55, 4.2, 6.0)      # falls back to average closing
    assert b["close_o25"] is None


def test_upsert_merges_without_erasing(conn):
    base = {"league": "EPL", "season": "2627", "home": "A", "away": "B"}
    mid = db.upsert_match(conn, {**base, "date": "2026-10-17", "kickoff_utc": "2026-10-17T14:00:00Z",
                                 "status": "SCHEDULED", "odds_api_id": "x1", "source": "odds-api"})
    db.upsert_match(conn, {**base, "date": "2026-10-17", "status": "FT", "fthg": 2, "ftag": 1, "hc": 5,
                           "kickoff_utc": None, "source": "football-data"})
    # a later fixture refresh must not reset status or source
    db.upsert_match(conn, {**base, "date": "2026-10-17", "status": "SCHEDULED", "source": "odds-api"})
    r = conn.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    assert r["status"] == "FT" and r["fthg"] == 2 and r["kickoff_utc"] == "2026-10-17T14:00:00Z"
    assert r["odds_api_id"] == "x1" and r["source"] == "football-data"


# ---------- tracking ----------

def test_settle_selection():
    s = tracking.settle_selection
    assert s("h2h", "home", None, 2, 1) == "win"
    assert s("h2h", "draw", None, 2, 1) == "loss"
    assert s("totals", "over", 2.5, 2, 1) == "win"
    assert s("totals", "under", 2.5, 2, 1) == "loss"
    assert s("totals", "over", 3.0, 2, 1) == "void"
    assert s("btts", "yes", None, 2, 0) == "loss"


def test_score_predictions_perfect_and_uniform():
    perfect = [{"p_home": 1.0, "p_draw": 0.0, "p_away": 0.0, "p_over25": 1.0, "p_btts": 1.0, "fthg": 2, "ftag": 1}]
    m = tracking.score_predictions(perfect)
    assert m["brier_1x2"] == 0 and m["log_loss_1x2"] == pytest.approx(0, abs=1e-9)
    uni = [{"p_home": 1 / 3, "p_draw": 1 / 3, "p_away": 1 / 3, "p_over25": 0.5, "p_btts": 0.5, "fthg": 0, "ftag": 0}]
    m = tracking.score_predictions(uni)
    assert m["log_loss_1x2"] == pytest.approx(np.log(3), abs=1e-4)
    assert m["brier_1x2"] == pytest.approx(2 / 3, abs=1e-4)


def test_calibration_buckets():
    pairs = [(0.05, 0), (0.15, 1), (0.95, 1), (1.0, 1)]
    b = tracking.calibration_buckets(pairs)
    assert sum(x["n"] for x in b) == 4
    assert b[-1]["bin"] == "0.9-1.0" and b[-1]["n"] == 2


def test_score_bets_roi_and_clv():
    bets = [{"stake": 0.02, "odds_taken": 2.5, "result": "win", "closing_odds": 2.2},
            {"stake": 0.01, "odds_taken": 3.0, "result": "loss", "closing_odds": 3.3},
            {"stake": 0.01, "odds_taken": 2.0, "result": None, "closing_odds": None}]
    s = tracking.score_bets(bets)
    assert s["n_settled"] == 2 and s["pnl"] == pytest.approx(0.02 * 1.5 - 0.01)
    assert s["roi_kelly"] == pytest.approx((0.03 - 0.01) / 0.03, abs=1e-4)
    assert s["roi_flat"] == pytest.approx((1.5 - 1) / 2)
    assert s["avg_clv"] == pytest.approx(((2.5 / 2.2 - 1) + (3.0 / 3.3 - 1)) / 2, abs=1e-4)


def test_bet_lifecycle(conn):
    mid = db.upsert_match(conn, {"league": "EPL", "season": "2627", "date": "2026-01-01", "home": "A",
                                 "away": "B", "kickoff_utc": "2026-01-01T15:00:00Z", "status": "SCHEDULED"})
    conn.execute("INSERT INTO odds_snapshots (match_id, fetched_at, bookmaker, market, selection, line, price) "
                 "VALUES (?, '2026-01-01T09:00:00Z', 'fanduel', 'h2h', 'home', NULL, 2.4), "
                 "(?, '2026-01-01T14:10:00Z', 'fanduel', 'h2h', 'home', NULL, 2.2)", (mid, mid))
    from matchpredictor.pipeline import log_bets
    a = {"market": "h2h", "selection": "home", "line": None, "model_prob": 0.5, "market_prob": 0.4,
         "price": 2.4, "edge": 0.25, "ev": 0.2, "stake": 0.02, "is_value": True}
    log_bets(conn, mid, [a], "2026-01-01T09:00:00Z")
    log_bets(conn, mid, [dict(a, price=2.2)], "2026-01-01T14:10:00Z")    # first price stays locked
    tracking.update_closing_odds(conn)
    conn.execute("UPDATE matches SET status='FT', fthg=1, ftag=0 WHERE id=?", (mid,))
    assert tracking.settle_bets(conn) == 1
    b = conn.execute("SELECT * FROM bets").fetchone()
    assert b["odds_taken"] == 2.4 and b["closing_odds"] == 2.2 and b["result"] == "win"
    assert b["pnl"] == pytest.approx(0.02 * 1.4)
    assert latest_odds(conn, mid)["h2h"]["home"] == 2.2


# ---------- value evaluation on a payload ----------

def test_evaluate_value_devigs_each_market():
    payload = {"markets": {"1x2": {"home": 0.5, "draw": 0.25, "away": 0.25},
                           "totals": {"2.5": {"over": 0.6, "under": 0.4}},
                           "btts": {"yes": 0.5, "no": 0.5}}}
    odds = {"h2h": {"home": 2.2, "draw": 3.5, "away": 3.5}, "totals": {"2.5": {"over": 1.9, "under": 1.9}}}
    out = evaluate_value(payload, odds)
    h2h = [v for v in out if v["market"] == "h2h"]
    assert sum(v["market_prob"] for v in h2h) == pytest.approx(1.0, abs=1e-3)
    over = [v for v in out if v["market"] == "totals" and v["selection"] == "over"][0]
    assert over["market_prob"] == pytest.approx(0.5) and over["edge"] == pytest.approx(0.2) and over["is_value"]
    assert not any(v["market"] == "btts" for v in out)       # no btts prices -> not evaluated


# ---------- schedule gate ----------

def test_gate(tmp_path):
    p = tmp_path / "m.db"
    c = db.connect(p)
    ko = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
    db.upsert_match(c, {"league": "EPL", "season": "2627", "date": "2026-10-17", "home": "A", "away": "B",
                        "kickoff_utc": ko.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "SCHEDULED"})
    c.commit(); c.close()
    assert schedule.gate(datetime(2026, 10, 17, 13, 7, tzinfo=timezone.utc), p) == "daily"      # 09:07 EDT
    assert schedule.gate(datetime(2026, 10, 17, 12, 7, tzinfo=timezone.utc), p) == "skip"       # 113 min out
    assert schedule.gate(datetime(2026, 10, 17, 18, 7, tzinfo=timezone.utc), p) == "skip"
    # outside the ET daily hour: 21:00 UTC kickoff, tick at 20:07 -> 53 min out
    c = db.connect(p)
    db.upsert_match(c, {"league": "EPL", "season": "2627", "date": "2026-10-17", "home": "C", "away": "D",
                        "kickoff_utc": "2026-10-17T21:00:00Z", "status": "SCHEDULED"})
    c.commit(); c.close()
    assert schedule.gate(datetime(2026, 10, 17, 20, 7, tzinfo=timezone.utc), p) == "prekickoff"
    assert schedule.gate(datetime(2026, 10, 17, 19, 7, tzinfo=timezone.utc), p) == "skip"
    assert schedule.gate(datetime(2026, 10, 17, 20, 7, tzinfo=timezone.utc), tmp_path / "none.db") == "skip"


# ---------- offline end-to-end: synthetic league -> fit -> full prediction ----------

def _synthetic_league(conn, seasons=("2425", "2526"), seed=0):
    rng = np.random.default_rng(seed)
    teams = [f"T{i}" for i in range(12)]
    strength = dict(zip(teams, np.linspace(-0.4, 0.4, len(teams))))
    for s in seasons:
        start = date(2000 + int(s[:2]), 8, 10)
        k = 0
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                lam = np.exp(0.25 + strength[h] - strength[a] * 0.8)
                mu = np.exp(0.05 + strength[a] - strength[h] * 0.8)
                d = start + timedelta(days=(k // 6) * 7)
                k += 1
                db.upsert_match(conn, {
                    "league": "EPL", "season": s, "date": d.isoformat(), "home": h, "away": a, "status": "FT",
                    "fthg": int(rng.poisson(lam)), "ftag": int(rng.poisson(mu)),
                    "hs": int(rng.poisson(13)), "as_": int(rng.poisson(11)), "hst": int(rng.poisson(4.5)),
                    "ast": int(rng.poisson(3.8)), "hc": int(rng.poisson(5.5)), "ac": int(rng.poisson(4.5)),
                    "hy": int(rng.poisson(1.8)), "ay": int(rng.poisson(2.0)), "hr": 0, "ar": 0,
                    "referee": f"Ref{k % 5}", "source": "football-data"})
    return teams


def test_fit_and_predict_offline(conn):
    teams = _synthetic_league(conn)
    df = load_matches(conn)
    model = fit_league(df, "EPL", date(2026, 8, 1), fixture_teams={"NEWCO"})
    assert model is not None
    assert "NEWCO" in model.promoted
    strong, weak = teams[-1], teams[0]
    p = predict_match(model, strong, weak, referee="Ref1")
    mk = p["markets"]
    assert sum(mk["1x2"].values()) == pytest.approx(1.0, abs=1e-3)
    assert mk["1x2"]["home"] > mk["1x2"]["away"]
    assert p["fair_odds"]["1x2"]["home"] == pytest.approx(1 / mk["1x2"]["home"], rel=1e-3)
    for stat in ("corners", "sot", "cards"):
        tl = p["stats"][stat]["total_lines"]
        for v in tl.values():
            assert v["over"] + v["under"] == pytest.approx(1.0, abs=1e-3)
    # promoted team with no history falls back to the prior, i.e. is predicted to be weak
    q = predict_match(model, teams[5], "NEWCO")
    assert q["markets"]["1x2"]["home"] > q["markets"]["1x2"]["away"]
    assert q["promoted"]["away"] is True
    # absences reduce expected goals
    r = predict_match(model, strong, weak, absences={"home": [0.4]})
    assert r["xg"]["home"] < p["xg"]["home"]


def test_backtest_runs_on_synthetic(conn):
    from matchpredictor import backtest
    _synthetic_league(conn, seasons=("2324", "2425", "2526"))
    df = load_matches(conn)
    rows = backtest.walk_forward(df, "EPL", "2526", refit_days=28)
    assert len(rows) > 100
    m = tracking.score_predictions(rows)
    base = tracking.score_predictions(backtest.baseline_rows(df, "EPL", "2526", rows))
    assert m["brier_1x2"] < base["brier_1x2"]            # real signal in synthetic data is learnt
