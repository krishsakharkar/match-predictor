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
    assert c["hit"] == {"result": True, "exact": True, "top3": True, "top6": None}   # only 3 scores given
    assert site.card(dict(r, status="SCHEDULED", fthg=None, ftag=None), pred).get("hit") is None


def test_current_config_prefers_adopted_tune(conn):
    from matchpredictor import backtest
    base = backtest.current_config(conn)
    assert base["xg_weight"] == config.settings()["model"]["xg_weight"]
    db.set_param(conn, "tuned", {"config": {"xg_weight": 0.25, "elo": True, "blend_weight": 0.75}}, "now")
    cfg = backtest.current_config(conn)
    assert cfg["xg_weight"] == 0.25 and cfg["elo"] is True and cfg["blend_weight"] == 0.75


def test_rolling_tune_needs_enough_matches(conn):
    from matchpredictor import backtest
    notes = []
    out = backtest.rolling_tune(conn, notes, today="2026-10-13")
    assert out["adopted"] is False and "skipped" in notes[0]


def test_shrink_moves_toward_or_away_from_even():
    from matchpredictor.predict import shrink
    lam, mu = 2.4, 0.8
    l1, m1 = shrink(lam, mu, 0.3)          # toward even
    assert l1 < lam and m1 > mu and l1 * m1 == pytest.approx(lam * mu)
    l2, m2 = shrink(lam, mu, -0.3)         # wider gap
    assert l2 > lam and m2 < mu
    assert shrink(lam, mu, 0.0) == (lam, mu)


def test_combine_applies_cross_league_shrink_by_category():
    info = dict(INFO, dc=None, joint=(2.0, 1.0), category="top")
    wide = combine(info, factors=NO, blend_weight=0.5, rest_coef=0, elo_params=PARAMS,
                   cross_shrink={"top": -0.3})
    plain = combine(info, factors=NO, blend_weight=0.5, rest_coef=0, elo_params=PARAMS, cross_shrink={})
    assert plain["lam"] == 2.0 and wide["lam"] > 2.0
    mixed = combine(dict(info, category="mixed"), factors=NO, blend_weight=0.5, rest_coef=0, elo_params=PARAMS,
                    cross_shrink={"top": -0.3})
    assert mixed["lam"] == 2.0


def test_extra_leagues_never_displayed_and_parse():
    from matchpredictor.ingest import football_data
    assert "POR" in config.extra_leagues() and not config.is_displayed("POR", "Sp Lisbon", "Benfica")
    text = ("Country,League,Season,Date,Time,Home,Away,HG,AG,Res\n"
            "Norway,Eliteserien,2025,12/04/2025,17:00,Viking,Bodo/Glimt,2,1,H\n"
            "Norway,Eliteserien,2019,12/04/2019,17:00,Viking,Molde,0,0,D\n")
    rows = football_data.parse_new_csv(text, "NOR", since="2021-07-01")
    assert len(rows) == 1 and rows[0]["fthg"] == 2 and rows[0]["season"] == "2425"
    assert config.canonical_team("Bodo/Glimt") == "Bodø/Glimt" and config.canonical_team("Sp Lisbon") == "Sporting CP"


def test_joint_model_extra_leagues_switch(monkeypatch):
    from matchpredictor import predict
    rng = np.random.default_rng(1)
    rows, d0 = [], pd.Timestamp("2025-08-01")
    for i in range(120):
        lg, a, b = [("EPL", "Arsenal", "Chelsea"), ("EPL", "Chelsea", "Arsenal"), ("POR", "Benfica", "Sp Braga"),
                    ("POR", "Sp Braga", "Benfica"), ("UCL", "Arsenal", "Benfica"), ("UCL", "Benfica", "Chelsea")][i % 6]
        rows.append({"league": lg, "home": a, "away": b, "date": d0 + pd.Timedelta(days=i), "status": "FT",
                     "fthg": float(rng.poisson(1.5)), "ftag": float(rng.poisson(1.1)), "xg_home": np.nan, "xg_away": np.nan})
    df = pd.DataFrame(rows)
    s = config.settings()
    monkeypatch.setattr(config, "settings", lambda: s)       # settings() re-reads the file on every call
    s["model"]["joint_extra_leagues"] = False
    off = predict.fit_joint(df, pd.Timestamp("2026-01-01").date())
    assert off is not None and not off.knows("Sp Braga")      # domestic-only extra-league club left out
    s["model"]["joint_extra_leagues"] = True
    on = predict.fit_joint(df, pd.Timestamp("2026-01-01").date())
    assert on.knows("Sp Braga")


