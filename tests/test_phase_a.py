"""Tests for Phase A: xG blend, Elo model, context factors, new ingest parsers, scope, site cards."""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from matchpredictor import absences, config, db, site, tracking
from matchpredictor.ingest import api_football, clubelo, understat
from matchpredictor.models import dixon_coles as dc, elo, injuries
from matchpredictor.predict import combine, referee_penalty_rates


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# ---------------- Elo goals model

def _elo_sample(n=3000, seed=0, hfa_ucl=0.15):
    rng = np.random.default_rng(seed)
    eh, ea = rng.normal(1700, 120, n), rng.normal(1700, 120, n)
    comps = np.where(rng.random(n) < 0.3, "UCL", "EPL")
    dr = (eh - ea) / 100
    hfa = np.where(comps == "UCL", hfa_ucl, 0.0)
    hg = rng.poisson(np.exp(0.35 + hfa + 0.15 * dr))
    ag = rng.poisson(np.exp(0.10 - 0.15 * dr))
    return eh, ea, hg, ag, comps


def test_elo_fit_recovers_parameters():
    eh, ea, hg, ag, comps = _elo_sample()
    p = elo.fit(eh, ea, hg, ag, comps=comps, hfa_ridge=1.0)
    assert p["beta"] == pytest.approx(0.15, abs=0.03)
    assert p["alpha_away"] == pytest.approx(0.10, abs=0.05)
    # the UCL offset relative to EPL is recovered (ridge splits it between the two offsets)
    assert p["hfa"]["UCL"] - p["hfa"]["EPL"] == pytest.approx(0.15, abs=0.08)


def test_elo_expected_goals_monotone_and_hfa():
    params = {"alpha_home": 0.35, "alpha_away": 0.1, "beta": 0.13, "hfa": {"UCL": -0.05}}
    lam1, mu1 = elo.expected_goals(params, 1800, 1700)
    lam2, mu2 = elo.expected_goals(params, 1900, 1700)
    assert lam2 > lam1 and mu2 < mu1
    lam_u, _ = elo.expected_goals(params, 1800, 1700, "UCL")
    assert lam_u == pytest.approx(lam1 * np.exp(-0.05))


def test_blend_is_geometric():
    assert elo.blend(2.0, 1.0, 1.0) == pytest.approx(2.0)
    assert elo.blend(2.0, 1.0, 0.0) == pytest.approx(1.0)
    assert elo.blend(4.0, 1.0, 0.5) == pytest.approx(2.0)


def test_fit_offset_coef_recovers_gamma():
    rng = np.random.default_rng(3)
    n = 6000
    base_h, base_a = np.full(n, 1.5), np.full(n, 1.1)
    f = rng.integers(-5, 6, n).astype(float)
    hg = rng.poisson(base_h * np.exp(0.03 * f))
    ag = rng.poisson(base_a * np.exp(-0.03 * f))
    assert elo.fit_offset_coef(base_h, base_a, f, hg, ag) == pytest.approx(0.03, abs=0.01)


def test_rest_feature():
    assert elo.rest_feature(3, 10) == 3 - 7
    assert elo.rest_feature(1, 4) == 2 - 4
    assert elo.rest_feature(None, 4) == 0.0


def test_dc_to_elo_map():
    strength = {f"T{i}": i * 0.1 for i in range(10)}
    elos = {f"T{i}": 1500 + 200 * i * 0.1 for i in range(10)}
    a, b = elo.dc_to_elo_map(strength, elos)
    assert a == pytest.approx(1500) and b == pytest.approx(200)
    assert elo.dc_to_elo_map({"A": 1.0}, {"A": 1500}) is None


# ---------------- Dixon-Coles on xG-blended targets

def test_dc_gradient_with_non_integer_targets():
    rng = np.random.default_rng(5)
    teams = [f"T{i}" for i in range(8)]
    H, A = zip(*[(i, j) for i in teams for j in teams if i != j])
    sh, sa = rng.poisson(1.4, len(H)), rng.poisson(1.1, len(H))
    th, ta = 0.75 * rng.gamma(2, 0.7, len(H)) + 0.25 * sh, 0.75 * rng.gamma(2, 0.55, len(H)) + 0.25 * sa
    negll, ctx = dc.make_objective(list(H), list(A), th, ta, ridge=0.5, score_home=sh, score_away=sa)
    p = ctx["x0"] + rng.normal(0, 0.05, len(ctx["x0"]))
    p[-1] = -0.07
    _, g = negll(p)
    eps = 1e-6
    num = np.array([(negll(p + eps * e)[0] - negll(p - eps * e)[0]) / (2 * eps) for e in np.eye(len(p))])
    np.testing.assert_allclose(g, num, rtol=1e-4, atol=1e-4)
    m = dc.fit(list(H), list(A), th, ta, score_home=sh, score_away=sa)
    assert m.converged


