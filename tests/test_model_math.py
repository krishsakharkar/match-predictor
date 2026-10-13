import math

import numpy as np
import pytest
from scipy.stats import poisson

from matchpredictor.models import counts, dixon_coles as dc, injuries, markets, props
from matchpredictor import value


# ---------- score matrix & derived markets ----------

@pytest.mark.parametrize("lam,mu,rho", [(1.5, 1.1, -0.1), (0.3, 0.2, 0.05), (3.2, 0.6, -0.2), (1.0, 1.0, 0.0)])
def test_score_matrix_sums_to_one(lam, mu, rho):
    m = dc.score_matrix(lam, mu, rho)
    assert m.sum() == pytest.approx(1.0, abs=1e-12)
    assert (m >= 0).all()


def test_rho_zero_is_independent_poisson():
    m = dc.score_matrix(1.4, 1.2, 0.0, max_goals=15)
    g = np.arange(16)
    expected = np.outer(poisson.pmf(g, 1.4), poisson.pmf(g, 1.2))
    np.testing.assert_allclose(m, expected / expected.sum(), atol=1e-12)


def test_negative_rho_inflates_draws_00_11():
    base = dc.score_matrix(1.3, 1.1, 0.0)
    adj = dc.score_matrix(1.3, 1.1, -0.15)
    assert adj[0, 0] > base[0, 0] and adj[1, 1] > base[1, 1]
    assert adj[1, 0] < base[1, 0] and adj[0, 1] < base[0, 1]


@pytest.mark.parametrize("lam,mu,rho", [(1.6, 1.0, -0.1), (0.9, 2.1, 0.03)])
def test_market_probabilities_are_complementary(lam, mu, rho):
    m = dc.score_matrix(lam, mu, rho)
    mk = markets.goal_markets(m, [1.5, 2.5, 3.5], [0.5, 1.5, 2.5])
    assert sum(mk["1x2"].values()) == pytest.approx(1.0)
    for line in ("1.5", "2.5", "3.5"):
        assert sum(mk["totals"][line].values()) == pytest.approx(1.0)
    for side in ("home", "away"):
        for line in ("0.5", "1.5", "2.5"):
            assert sum(mk["team_totals"][side][line].values()) == pytest.approx(1.0)
    assert sum(mk["btts"].values()) == pytest.approx(1.0)
    dcm = mk["double_chance"]
    assert dcm["1x"] == pytest.approx(mk["1x2"]["home"] + mk["1x2"]["draw"])
    assert dcm["1x"] + dcm["x2"] + dcm["12"] == pytest.approx(2.0)
    # totals are monotone in the line
    assert mk["totals"]["1.5"]["over"] > mk["totals"]["2.5"]["over"] > mk["totals"]["3.5"]["over"]


def test_correct_scores_sorted_top5():
    m = dc.score_matrix(1.4, 1.0, -0.1)
    cs = markets.correct_scores(m, 5)
    assert len(cs) == 5
    probs = [c["prob"] for c in cs]
    assert probs == sorted(probs, reverse=True)
    assert cs[0]["score"] == markets.most_likely_score(m)


def test_btts_matches_independent_closed_form():
    lam, mu = 1.5, 1.2
    m = dc.score_matrix(lam, mu, 0.0, max_goals=20)
    expected = (1 - math.exp(-lam)) * (1 - math.exp(-mu))
    assert markets.btts(m)["yes"] == pytest.approx(expected, abs=1e-9)


def test_expected_goals_recovered_from_matrix():
    m = dc.score_matrix(1.7, 0.9, 0.0, max_goals=20)
    g = np.arange(21)
    assert (m.sum(axis=1) * g).sum() == pytest.approx(1.7, abs=1e-6)
    assert (m.sum(axis=0) * g).sum() == pytest.approx(0.9, abs=1e-6)


# ---------- fair odds & vig ----------

@pytest.mark.parametrize("p", [0.01, 0.2, 0.5, 0.73, 0.999])
def test_fair_odds_inversion(p):
    o = markets.fair_odds(p)
    assert markets.implied_prob(o) == pytest.approx(p)
    assert o * p == pytest.approx(1.0)


def test_fair_odds_zero_prob():
    assert markets.fair_odds(0.0) is None


def test_attach_fair_odds_structure():
    m = dc.score_matrix(1.2, 1.2, -0.05)
    mk = markets.goal_markets(m, [2.5], [0.5])
    fo = markets.attach_fair_odds(mk)
    assert fo["1x2"]["home"] == pytest.approx(1 / mk["1x2"]["home"])
    assert fo["totals"]["2.5"]["over"] == pytest.approx(1 / mk["totals"]["2.5"]["over"])