def test_openfootball_parse_ninety_minute_scores_and_dates():
    from matchpredictor.ingest import openfootball
    txt = """▪ League, Matchday 1
  Tue Sep 16 2025
    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2 (0-0)
           PSV (NED)               v Royale Union Saint-Gilloise (BEL)  1-3 (0-2)
  Wed Jan 21
    21:00  Juventus FC (ITA)       v Galatasaray SK (TUR)     3-2 a.e.t. (3-0, 1-0)
           Paris Saint-Germain FC (FRA) v Arsenal FC (ENG)         4-3 pen. 1-1 a.e.t. (1-1, 0-1)
"""
    rows = openfootball.parse(txt, 2025)
    assert [(r["home"], r["away"], r["home_goals"], r["away_goals"]) for r in rows] == [
        ("Athletic Club", "Arsenal FC", 0, 2), ("PSV", "Royale Union Saint-Gilloise", 1, 3),
        ("Juventus FC", "Galatasaray SK", 3, 0), ("Paris Saint-Germain FC", "Arsenal FC", 1, 1)]
    assert rows[0]["kickoff_utc"] == "2025-09-16T16:45:00+00:00"          # CEST
    assert rows[1]["kickoff_utc"] == rows[0]["kickoff_utc"]                # time carries over
    assert rows[2]["date"] == "2026-01-21" and rows[2]["kickoff_utc"].startswith("2026-01-21T20:00")  # CET


def test_seed_names_are_canonical():
    import csv
    aliases = json.loads((config.CONFIG_DIR / "team_aliases.json").read_text())
    for path in sorted(config.ROOT.joinpath("data", "history").glob("*_results*.csv")):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            for t in (r["home"], r["away"]):
                assert aliases.get(t, t) == t, (path.name, t)


def _understat_match_payload():
    p = lambda i, name, pos, t, **k: {"id": str(i), "player_id": str(i), "player": name, "position": pos, "time": str(t),
                                      "goals": "0", "own_goals": "0", "assists": "0", "shots": "0", "key_passes": "0",
                                      "xG": "0", "xA": "0", "xGChain": "0", "xGBuildup": "0", "yellow_card": "0",
                                      "red_card": "0", **k}
    return {"rosters": {"h": {"1": p(1, "Vital N&#039;Simba", "DL", 90, goals="1", shots="2", xG="0.7"),
                              "2": p(2, "Sub One", "Sub", 20, yellow_card="1")},
                        "a": {"3": p(3, "Keeper", "GK", 90)}},
            "shots": {"h": [{"id": "10", "minute": "33", "player_id": "1", "player": "Vital N&#039;Simba",
                             "result": "Goal", "X": "0.9", "Y": "0.5", "xG": "0.6", "situation": "OpenPlay",
                             "shotType": "Head", "player_assisted": "Sub One", "lastAction": "Cross"}], "a": []}}


def test_understat_match_rows():
    players, shots = understat.match_rows(understat.parse_match(json.dumps(_understat_match_payload())),
                                          "EPL:2526:A:B", "A", "B")
    by = {p["player_id"]: p for p in players}
    assert by[1]["team"] == "A" and by[1]["player"] == "Vital N'Simba" and by[1]["started"] == 1
    assert by[1]["goals"] == 1 and by[1]["xg"] == 0.7
    assert by[2]["started"] == 0 and by[2]["yellow"] == 1 and by[3]["team"] == "B"
    assert shots[0]["team"] == "A" and shots[0]["result"] == "Goal" and shots[0]["assisted_by"] == "Sub One"