# ---------------- combine(): factors

INFO = {"dc": (1.6, 1.0), "elo": (1.2, 1.2), "elo_ratings": (1800, 1750), "dc_elo": None, "hfa_key": "EPL",
        "rho": -0.05, "rest": (3, 7)}
NO = {f: False for f in ("xg", "elo", "rest", "injuries", "referee_penalties")}
PARAMS = {"alpha_home": 0.35, "alpha_away": 0.1, "beta": 0.13}


def test_combine_sources_and_factors():
    g = combine(INFO, factors=NO, blend_weight=0.5, rest_coef=0.0, elo_params=PARAMS)
    assert (g["lam"], g["mu"], g["source"]) == (1.6, 1.0, "Dixon-Coles")
    g = combine(INFO, factors=dict(NO, elo=True), blend_weight=0.5, rest_coef=0.0, elo_params=PARAMS)
    assert g["lam"] == pytest.approx(np.sqrt(1.6 * 1.2)) and g["source"] == "Dixon-Coles + Elo"
    g = combine(INFO, factors=dict(NO, rest=True), blend_weight=0.5, rest_coef=0.02, elo_params=PARAMS)
    x = 3 - 7
    assert g["lam"] == pytest.approx(1.6 * np.exp(0.02 * x)) and g["applied"]["rest"] == x
    g = combine(INFO, factors=dict(NO, referee_penalties=True), blend_weight=0.5, rest_coef=0, elo_params=PARAMS,
                referee_delta=0.03)
    assert g["lam"] == pytest.approx(1.63) and g["mu"] == pytest.approx(1.03)
    cross = dict(INFO, dc=None)
    g = combine(cross, factors=NO, blend_weight=0.5, rest_coef=0, elo_params=PARAMS)
    assert g["source"] == "Elo"
    assert combine(dict(INFO, dc=None, elo=None), factors=NO, blend_weight=0.5, rest_coef=0,
                   elo_params=PARAMS) is None


def test_referee_penalty_rates():
    df = pd.DataFrame({"referee": ["A"] * 10 + ["B"] * 10, "pens_home": [1] * 10 + [0] * 10,
                       "pens_away": [0] * 20})
    rates, league = referee_penalty_rates(df)
    assert rates["A"] == (10, 1.0) and rates["B"] == (10, 0.0) and league == 0.5
    assert referee_penalty_rates(pd.DataFrame({"referee": ["A"]}))[1] is None


# ---------------- absences

PLAYERS = [
    {"player": "Keeper One", "position": "GK", "games": 30, "minutes": 2700, "goals": 0, "npxg": 0, "xa": 0.1},
    {"player": "Keeper Two", "position": "GK S", "games": 3, "minutes": 270, "goals": 0, "npxg": 0, "xa": 0},
    {"player": "Star Striker", "position": "F S", "games": 30, "minutes": 2500, "goals": 20, "npxg": 15, "xa": 3},
    {"player": "Centre Back", "position": "D", "games": 30, "minutes": 2600, "goals": 2, "npxg": 1, "xa": 0.5},
    {"player": "Mid Field", "position": "M", "games": 30, "minutes": 2400, "goals": 8, "npxg": 4, "xa": 6.4},
]
CFG = {"replacement_retention": 0.5, "min_multiplier": 0.75, "gk_penalty": 0.06, "defender_penalty": 0.08,
       "regular_minutes_share": 0.4, "bench_weight": 0.5}


def test_absence_effect_attack_gk_defender():
    none = injuries.absence_effect([], PLAYERS, CFG)
    assert none == {"attack": 1.0, "defence": 1.0, "players": []}
    st = injuries.absence_effect(["Star Striker"], PLAYERS, CFG)
    share = 0.5 * 15 / 20 + 0.3 * 3 / 10 + 0.2 * 20 / 30
    assert st["attack"] == pytest.approx(max(0.75, 1 - share * 0.5)) and st["defence"] == 1.0
    gk = injuries.absence_effect(["Keeper One"], PLAYERS, CFG)
    assert gk["defence"] == pytest.approx(1.06) and gk["players"][0]["gk"] is True
    backup = injuries.absence_effect(["Keeper Two"], PLAYERS, CFG)
    assert backup["defence"] == 1.0                       # only the first-choice keeper matters
    cb = injuries.absence_effect(["Centre Back"], PLAYERS, CFG)
    assert cb["defence"] == pytest.approx(1 + 0.08 * 2600 / 2700)
    bench = injuries.absence_effect([], PLAYERS, CFG, partial={"Star Striker": 0.5})
    assert 1 - bench["attack"] == pytest.approx(share * 0.5 / 2)          # half weight, below the floor