@pytest.mark.parametrize("odds", [[1.91, 1.91], [2.1, 3.2, 3.6], [1.25, 5.5, 11.0]])
def test_remove_vig_sums_to_one(odds):
    fair = value.remove_vig(odds)
    assert sum(fair) == pytest.approx(1.0)
    assert value.overround(odds) > 0
    # ordering preserved: shorter price -> higher probability
    assert np.argsort(fair).tolist() == np.argsort([-o for o in odds]).tolist()


def test_remove_vig_no_margin_is_identity():
    assert value.remove_vig([2.0, 2.0]) == pytest.approx([0.5, 0.5])


def test_remove_vig_rejects_bad_odds():
    with pytest.raises(ValueError):
        value.remove_vig([1.0, 3.0])


# ---------- Kelly / value ----------

def test_kelly_known_value():
    # p=0.6 at evens: f* = (0.6*1 - 0.4)/1 = 0.2
    assert value.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    assert value.kelly_fraction(0.4, 2.0) == 0.0


def test_quarter_kelly_capped():
    assert value.stake(0.6, 2.0, 0.25, 0.02) == pytest.approx(0.02)       # 0.05 -> capped
    assert value.stake(0.52, 2.0, 0.25, 0.02) == pytest.approx(0.01)      # 0.04*0.25


def test_assess_flags_edge():
    cfg = {"min_edge": 0.05, "kelly_fraction": 0.25, "max_stake": 0.02, "min_odds": 1.3, "max_odds": 10}
    hit = value.assess(0.55, 2.0, 0.48, cfg)
    assert hit["is_value"] and hit["stake"] > 0
    miss = value.assess(0.49, 2.0, 0.48, cfg)
    assert not miss["is_value"] and miss["stake"] == 0
    # edge vs fair market but negative EV at the vigged price -> not value
    neg = value.assess(0.50, 1.9, 0.47, cfg)
    assert neg["edge"] > 0.05 and neg["ev"] < 0 and not neg["is_value"]
    # implausibly large edge -> suspect, not staked
    sus = value.assess(0.30, 6.0, 0.16, dict(cfg, max_edge=0.35))
    assert sus["suspect"] and not sus["is_value"] and sus["stake"] == 0
    assert value.assess(0.30, 6.0, 0.16, cfg)["is_value"]          # no cap configured


# ---------- Dixon-Coles fitting ----------

def _simulate(n_rounds=6, seed=0):
    rng = np.random.default_rng(seed)
    teams = [f"T{i}" for i in range(10)]
    att = rng.normal(0, 0.3, len(teams))
    att -= att.mean()
    dfn = rng.normal(0, 0.25, len(teams))
    dfn -= dfn.mean()
    c, h = 0.1, 0.3
    H, A, hg, ag = [], [], [], []
    for _ in range(n_rounds):
        for i in range(len(teams)):
            for j in range(len(teams)):
                if i == j:
                    continue
                H.append(teams[i]); A.append(teams[j])
                hg.append(rng.poisson(np.exp(c + h + att[i] + dfn[j])))
                ag.append(rng.poisson(np.exp(c + att[j] + dfn[i])))
    return teams, att, dfn, c, h, H, A, np.array(hg), np.array(ag)


def test_fit_recovers_parameters():
    teams, att, dfn, c, h, H, A, hg, ag = _simulate()
    model = dc.fit(H, A, hg, ag, ridge=0.01)
    assert model.converged
    assert model.home_adv == pytest.approx(h, abs=0.08)
    order = [model.index[t] for t in teams]
    assert np.corrcoef(model.attack[order], att)[0, 1] > 0.9
    assert np.corrcoef(model.defence[order], dfn)[0, 1] > 0.85
    assert abs(model.rho) < 0.15


def test_fit_gradient_matches_finite_difference():
    teams, att, dfn, c, h, H, A, hg, ag = _simulate(n_rounds=1, seed=3)
    w = dc.time_weights(np.linspace(0, 300, len(hg)), 0.003)
    negll, ctx = dc.make_objective(H, A, hg, ag, w, ridge=0.3, prior_attack={"T1": -0.2})
    rng = np.random.default_rng(7)
    p = ctx["x0"] + rng.normal(0, 0.1, len(ctx["x0"]))
    p[-1] = -0.08                                  # rho
    f0, g = negll(p)
    eps = 1e-6
    num = np.array([(negll(p + eps * e)[0] - negll(p - eps * e)[0]) / (2 * eps)
                    for e in np.eye(len(p))])
    np.testing.assert_allclose(g, num, rtol=1e-4, atol=1e-4)