def test_detail_archive_round_trip(tmp_path, monkeypatch):
    from matchpredictor import detail
    monkeypatch.setattr(understat, "MATCH_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "99.json").write_text(json.dumps(_understat_match_payload()))
    main = db.connect(":memory:")
    db.upsert_match(main, {"league": "EPL", "season": "2526", "date": "2025-09-01", "home": "A", "away": "B",
                           "status": "FT", "fthg": 1, "ftag": 0, "understat_id": "99", "source": "understat"})
    d1 = detail.connect(tmp_path / "d1.db")
    out = detail.sync_understat(main, d1, ["2526"], [], pause=0)
    assert out == {"todo": 1, "stored": 1, "downloaded": 0, "failed": 0}       # served from cache
    assert detail.sync_understat(main, d1, ["2526"], [], pause=0)["todo"] == 0  # nothing left to do
    counts = detail.export_archive(d1, "EPL", "2526", tmp_path / "arch")
    assert counts == {"matches": 1, "players": 3, "shots": 1}
    first = (tmp_path / "arch" / "EPL_2526.players.csv.gz").read_bytes()
    detail.export_archive(d1, "EPL", "2526", tmp_path / "arch")
    assert (tmp_path / "arch" / "EPL_2526.players.csv.gz").read_bytes() == first   # deterministic bytes
    d2 = detail.connect(tmp_path / "d2.db")
    assert detail.load_archive(d2, tmp_path / "arch") == {"EPL_2526": 1}
    row = d2.execute("SELECT player, goals, xg, started FROM player_matches WHERE player_id=1").fetchone()
    assert tuple(row) == ("Vital N'Simba", 1, 0.7, 1)
    assert d2.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 1


class _FakeAF:
    """Canned API-Football responses for the bulk history test."""
    def __init__(self):
        self.calls = []

    def fetch(self, path, params):
        self.calls.append((path, dict(params)))
        cov = {"fixtures": {"events": True, "lineups": True, "statistics_fixtures": True, "statistics_players": True},
               "injuries": True, "players": True}
        fx = lambda fid, season: {
            "fixture": {"id": fid, "date": f"{season}-09-01T15:00:00+00:00", "referee": "M. Oliver",
                        "venue": {"name": "Emirates"}, "status": {"short": "FT"}},
            "league": {"id": 39, "season": season, "round": "Regular Season - 3"},
            "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 50, "name": "Manchester City"}},
            "goals": {"home": 2, "away": 1}, "score": {"halftime": {"home": 1, "away": 0}}}
        if path == "/teams":
            return {"response": [{"team": {"id": 42, "name": "Arsenal", "country": "England"}},
                                 {"team": {"id": 50, "name": "Manchester City", "country": "England"}}]}
        if path == "/leagues":
            return {"response": [{"league": {"id": 39, "name": "Premier League", "type": "League"},
                                  "country": {"name": "England"}, "seasons": [{"year": params["season"], "coverage": cov}]},
                                 {"league": {"id": 667, "name": "Friendlies Clubs", "type": "Cup"}, "seasons": []}]}
        if path == "/fixtures" and "ids" in params:
            out = []
            for fid in map(int, params["ids"].split("-")):
                f = fx(fid, 2025 if fid < 2000 else 2026)
                f["statistics"] = [{"team": {"id": 42}, "statistics": [{"type": "Corner Kicks", "value": 7},
                                                                      {"type": "expected_goals", "value": "1.85"}]}]
                f["lineups"] = [{"team": {"id": 42}, "formation": "4-3-3", "coach": {"id": 1, "name": "M. Arteta"},
                                 "startXI": [{"player": {"id": 7, "name": "B. Saka", "number": 7, "pos": "F", "grid": "4:3"}}],
                                 "substitutes": [{"player": {"id": 19, "name": "L. Trossard", "number": 19, "pos": "F"}}]}]
                f["players"] = [{"team": {"id": 42}, "players": [{"player": {"id": 7, "name": "B. Saka"}, "statistics": [{
                    "games": {"minutes": 90, "number": 7, "position": "F", "rating": "8.1", "captain": False,
                              "substitute": False}, "shots": {"total": 4, "on": 2}, "goals": {"total": 1, "assists": 1},
                    "passes": {"total": 40, "key": 3, "accuracy": "31"}, "cards": {"yellow": 1, "red": 0},
                    "penalty": {"commited": None, "scored": 0}}]}]}]
                f["events"] = [{"time": {"elapsed": 23}, "team": {"id": 42}, "player": {"id": 7, "name": "B. Saka"},
                                "assist": {"id": 19, "name": "L. Trossard"}, "type": "Goal", "detail": "Normal Goal"}]
                out.append(f)
            return {"response": out}
        if path == "/fixtures":
            return {"response": [fx(1001 if params["season"] == 2025 else 2001, params["season"])]}
        if path == "/injuries":
            page = params.get("page", 1)
            return {"paging": {"current": page, "total": 2}, "response": [
                {"player": {"id": 100 + page, "name": f"P{page}", "type": "Missing Fixture", "reason": "Knee Injury"},
                 "team": {"id": 42}, "fixture": {"id": 1001 if params["season"] == 2025 else 2001,
                                                 "date": f"{params['season']}-09-01"}}]}
        if path == "/players":
            return {"paging": {"current": 1, "total": 1}, "response": [
                {"player": {"id": 7, "name": "B. Saka", "birth": {"date": "2001-09-05"}, "nationality": "England"},
                 "statistics": [{"team": {"id": params["team"]}, "league": {"id": 39},
                                 "games": {"appearences": 30, "lineups": 28, "minutes": 2500, "position": "Attacker",
                                           "rating": "7.4"}, "goals": {"total": 12, "assists": 9}}]}]}
        if path == "/transfers":
            return {"response": [{"player": {"id": 7, "name": "B. Saka"}, "transfers": [
                {"date": "2018-07-01", "type": "N/A", "teams": {"in": {"id": 42, "name": "Arsenal"},
                                                                "out": {"id": 9, "name": "Arsenal U18"}}}]}]}
        raise AssertionError(path)


def test_api_football_history_backfill_resume_and_archive(tmp_path):
    from matchpredictor import detail
    from matchpredictor.ingest import api_football_history as afh
    teams = {"Arsenal": {"competitions": ["EPL"]}, "Man City": {"competitions": ["EPL"]}}
    d1 = detail.connect(tmp_path / "d1.db")
    fake = _FakeAF()
    out = afh.Backfill(None, d1, [], client=fake).run(teams, current=2026, first=2025)
    assert out["complete"] and out["teams"] == 2 and out["detail"] == 2
    assert {r[0] for r in d1.execute("SELECT our_name FROM af_teams WHERE our_name IS NOT NULL")} == {"Arsenal", "Man City"}
    assert d1.execute("SELECT COUNT(*) FROM af_competitions WHERE name LIKE 'Friendl%'").fetchone()[0] == 0
    pm = d1.execute("SELECT minutes, rating, shots, goals, assists, yellow FROM af_player_match WHERE fixture_id=1001").fetchone()
    assert tuple(pm) == (90, 8.1, 4, 1, 1, 1)
    assert d1.execute("SELECT value FROM af_team_stats WHERE fixture_id=1001 AND stat='expected_goals'").fetchone()[0] == "1.85"
    assert d1.execute("SELECT COUNT(*) FROM af_injuries WHERE season=2025").fetchone()[0] == 2        # both pages
    assert d1.execute("SELECT started FROM af_lineups WHERE player_id=19").fetchone()[0] == 0
    assert d1.execute("SELECT type, minute FROM af_events").fetchone()[:] == ("Goal", 23)
    # a second run fetches only the current season again
    fake.calls.clear()
    afh.Backfill(None, d1, [], client=fake).run(teams, current=2026, first=2025)
    assert all(p.get("season") in (None, 2026) for path, p in fake.calls if path != "/transfers"), fake.calls
    # archive: finished season only, reload into a fresh db
    files = detail.export_af(d1, finished_before=2026, out_dir=tmp_path / "af")
    assert files["af_player_match_2025"] == 1 and "af_player_match_2026" not in files
    d2 = detail.connect(tmp_path / "d2.db")
    detail.load_af(d2, tmp_path / "af")
    assert d2.execute("SELECT rating FROM af_player_match WHERE fixture_id=1001").fetchone()[0] == 8.1
    fake.calls.clear()
    afh.Backfill(None, d2, [], client=fake).run(teams, current=2026, first=2025)
    assert not any(p.get("season") == 2025 for _, p in fake.calls)       # archived season not re-fetched


def test_warehouse_merges_sources(tmp_path):
    from matchpredictor import detail, warehouse
    main = db.connect(tmp_path / "m.db")
    for row in ({"league": "SERIEA", "season": "2526", "date": "2025-09-20", "home": "Inter", "away": "Napoli",
                 "status": "FT", "fthg": 2, "ftag": 1, "hs": 15, "as_": 9, "hc": 6, "ac": 3, "xg_home": 1.9,
                 "xg_away": 0.8, "source": "football-data"},
                {"league": "EPL", "season": "2526", "date": "2025-09-21", "home": "Arsenal", "away": "Chelsea",
                 "status": "FT", "fthg": 1, "ftag": 1, "understat_id": "5", "source": "football-data"}):
        db.upsert_match(main, row)
    main.commit()
    d = detail.connect(tmp_path / "d.db")
    d.executemany("INSERT INTO af_competitions VALUES (?,?,?,?,?,1,1,1,1,1,1)",
                  [(135, 2025, "Serie A", "League", "Italy"), (39, 2025, "Premier League", "League", "England"),
                   (137, 2025, "Coppa Italia", "Cup", "Italy"), (500, 2025, "Veikkausliiga", "League", "Finland")])
    d.executemany("INSERT INTO af_teams (team_id, name, our_name, country) VALUES (?,?,?,?)",
                  [(505, "Inter", "Inter", "Italy"), (492, "Napoli", None, "Italy"), (42, "Arsenal", "Arsenal", "England"),
                   (49, "Chelsea", None, "England"), (900, "Inter Turku", None, "Finland"), (901, "Lecce", None, "Italy")])
    fx = "INSERT INTO af_fixtures (fixture_id, league_id, season, date, status, home_id, away_id, home, away, " \
         "home_goals, away_goals, et_home, et_away, pen_home, pen_away) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    d.executemany(fx, [(1, 135, 2025, "2025-09-20", "FT", 505, 492, "Inter", "Napoli", 2, 1, None, None, None, None),
                       (2, 137, 2025, "2026-01-14", "AET", 505, 901, "Inter", "Lecce", 3, 2, 1, 0, None, None),
                       (3, 500, 2025, "2025-09-20", "FT", 900, 901, "Inter Turku", "Lecce", 0, 0, None, None, None, None),
                       (4, 39, 2025, "2025-09-21", "FT", 42, 49, "Arsenal", "Chelsea", 1, 1, None, None, None, None)])
    d.executemany("INSERT INTO af_team_stats VALUES (?,?,?,?,?)",
                  [(1, 2025, 505, "Total Shots", "99"), (1, 2025, 505, "Ball Possession", "61%"),
                   (2, 2025, 505, "Corner Kicks", "8"), (2, 2025, 505, "expected_goals", "2.40")])
    d.execute("INSERT INTO af_player_match (fixture_id, season, team_id, player_id, player, minutes, substitute, rating, "
              "goals) VALUES (4, 2025, 42, 7, 'B. Saka', 90, 0, 7.9, 1)")
    d.execute("INSERT INTO af_player_match (fixture_id, season, team_id, player_id, player, minutes, substitute) "
              "VALUES (4, 2025, 42, 30, 'Unused Sub', NULL, 1)")
    d.execute("INSERT INTO detail_matches VALUES ('EPL:2526:Arsenal:Chelsea','EPL','2526','2025-09-21','Arsenal',"
              "'Chelsea','understat','5','x')")
    d.execute("INSERT INTO player_matches (match_id, team, side, player_id, player, started, minutes, goals, xg, xa) "
              "VALUES ('EPL:2526:Arsenal:Chelsea', 'Arsenal', 'h', 1111, 'Bukayo Saka', 1, 90, 1, 0.62, 0.1)")
    d.commit()
    out = warehouse.build(tmp_path / "m.db", tmp_path / "d.db", tmp_path / "w.db")
    w = sqlite3.connect(tmp_path / "w.db")
    # API fixture merged onto our league match: football-data stats kept, possession added from the API
    assert w.execute("SELECT af_fixture_id FROM matches WHERE match_id='SERIEA:2526:Inter:Napoli'").fetchone()[0] == 1
    st = w.execute("SELECT shots, corners, possession, xg, stats_source FROM team_match_stats WHERE "
                   "match_id='SERIEA:2526:Inter:Napoli' AND team='Inter'").fetchone()
    assert tuple(st) == (15, 6, 61, 1.9, "football-data")
    # API-only cup match: added under a dated id, 90-minute score (extra-time goal removed), API xG
    cup = w.execute("SELECT home_goals, away_goals, home_xg, xg_source FROM matches WHERE match_id='COPPAITALIA:2026-01-14:Inter:Lecce'").fetchone()
    assert tuple(cup) == (2, 2, 2.4, "api-football")
    # a Finnish 'Inter' is not Inter
    assert w.execute("SELECT home FROM matches WHERE af_fixture_id=3").fetchone()[0] == "Inter Turku"
    # one player across both sources, full name kept, xG from Understat, rating from the API
    p = w.execute("SELECT player_id, name, understat_player_id FROM players WHERE af_player_id=7").fetchone()
    assert tuple(p) == ("af:7", "Bukayo Saka", 1111)
    pm = w.execute("SELECT player_id, rating, xg, goals, sources FROM player_match_stats WHERE team='Arsenal'").fetchall()
    assert [tuple(r) for r in pm] == [("af:7", 7.9, 0.62, 1, "api-football,understat")]   # unused sub dropped
    assert out["checks"][0].endswith(": 0") and out["checks"][1].endswith(": 0")


