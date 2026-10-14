import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from matchpredictor import config, db, schedule, tracking
from matchpredictor.ingest import football_data, odds_api
from matchpredictor.betting import evaluate_value, latest_odds
from matchpredictor.ingest.api_football import normalise_referee
from matchpredictor.predict import Predictor, fit_league, load_matches, predict_match

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
    from matchpredictor.betting import log_bets
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
    assert schedule.gate(datetime(2026, 10, 17, 18, 7, tzinfo=timezone.utc), p) == "results"   # 14:00 game, no score yet
    # outside the ET daily hour: 21:00 UTC kickoff, tick at 20:07 -> 53 min out
    c = db.connect(p)
    db.upsert_match(c, {"league": "EPL", "season": "2627", "date": "2026-10-17", "home": "C", "away": "D",
                        "kickoff_utc": "2026-10-17T21:00:00Z", "status": "SCHEDULED"})
    c.commit(); c.close()
    assert schedule.gate(datetime(2026, 10, 17, 20, 7, tzinfo=timezone.utc), p) == "prekickoff"
    assert schedule.gate(datetime(2026, 10, 17, 19, 7, tzinfo=timezone.utc), p) == "results"   # 14:00 game still unscored
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
    p = predict_match(model, strong, weak)
    mk = p["markets"]
    assert sum(mk["1x2"].values()) == pytest.approx(1.0, abs=1e-3)
    assert mk["1x2"]["home"] > mk["1x2"]["away"]
    assert p["fair_odds"]["1x2"]["home"] == pytest.approx(1 / mk["1x2"]["home"], rel=1e-3)
    assert len(p["top_scores"]) == 6 and p["top_scores"][0]["score"] == p["predicted_score"]
    # promoted team with no history falls back to the prior, i.e. is predicted to be weak
    q = predict_match(model, teams[5], "NEWCO")
    assert q["markets"]["1x2"]["home"] > q["markets"]["1x2"]["away"]
    assert q["promoted"]["away"] is True


def test_predictor_league_factors_and_absences(conn):
    teams = _synthetic_league(conn)
    df = load_matches(conn)
    strong, weak = teams[-1], teams[0]
    off = {f: False for f in ("xg", "elo", "rest", "injuries", "referee_penalties")}
    pr = Predictor(df, date(2026, 8, 1), factors=dict(off, injuries=True))
    p = pr.predict("EPL", strong, weak, "2026-08-15")
    assert p["source"] == "Dixon-Coles" and p["factors"] == {}
    assert sum((p["markets"]["1x2"][k] for k in ("home", "draw", "away"))) == pytest.approx(1, abs=1e-3)
    eff = {"home": {"attack": 0.8, "defence": 1.1, "players": []}}
    q = pr.predict("EPL", strong, weak, "2026-08-15", absences=eff)
    assert q["xg"]["home"] == pytest.approx(p["xg"]["home"] * 0.8, rel=1e-3)
    assert q["xg"]["away"] == pytest.approx(p["xg"]["away"] * 1.1, rel=1e-3)
    # injuries switched off -> absences ignored
    pr2 = Predictor(df, date(2026, 8, 1), factors=off, models=pr.models)
    assert pr2.predict("EPL", strong, weak, "2026-08-15", absences=eff)["xg"] == p["xg"]
    # no ClubElo and teams from no league -> no prediction rather than a guess
    assert pr.predict("UCL", "Nowhere A", "Nowhere B", "2026-09-15") is None


def test_predictor_uses_elo_cross_league(conn):
    teams = _synthetic_league(conn)
    df = load_matches(conn)
    book = _FakeElo({teams[-1]: 1900, teams[0]: 1500, "Galatasaray": 1700})
    pr = Predictor(df, date(2026, 8, 1), elo_book=book, factors={"elo": True, "rest": False, "xg": False,
                                                                 "injuries": False, "referee_penalties": False},
                   elo_params={"alpha_home": 0.3, "alpha_away": 0.05, "beta": 0.12, "hfa": {"UCL": 0.05}})
    p = pr.predict("UCL", "Galatasaray", teams[-1], "2026-09-15")
    assert p is not None and p["source"] in ("Elo", "Elo + league ratings")
    assert p["markets"]["1x2"]["away"] > p["markets"]["1x2"]["home"]
    lg = pr.predict("EPL", teams[-1], teams[0], "2026-08-15")
    assert lg["source"] == "Dixon-Coles + Elo"