def test_time_weights_decay():
    w = dc.time_weights(np.array([0, 365, 730]), 0.0019)
    assert w[0] == 1.0 and w[0] > w[1] > w[2]
    assert w[1] == pytest.approx(math.exp(-0.0019 * 365))


def test_promoted_prior_pulls_unknown_team_down():
    teams, att, dfn, c, h, H, A, hg, ag = _simulate(n_rounds=2)
    # One match for a brand-new team: its rating should sit near its prior.
    H2 = H + ["NEW"]; A2 = A + ["T0"]
    hg2 = np.append(hg, 1); ag2 = np.append(ag, 1)
    m = dc.fit(H2, A2, hg2, ag2, ridge=2.0, prior_attack={"NEW": -0.3}, prior_defence={"NEW": 0.3})
    i = m.index["NEW"]
    assert m.attack[i] < 0 and m.defence[i] > 0


# ---------- count models ----------

@pytest.mark.parametrize("alpha", [0.0, 0.15])
def test_count_pmf_sums_and_mean(alpha):
    p = counts.count_pmf(5.2, alpha, max_k=60)
    assert p.sum() == pytest.approx(1.0)
    assert (np.arange(61) * p).sum() == pytest.approx(5.2, rel=1e-3)


def test_convolved_lines_complementary():
    p = counts.convolve(counts.count_pmf(5.5, 0.1), counts.count_pmf(4.3, 0.1))
    lp = counts.line_probs(p, [8.5, 9.5, 10.5])
    for v in lp.values():
        assert v["over"] + v["under"] == pytest.approx(1.0)
    assert (np.arange(len(p)) * p).sum() == pytest.approx(9.8, rel=1e-3)


def test_count_model_fit_and_overdispersion():
    rng = np.random.default_rng(1)
    teams = [f"T{i}" for i in range(8)]
    H, A, yh, ya = [], [], [], []
    for _ in range(8):
        for i in teams:
            for j in teams:
                if i != j:
                    H.append(i); A.append(j)
                    # NB with mean 5, alpha 0.2 -> r=5, p=0.5
                    yh.append(rng.negative_binomial(5, 5 / (5 + 5.5)))
                    ya.append(rng.negative_binomial(5, 5 / (5 + 4.5)))
    m = counts.fit_count_model("corners", H, A, yh, ya, ridge=1.0)
    eh, ea = m.expected("T0", "T1")
    assert 4.5 < eh < 6.5 and 3.6 < ea < 5.4
    assert 0.08 < m.alpha < 0.35


def test_referee_factor_shrinks_toward_one():
    H = ["A", "B"] * 50
    A = ["B", "A"] * 50
    yh = np.full(100, 2.0)
    ya = np.full(100, 2.0)
    refs = ["Strict"] * 10 + ["Normal"] * 90
    yh[:10] = 4.0
    m = counts.fit_count_model("cards", H, A, yh, ya, referees=refs)
    assert m.referee_factor["Strict"] > 1.1
    assert m.referee_factor["Strict"] < 6.0 / (4.2)    # shrunk below the raw ratio
    assert m.referee_factor["Normal"] < 1.0


# ---------- injuries & props ----------

def test_absence_multiplier():
    assert injuries.absence_multiplier([]) == 1.0
    assert injuries.absence_multiplier([0.3], retention=0.5) == pytest.approx(0.85)
    assert injuries.absence_multiplier([0.4, 0.3, 0.3], retention=0.5, floor=0.75) == 0.75


def test_attacking_share():
    assert injuries.attacking_share(10, 60, 50, 400) == pytest.approx(0.5 * 0.2 + 0.5 * 0.15)


def test_player_props_bounds_and_out_status():
    players = [
        {"player": "Striker", "apps": 30, "minutes": 2500, "goals": 20, "shots": 100, "sot": 45},
        {"player": "Mid", "apps": 30, "minutes": 2600, "goals": 5, "shots": 40, "sot": 15},
    ]
    out = props.player_props(players, team_xg=1.8, team_sot=5.5, team_goals=70, team_shots=500,
                             team_sot_total=180, team_minutes=38 * 90)
    assert out[0]["player"] == "Striker"
    for r in out:
        assert 0 < r["anytime_scorer"] < 1 and 0 < r["sot_1plus"] < 1
        assert r["anytime_scorer_fair"] == pytest.approx(1 / r["anytime_scorer"], rel=1e-2)
    out2 = props.player_props(players, 1.8, 5.5, 70, 500, 180, 38 * 90, statuses={"Striker": "out"})
    striker = [r for r in out2 if r["player"] == "Striker"][0]
    assert striker["anytime_scorer"] == 0.0 and striker["anytime_scorer_fair"] is None