def test_player_name_matching():
    from matchpredictor.warehouse import same_player
    assert same_player("B. Saka", "Bukayo Saka") and same_player("Rodri", "Rodrigo Hernández Rodri")
    assert same_player("Vinícius Júnior", "Vinicius Junior")
    assert not same_player("B. Saka", "J. Saka") and not same_player("Gabriel Jesus", "Gabriel Martinelli")


def test_api_football_history_retries_dropped_connections(tmp_path, monkeypatch):
    from matchpredictor import detail
    from matchpredictor.ingest import api_football_history as afh
    from matchpredictor.ingest.api_football import ApiFootballUnavailable
    monkeypatch.setattr(afh.time, "sleep", lambda s: None)
    fake, fails = _FakeAF(), {"n": 2}
    real = fake.fetch

    def flaky(path, params):
        if fails["n"]:
            fails["n"] -= 1
            fake.disabled_reason = "network error: ChunkedEncodingError"
            raise ApiFootballUnavailable(fake.disabled_reason)
        return real(path, params)
    fake.fetch, fake.disabled_reason = flaky, None
    out = afh.Backfill(None, detail.connect(tmp_path / "d.db"), [], client=fake).run(
        {"Arsenal": {"competitions": ["EPL"]}}, current=2026, first=2026)
    assert out["complete"] and fails["n"] == 0


