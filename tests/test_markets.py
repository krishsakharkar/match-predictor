"""Tests for the per-market modules (corners, cards, shots, players), bankers and market backtest."""
import math

import numpy as np
import pytest

from matchpredictor import backtest, db
from matchpredictor.absences import covered
from matchpredictor.ingest import understat
from matchpredictor.models import cards, corners, counts, players, shots


def _model(stat, teams, gen, conc, b=1.6, h=0.1, alpha=0.05, refs=None):
    return counts.CountModel(stat=stat, teams=teams, gen=np.array(gen), conc=np.array(conc), intercept=b,
                             home_adv=h, alpha=alpha, referee_factor=refs or {})


EPL = _model("corners", ["A", "B"], [0.2, -0.1], [-0.1, 0.1])
LIGA = _model("corners", ["C", "D"], [0.1, 0.0], [0.0, 0.05], b=1.5, h=0.12)


def test_same_league_expectation_and_lines():
    out = corners.predict({"EPL": EPL}, "EPL", "A", "B", {"A": "EPL", "B": "EPL"})
    assert out["home"] == pytest.approx(math.exp(1.6 + 0.1 + 0.2 + 0.1))
    assert out["away"] == pytest.approx(math.exp(1.6 - 0.1 - 0.1))
    assert out["total"] == pytest.approx(out["home"] + out["away"])
    for v in list(out["total_lines"].values()) + list(out["home_lines"].values()):
        assert v["over"] + v["under"] == pytest.approx(1.0, abs=1e-6)
    assert out["source"] == "league model" and out["thin"] == []


def test_cross_league_uses_each_teams_own_ratings():
    out = corners.predict({"EPL": EPL, "LALIGA": LIGA}, "UCL", "A", "C", {"A": "EPL", "C": "LALIGA"})
    b, h = (1.6 + 1.5) / 2, (0.1 + 0.12) / 2
    assert out["home"] == pytest.approx(math.exp(b + h + 0.2 + 0.0))
    assert out["source"] == "cross-league estimate"
    thin = corners.predict({"EPL": EPL}, "UCL", "A", "Galatasaray", {"A": "EPL"})
    assert thin["thin"] == ["Galatasaray"]


def test_cards_referee_factor():
    m = _model("cards", ["A", "B"], [0, 0], [0, 0], b=0.7, refs={"Strict": 1.3})
    base = cards.predict({"EPL": m}, "EPL", "A", "B", {"A": "EPL", "B": "EPL"})
    strict = cards.predict({"EPL": m}, "EPL", "A", "B", {"A": "EPL", "B": "EPL"}, referee="Strict")
    assert strict["total"] == pytest.approx(base["total"] * 1.3)
    assert strict["referee_factor"] == 1.3 and base["referee_factor"] is None


def test_shots_and_sot_blocks():
    sm = _model("shots", ["A", "B"], [0, 0], [0, 0], b=2.5)
    tm = _model("sot", ["A", "B"], [0, 0], [0, 0], b=1.4)
    out = shots.predict({"EPL": sm}, {"EPL": tm}, "EPL", "A", "B", {"A": "EPL", "B": "EPL"})
    assert set(out) == {"shots", "sot"} and out["shots"]["total"] > out["sot"]["total"]
    assert shots.predict({}, {}, "EPL", "A", "B", {}) is None


PLAYERS = [
    {"player": "Striker", "position": "F S", "games": 10, "minutes": 900, "goals": 8, "xg": 7.5, "npxg": 6.0,
     "assists": 1, "xa": 1.0, "shots": 40},
    {"player": "Winger", "position": "M", "games": 10, "minutes": 800, "goals": 2, "xg": 2.0, "npxg": 2.0,
     "assists": 4, "xa": 3.5, "shots": 20},
    {"player": "Keeper", "position": "GK", "games": 10, "minutes": 900, "goals": 0, "xg": 0, "npxg": 0,
     "assists": 0, "xa": 0, "shots": 0},
    {"player": "Sub", "position": "S", "games": 3, "minutes": 60, "goals": 1, "xg": 0.5, "npxg": 0.5,
     "assists": 0, "xa": 0, "shots": 3},
]