class _FakeElo:
    """Minimal stand-in for ingest.clubelo.EloBook."""
    def __init__(self, ratings):
        self.r = ratings

    def __bool__(self):
        return True

    def club_for(self, team, country=None):
        return team if team in self.r else None

    def team_rating(self, team, day, country=None):
        return self.r.get(team)

    def last_match(self, club, day):
        return None


def test_backtest_pipeline_on_synthetic(conn):
    from matchpredictor import backtest
    _synthetic_league(conn, seasons=("2324", "2425", "2526"))
    df = load_matches(conn)
    elo_p = config.settings()["elo"]["default_params"]
    rows = backtest.collect_league(df, conn, None, "EPL", "2526", [0.0], elo_p, refit_days=28)
    assert len(rows) > 100
    base = {"xg_weight": 0.0, "blend_weight": 1.0, "rest_coef": 0.0, "elo_params": elo_p,
            **{f: False for f in ("xg", "elo", "rest", "injuries", "referee_penalties")}}
    scored = backtest.evaluate(rows, base)
    m = tracking.score_predictions(scored)
    assert m["n"] == len(rows)
    assert 0 <= m["exact_score_hit"] <= m["top3_score_hit"] <= 1
    naive = [dict(r, p_home=0.45, p_draw=0.27, p_away=0.28) for r in scored]
    assert m["brier_1x2"] < tracking.score_predictions(naive)["brier_1x2"]   # real signal is learnt
    cfg, log = backtest.tune(rows, base, [])
    assert cfg["xg"] is False and log["xg"]["available"] is False           # no xG in synthetic data
    assert log["elo"]["available"] is False


def test_sync_fixtures_dedupes_by_event(conn, monkeypatch):
    from matchpredictor import pipeline
    from matchpredictor.ingest import odds_api as oa
    db.upsert_match(conn, {"league": "UCL", "season": "2627", "date": "2026-10-14", "home": "Porto-old",
                           "away": "Betis", "kickoff_utc": "2026-10-14T19:00:00Z", "odds_api_id": "ev1",
                           "status": "SCHEDULED", "source": "odds-api"})
    monkeypatch.setattr(oa, "events", lambda conn, sport: [{"id": "ev1", "commence_time": "2026-10-14T19:00:00Z",
                                                             "home_team": "Porto", "away_team": "Real Betis"}]
                        if sport == "soccer_uefa_champs_league" else [])
    pipeline.sync_fixtures(conn, [])
    rows = conn.execute("SELECT home, away FROM matches WHERE odds_api_id='ev1'").fetchall()
    assert [tuple(r) for r in rows] == [("FC Porto", "Betis")]


def test_gate_results_mode(tmp_path):
    p = tmp_path / "r.db"
    c = db.connect(p)
    db.upsert_match(c, {"league": "EPL", "season": "2627", "date": "2026-10-17", "home": "A", "away": "B",
                        "kickoff_utc": "2026-10-17T14:00:00Z", "status": "SCHEDULED"})
    c.commit(); c.close()
    assert schedule.gate(datetime(2026, 10, 17, 17, 7, tzinfo=timezone.utc), p) == "results"   # 3h after
    assert schedule.gate(datetime(2026, 10, 17, 22, 7, tzinfo=timezone.utc), p) == "results"   # 8h after (missed ticks)
    assert schedule.gate(datetime(2026, 10, 18, 3, 7, tzinfo=timezone.utc), p) == "skip"       # 13h after


def test_retro_predictions_excluded_from_live_tracking(conn):
    mid = db.upsert_match(conn, {"league": "EPL", "season": "2627", "date": "2026-10-10", "home": "A",
                                 "away": "B", "status": "FT", "fthg": 1, "ftag": 0})
    payload = {"markets": {"1x2": {"home": .5, "draw": .3, "away": .2}, "totals": {"2.5": {"over": .5}},
                           "btts": {"yes": .5}}, "xg": {"home": 1.4, "away": 1.0}, "predicted_score": "1-0",
               "top_scores": [{"score": "1-0", "prob": .1}]}
    db.save_prediction(conn, mid, "2026-10-13T00:00:00Z", "x", dict(payload, retro=True))
    assert tracking.tracked_rows(conn) == []
    db.save_prediction(conn, mid, "2026-10-13T00:00:00Z", "x", payload)
    assert len(tracking.tracked_rows(conn)) == 1