def test_extra_league_names_never_fuzzy_matched(conn, monkeypatch):
    from matchpredictor.ingest import football_data
    db.upsert_match(conn, {"league": "LIGUE1", "season": "2526", "date": "2025-08-17", "home": "Angers",
                           "away": "Paris FC", "status": "FT", "fthg": 1, "ftag": 0, "source": "football-data"})
    # a v1 row stored under a fuzzy-matched name
    db.upsert_match(conn, {"league": "GRE", "season": "2526", "date": "2025-09-01", "home": "Paris FC",
                           "away": "PAOK", "status": "FT", "fthg": 1, "ftag": 1, "source": "football-data"})
    csv = ("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
           "G1,01/09/2025,18:00,Aris,PAOK,1,1,D\nG1,02/09/2025,18:00,Rangers,Sp Lisbon,2,0,H\n")
    monkeypatch.setattr(config, "extra_leagues", lambda: {"GRE": {"name": "Greek Super League", "football_data_code": "G1"}})
    monkeypatch.setattr(football_data, "_get", lambda url: csv)
    monkeypatch.setattr(football_data, "season_complete", lambda *a: False)
    football_data.update_extra(conn, "2526", [])
    teams = {r[0] for r in conn.execute("SELECT home FROM matches WHERE league='GRE' UNION SELECT away FROM matches WHERE league='GRE'")}
    assert teams == {"Aris", "PAOK", "Rangers", "Sporting CP"}          # aliases apply, fuzzy matches don't
    assert db.get_param(conn, "extra_names_version") == football_data.EXTRA_NAMES_VERSION