def test_match_player():
    names = [p["player"] for p in PLAYERS]
    assert injuries.match_player("S. Striker", names) == "Star Striker"
    assert injuries.match_player("Star Striker", names) == "Star Striker"
    assert injuries.match_player("Nobody Here", names) is None


def _seed_team(conn):
    for p in PLAYERS:
        conn.execute("INSERT INTO understat_players (league, season, player_id, player, team, position, games, minutes,"
                     " goals, xg, npxg, assists, xa, shots, updated_at) VALUES ('EPL','2425',?,?,?,?,?,?,?,?,?,0,?,0,'x')",
                     (hash(p["player"]) % 10000, p["player"], "Alpha", p["position"], p["games"], p["minutes"],
                      p["goals"], p["npxg"], p["npxg"], p["xa"]))
    for i, d in enumerate(["2025-08-10", "2025-08-17", "2025-08-24", "2025-08-31"]):
        db.upsert_match(conn, {"league": "EPL", "season": "2526", "date": d, "home": "Alpha", "away": f"Opp{i}",
                               "status": "FT", "fthg": 1, "ftag": 0})


def test_fresh_absence_rule_and_lineups(conn):
    _seed_team(conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM matches ORDER BY date")]
    for mid in ids:                             # striker missed every game; centre back only the last one
        conn.execute("INSERT INTO absences VALUES (?, 'Alpha', 1, 'S. Striker', 'Knee')", (mid,))
    conn.execute("INSERT INTO absences VALUES (?, 'Alpha', 2, 'Centre Back', 'Suspended')", (ids[-1],))
    eff, shown = absences.match_absences(conn, ids[-1], "Alpha", "Opp3", "EPL", "EPL", "2526",
                                         allow_current=False, fresh_k=0)
    assert {p["player"] for p in eff["home"]["players"]} == {"Star Striker", "Centre Back"}
    eff, _ = absences.match_absences(conn, ids[-1], "Alpha", "Opp3", "EPL", "EPL", "2526",
                                     allow_current=False, fresh_k=3)
    assert {p["player"] for p in eff["home"]["players"]} == {"Centre Back"}   # long-term absence dropped
    # confirmed lineup: regulars not named are out, bench players count half
    conn.execute("DELETE FROM absences")
    lineup = {"starters": [{"name": "Keeper One"}, {"name": "Centre Back"}], "subs": ["Mid Field"]}
    conn.execute("INSERT INTO lineups VALUES (?, 'Alpha', ?)", (ids[-1], json.dumps(lineup)))
    absent, partial = absences.absent_names(conn, ids[-1], "Alpha", PLAYERS)
    assert absent == ["Star Striker"] and partial == {"Mid Field": 0.5}


# ---------------- ingest parsers

def test_clubelo_book_rating_and_last_match(conn):
    csv = ("Rank,Club,Country,Level,Elo,From,To\n"
           "None,Barcelona,ESP,1,1900,2025-08-01,2025-08-17\n"
           "None,Barcelona,ESP,1,1910,2025-08-18,2025-08-24\n"
           "None,Barcelona,ESP,1,1905,2025-08-25,2025-08-31\n")
    clubelo.store(conn, clubelo.parse_csv(csv))
    book = clubelo.EloBook(conn)
    assert book.rating("Barcelona", "2025-08-17") == 1900        # pre-match rating on match day
    assert book.rating("Barcelona", "2025-08-18") == 1910
    assert book.rating("Barcelona", "2025-07-01") is None
    assert book.last_match("Barcelona", "2025-08-27") == "2025-08-24"
    assert book.last_match("Barcelona", "2025-08-05") is None


def test_clubelo_resolve():
    clubs = {"Atletico": "ESP", "Real Madrid": "ESP", "Bayern": "GER", "Man City": "ENG", "Galatasaray": "TUR"}
    assert clubelo.resolve("Ath Madrid", clubs, "ESP") == "Atletico"          # alias table
    assert clubelo.resolve("Real Madrid", clubs, "ESP") == "Real Madrid"
    assert clubelo.resolve("Bayern Munich", clubs, "GER") == "Bayern"
    assert clubelo.resolve("Galatasaray", clubs) == "Galatasaray"
    assert clubelo.resolve("Nowhere Town", clubs, "ENG") is None


UNDERSTAT_JSON = {
    "dates": [
        {"id": "1", "isResult": True, "h": {"title": "Manchester City"}, "a": {"title": "Arsenal"},
         "goals": {"h": "2", "a": "1"}, "xG": {"h": "1.85", "a": "0.62"}, "datetime": "2025-08-16 17:30:00"},
        {"id": "2", "isResult": True, "h": {"title": "Unknown FC"}, "a": {"title": "Arsenal"},
         "goals": {"h": "0", "a": "0"}, "xG": {"h": "0.5", "a": "0.6"}, "datetime": "2025-08-20 17:30:00"},
        {"id": "3", "isResult": False, "h": {"title": "Arsenal"}, "a": {"title": "Manchester City"},
         "goals": {"h": None, "a": None}, "xG": {"h": None, "a": None}, "datetime": "2026-03-01 16:00:00"},
    ],
    "players": [{"id": "9", "player_name": "Erling Haaland", "games": "30", "time": "2600", "goals": "25",
                 "xG": "22.1", "npxG": "19.0", "assists": "5", "xA": "3.2", "shots": "110", "position": "F S",
                 "team_title": "Manchester City"}],
}


def test_understat_parse_both_formats():
    assert understat.parse_payload(json.dumps(UNDERSTAT_JSON))["dates"][0]["id"] == "1"
    blob = json.dumps(UNDERSTAT_JSON["dates"]).encode("unicode_escape").decode().replace('"', "\\x22")
    html = f"<script>var datesData = JSON.parse('{blob}');</script>"
    assert understat.parse_payload(html)["dates"][0]["xG"]["h"] == "1.85"
    with pytest.raises(understat.UnderstatUnavailable):
        understat.parse_payload("<html>nothing</html>")


def test_understat_store_skips_unmatched(conn):
    db.upsert_match(conn, {"league": "EPL", "season": "2526", "date": "2025-08-16", "home": "Man City",
                           "away": "Arsenal", "status": "FT", "fthg": 2, "ftag": 1, "source": "football-data"})
    res = understat.store(conn, "EPL", "2526", UNDERSTAT_JSON, {"Man City", "Arsenal"})
    assert res["matches"] == 1 and res["unmatched"] == ["Unknown FC"]
    r = conn.execute("SELECT * FROM matches").fetchall()
    assert len(r) == 1 and r[0]["xg_home"] == 1.85 and r[0]["source"] == "football-data"
    p = conn.execute("SELECT * FROM understat_players").fetchone()
    assert p["team"] == "Man City" and p["npxg"] == 19.0


def test_api_football_parsers():
    f = {"fixture": {"id": 7, "date": "2024-09-17T19:00:00+00:00", "referee": "S. Vincic, Slovenia",
                     "status": {"short": "FT"}},
         "teams": {"home": {"name": "Juventus"}, "away": {"name": "PSV Eindhoven"}},
         "goals": {"home": 3, "away": 1}, "league": {"round": "League Stage - 1"}}
    p = api_football.parse_fixture(f)
    assert p["referee"] == "S Vincic" and p["finished"] and (p["fthg"], p["ftag"]) == (3, 1)
    assert api_football.is_main_round("League Stage - 1")
    assert api_football.is_main_round("Round of 16") and api_football.is_main_round("Knockout Round Play-offs")
    assert not api_football.is_main_round("3rd Qualifying Round") and not api_football.is_main_round("Play-offs")
    inj = [{"team": {"name": "A"}, "player": {"id": 1, "name": "X", "type": "Missing Fixture", "reason": "Knee"},
            "fixture": {"id": 7}},
           {"team": {"name": "A"}, "player": {"id": 2, "name": "Y", "type": "Questionable", "reason": "Knock"},
            "fixture": {"id": 7}}]
    assert [i["player"] for i in api_football.parse_injuries(inj)] == ["X"]
    ev = [{"team": {"name": "A"}, "detail": "Penalty"}, {"team": {"name": "B"}, "detail": "Missed Penalty"},
          {"team": {"name": "A"}, "detail": "Normal Goal"},
          {"team": {"name": "A"}, "detail": "Penalty", "comments": "Penalty Shootout"}]
    assert api_football.count_penalties(ev, "A", "B") == (1, 1)


def test_api_football_cache_hit_returns_response_list(conn, monkeypatch):
    """Regression: a DB cache hit used to return the whole body instead of body['response']."""
    from matchpredictor.ingest.http_cache import cache_put
    key = "api_football:/injuries:[('fixture', 1)]"
    cache_put(conn, key, {"response": [{"a": 1}], "errors": []})
    monkeypatch.setenv("API_FOOTBALL_KEY", "x")
    assert api_football.Client(conn).injuries(1) == [{"a": 1}]


# ---------------- scope, ids, migrations

def test_display_scope():
    assert config.is_displayed("EPL", "Fulham", "Brentford")                 # full coverage
    assert config.is_displayed("UCL", "Galatasaray", "Club Brugge")
    assert config.is_displayed("LALIGA", "Getafe", "Barcelona")              # tracked club
    assert not config.is_displayed("LALIGA", "Getafe", "Sevilla")
    assert config.is_displayed("DFBPOKAL", "Bayern Munich", "Elversberg")
    assert not config.is_displayed("SERIEA", "Milan", "Roma")
    assert not config.is_displayed("NOPE", "Arsenal", "Chelsea")


def test_cup_ids_include_date_league_ids_do_not():
    a = db.match_id("UCL", "2627", "Arsenal", "Inter", "2026-10-21")
    b = db.match_id("UCL", "2627", "Arsenal", "Inter", "2027-03-10")
    assert a != b and "2026-10-21" in a
    assert db.match_id("EPL", "2627", "Arsenal", "Chelsea", "2026-10-21") == "EPL:2627:Arsenal:Chelsea"


def test_migration_adds_columns(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE matches (id TEXT PRIMARY KEY, league TEXT NOT NULL, season TEXT NOT NULL, "
              "date TEXT NOT NULL, kickoff_utc TEXT, home TEXT NOT NULL, away TEXT NOT NULL, status TEXT)")
    c.commit(); c.close()
    conn = db.connect(p)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    assert {"xg_home", "xg_away", "pens_home"} <= cols


def test_match_name_rules():
    known = {"Leverkusen", "Milan", "Inter", "Paris SG", "Paris FC"}
    assert config.match_name("Bayer Leverkusen", known) == "Leverkusen"
    assert config.match_name("Paris Saint-Germain", known) is None           # not swallowed by "Paris FC"
    assert config.match_name("Paris FC", known) == "Paris FC"
    assert config.match_name("Inter Milan", known) is None                    # ambiguous without the alias table
    assert config.canonical_team("Inter Milan", known) == "Inter"             # ...which resolves it


# ---------------- metrics and site cards

def test_exact_and_top3_metrics():
    rows = [
        {"p_home": .5, "p_draw": .3, "p_away": .2, "fthg": 1, "ftag": 0, "predicted_score": "1-0",
         "top_scores": ["1-0", "1-1", "0-0"]},
        {"p_home": .5, "p_draw": .3, "p_away": .2, "fthg": 0, "ftag": 0, "predicted_score": "1-0",
         "top_scores": ["1-0", "1-1", "0-0"]},
        {"p_home": .5, "p_draw": .3, "p_away": .2, "fthg": 3, "ftag": 3, "predicted_score": "1-0",
         "top_scores": ["1-0", "1-1", "0-0"]},
    ]
    m = tracking.score_predictions(rows)
    assert m["exact_score_hit"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["top3_score_hit"] == pytest.approx(2 / 3, abs=1e-3)
    assert set(m["calibration_1x2"]) == {"home", "draw", "away"}


def test_site_card_hits():
    r = {"id": "EPL:2627:A:B", "league": "EPL", "date": "2026-10-17", "kickoff_utc": "2026-10-17T14:00:00Z",
         "home": "A", "away": "B", "status": "FT", "referee": None, "fthg": 1, "ftag": 1, "predicted_at": "x"}
    pred = {"markets": {"1x2": {"home": 0.3, "draw": 0.4, "away": 0.3}, "correct_score": []},
            "predicted_score": "1-1", "top_scores": [{"score": "1-1", "prob": .12}, {"score": "1-0", "prob": .1},
                                                     {"score": "0-1", "prob": .09}],
            "xg": {"home": 1.1, "away": 1.0}, "referee": "M Oliver", "absences": []}
    c = site.card(r, pred)
    assert c["badge"] == "PL" and c["referee"] == "M Oliver"
    assert c["hit"] == {"result": True, "exact": True, "top3": True}
    assert site.card(dict(r, status="SCHEDULED", fthg=None, ftag=None), pred).get("hit") is None