def test_backfill_past_predicts_from_earlier_data_only(conn):
    from matchpredictor import pipeline
    teams = _synthetic_league(conn, seasons=("2425", "2526", "2627"))
    rows = conn.execute("SELECT id, date FROM matches WHERE season='2627' ORDER BY date LIMIT 3").fetchall()
    when = datetime.fromisoformat(rows[-1]["date"]).replace(tzinfo=timezone.utc) + timedelta(days=1)
    cfg = {"xg_weight": 0.0, "blend_weight": 1.0, "rest_coef": 0.0, "fresh_k": 0,
           **{f: False for f in ("xg", "elo", "rest", "injuries", "referee_penalties")}}
    n = pipeline.backfill_past(conn, [], cfg, when)
    assert n > 0
    got = conn.execute("SELECT payload FROM predictions").fetchall()
    assert all(json.loads(r["payload"])["retro"] for r in got)
    assert teams  # synthetic league is used


def test_sync_results_adds_finished_games_never_seen(conn, monkeypatch):
    from matchpredictor import pipeline
    from matchpredictor.ingest import odds_api as oa
    db.upsert_match(conn, {"league": "UCL", "season": "2627", "date": "2026-10-13", "home": "Arsenal",
                           "away": "Lille", "kickoff_utc": (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "odds_api_id": "seen", "status": "SCHEDULED", "source": "odds-api"})
    ko = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(oa, "scores", lambda conn, sport, days_from=3, ttl_minutes=None: [
        {"id": "seen", "completed": True, "home_team": "Arsenal", "away_team": "Lille", "commence_time": ko,
         "scores": [{"name": "Arsenal", "score": "3"}, {"name": "Lille", "score": "0"}]},
        {"id": "new", "completed": True, "home_team": "RC Lens", "away_team": "Sporting Lisbon", "commence_time": ko,
         "scores": [{"name": "RC Lens", "score": "0"}, {"name": "Sporting Lisbon", "score": "2"}]},
    ] if sport == "soccer_uefa_champs_league" else [])
    pipeline.sync_results(conn, [])
    rows = {(r["home"], r["away"]): (r["status"], r["fthg"], r["ftag"]) for r in conn.execute("SELECT * FROM matches")}
    assert rows[("Arsenal", "Lille")] == ("FT", 3, 0)
    assert rows[("Lens", "Sporting CP")] == ("FT", 0, 2)          # stored under canonical names


def test_sync_fixtures_one_european_competition_per_club(conn, monkeypatch):
    from matchpredictor import pipeline
    from matchpredictor.ingest import odds_api as oa
    db.upsert_match(conn, {"league": "SERIEA", "season": "2627", "date": "2026-10-10", "home": "Inter",
                           "away": "Parma", "status": "FT", "fthg": 2, "ftag": 0, "source": "football-data"})
    db.upsert_match(conn, {"league": "UCL", "season": "2627", "date": "2026-10-13", "home": "Inter",
                           "away": "Club Brugge KV", "status": "FT", "fthg": 3, "ftag": 4, "source": "odds-api"})
    # an earlier run stored a Conference League event under the wrong club
    db.upsert_match(conn, {"league": "UECL", "season": "2627", "date": "2026-10-15", "home": "Mjällby AIF",
                           "away": "Inter", "kickoff_utc": "2026-10-15T16:45:00Z", "odds_api_id": "ev9",
                           "status": "SCHEDULED", "source": "odds-api"})
    monkeypatch.setattr(oa, "events", lambda conn, sport: [{"id": "ev9", "commence_time": "2026-10-15T16:45:00Z",
                                                             "home_team": "Mjällby AIF", "away_team": "Inter Turku"}]
                        if sport == "soccer_uefa_europa_conference_league" else [])
    notes = []
    pipeline.sync_fixtures(conn, notes)
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE league='UECL'").fetchone()[0] == 0
    assert any("Inter Turku" in n for n in notes)


def test_load_seed_drops_rows_of_a_corrected_seed(conn, tmp_path, monkeypatch):
    from matchpredictor import history
    monkeypatch.setattr(history, "SEED_DIR", tmp_path)
    head = "competition,season,date,kickoff_utc,home,away,home_goals,away_goals,referee\n"
    (tmp_path / "ucl.csv").write_text(head + "UCL,2223,2022-09-07,,Ajax,Angers,4,0,\n")
    history.load_seed(conn)
    (tmp_path / "ucl.csv").write_text(head + "UCL,2223,2022-09-07,,Ajax,Rangers,4,0,\n")    # name fixed
    history.load_seed(conn)
    assert [r[0] for r in conn.execute("SELECT away FROM matches WHERE league='UCL'")] == ["Rangers"]