def test_api_football_team_ids_country_checked_and_repaired(tmp_path):
    from matchpredictor import detail
    from matchpredictor.ingest import api_football_history as afh
    d = detail.connect(tmp_path / "d.db")
    # state left by the first backfill: a UCL qualifier from Andorra linked to Inter, next to the real Inter
    d.executemany("INSERT INTO af_teams (team_id, name, our_name, country) VALUES (?,?,?,?)",
                  [(505, "Inter", "Inter", "Italy"), (3342, "Inter Club d'Escaldes", "Inter", "Andorra"),
                   (560, "Slavia Praha", "Slavia Praha", "Czech-Republic")])
    teams = {"Inter": {"competitions": ["UCL"], "country": "Italy"},
             "Slavia Praha": {"competitions": ["UCL"], "country": "Czechia"}}
    notes = []
    ids = afh.Backfill(None, d, notes, client=object()).team_ids(teams, 2026)
    assert ids == {"Inter": 505, "Slavia Praha": 560}                     # Czech-Republic == Czechia
    assert any("Escaldes" in n for n in notes)

    class _Teams:            # a fresh lookup: the Andorran club must not be taken for Inter
        def fetch(self, path, params):
            return {"response": [{"team": {"id": 3342, "name": "Inter Club d'Escaldes", "country": "Andorra"}},
                                 {"team": {"id": 505, "name": "Inter", "country": "Italy"}}]}
    d2 = detail.connect(tmp_path / "d2.db")
    assert afh.Backfill(None, d2, [], client=_Teams()).team_ids({"Inter": teams["Inter"]}, 2026) == {"Inter": 505}