def test_player_props_maths():
    out = players.predict(PLAYERS, team_games=10, team_xg=2.0, team_shots=15, team_sot=5.5)
    names = [r["player"] for r in out]
    assert "Keeper" not in names and "Sub" not in names          # GK excluded, too few minutes
    st = next(r for r in out if r["player"] == "Striker")
    assert st["penalty_taker"] is True                            # holds the penalty xG
    pen_share = (7.5 + 2 + 0.5 - (6 + 2 + 0.5)) / (7.5 + 2 + 0.5)
    g_share = min(0.6, (6.0 / 8.5) * (900 / 900))
    lam = 2.0 * (1 - pen_share) * g_share + 2.0 * pen_share
    assert st["anytime_goal"] == pytest.approx(1 - math.exp(-lam), abs=1e-3)
    for r in out:
        for k in ("anytime_goal", "assist", "sot_1plus", "shots_2plus"):
            assert 0 <= r[k] < 1
    out2 = players.predict(PLAYERS, 10, 2.0, 15, 5.5, statuses={"Striker": "out"})
    assert next(r for r in out2 if r["player"] == "Striker")["anytime_goal"] == 0
    assert players.predict([], 10, 2.0, 15, 5.5) == []


def test_two_plus_shots_formula():
    lam = 1.7
    out = players.predict([dict(PLAYERS[0], shots=40)], team_games=10, team_xg=1.0, team_shots=lam / 0.6, team_sot=1)
    assert out[0]["shots_2plus"] == pytest.approx(1 - math.exp(-lam) * (1 + lam), abs=1e-3)


def test_bankers_threshold_table():
    rows = [{"p_home": 0.8, "p_draw": 0.1, "p_away": 0.1, "fthg": 2, "ftag": 0},
            {"p_home": 0.75, "p_draw": 0.15, "p_away": 0.1, "fthg": 0, "ftag": 0},
            {"p_home": 0.4, "p_draw": 0.3, "p_away": 0.3, "fthg": 1, "ftag": 0}]
    b = backtest.bankers(rows, thresholds=(0.7, 0.8))
    assert b["0.7"] == {"n": 2, "share": pytest.approx(0.6667, abs=1e-3), "hit": 0.5}
    assert b["0.8"]["n"] == 1 and b["0.8"]["hit"] == 1.0


def test_understat_skips_multi_club_titles():
    conn = db.connect(":memory:")
    payload = {"dates": [], "players": [
        {"id": "1", "player_name": "Mover", "team_title": "Paris Saint Germain,Rennes", "games": "6", "time": "500"},
        {"id": "2", "player_name": "Stayer", "team_title": "Paris Saint Germain", "games": "6", "time": "500",
         "goals": "1", "xG": "1.0", "npxG": "1.0", "assists": "0", "xA": "0.2", "shots": "8", "position": "F"}]}
    understat.store(conn, "LIGUE1", "2627", payload, {"Paris SG", "Rennes"})
    rows = conn.execute("SELECT player, team FROM understat_players").fetchall()
    assert [tuple(r) for r in rows] == [("Stayer", "Paris SG")]


def test_coverage_guard():
    conn = db.connect(":memory:")
    for i in range(5):
        db.upsert_match(conn, {"league": "EPL", "season": "2627", "date": f"2026-09-0{i + 1}", "home": "A",
                               "away": f"O{i}", "status": "FT", "fthg": 1, "ftag": 0})
    full = [{"minutes": 450}] * 11
    assert covered(conn, "EPL", "2627", "A", full)
    assert not covered(conn, "EPL", "2627", "A", full[:4])