def test_audit_runs_on_a_built_warehouse(tmp_path):
    from matchpredictor import audit, detail, warehouse
    db.connect(tmp_path / "m.db").close()
    detail.connect(tmp_path / "d.db").close()
    warehouse.build(tmp_path / "m.db", tmp_path / "d.db", tmp_path / "w.db")
    res, md = audit.run(tmp_path / "w.db", tmp_path / "d.db")
    assert res and "| Check |" in md and all(r["ok"] <= r["total"] or r["total"] == 0 for r in res)


def test_warehouse_aligns_clubs_on_busy_matchdays(tmp_path):
    """Seeds spell 'CFR Cluj', API-Football 'CFR 1907 Cluj'; every matchday has two 1-0 games, so only the
    opponent (whose name is known) tells them apart. No duplicate match may come out of it."""
    from matchpredictor import detail, warehouse
    main = db.connect(tmp_path / "m.db")
    games = [("2021-09-16", "FK Jablonec", "CFR Cluj", "Gent", "Flora"), ("2021-10-21", "CFR Cluj", "AZ Alkmaar", "Flora", "Gent"),
             ("2021-11-25", "Randers FC", "CFR Cluj", "Partizan", "Flora"), ("2021-12-09", "CFR Cluj", "FK Jablonec", "Gent", "Partizan")]
    for d_, h1, a1, h2, a2 in games:
        for h, a in ((h1, a1), (h2, a2)):
            db.upsert_match(main, {"league": "UECL", "season": "2122", "date": d_, "home": h, "away": a,
                                   "status": "FT", "fthg": 1, "ftag": 0, "source": "seed"})
    main.commit()
    d = detail.connect(tmp_path / "d.db")
    d.execute("INSERT INTO af_competitions VALUES (848, 2021, 'UEFA Europa Conference League', 'Cup', 'World',1,1,1,1,1,1)")
    ids = {"CFR 1907 Cluj": 2246, "FK Jablonec": 1, "AZ Alkmaar": 2, "Randers FC": 3, "Gent": 4, "Flora Tallinn": 5,
           "Partizan": 6}
    api = {"CFR Cluj": "CFR 1907 Cluj", "Flora": "Flora Tallinn"}
    d.executemany("INSERT INTO af_teams (team_id, name) VALUES (?,?)", [(v, k) for k, v in ids.items()])
    n = 0
    for d_, h1, a1, h2, a2 in games:
        for h, a in ((h1, a1), (h2, a2)):
            h, a, n = api.get(h, h), api.get(a, a), n + 1
            d.execute("INSERT INTO af_fixtures (fixture_id, league_id, season, date, status, home_id, away_id, home, away,"
                      " home_goals, away_goals) VALUES (?,848,2021,?,'FT',?,?,?,?,1,0)", (n, d_, ids[h], ids[a], h, a))
    d.commit()
    out = warehouse.build(tmp_path / "m.db", tmp_path / "d.db", tmp_path / "w.db")
    w = sqlite3.connect(tmp_path / "w.db")
    assert w.execute("SELECT COUNT(*), COUNT(af_fixture_id) FROM matches").fetchone() == (8, 8)
    assert w.execute("SELECT af_team_id FROM teams WHERE team='CFR Cluj'").fetchone()[0] == 2246
    assert any(c.startswith("possible duplicate") and c.endswith(": 0") for c in out["checks"])
