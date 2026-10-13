# match-predictor

Football match predictions and value-bet tracking for the **Premier League** and **La Liga**
(other leagues: add an entry to `config/leagues.json`). It runs as a static GitHub Pages site and
updates itself through one GitHub Actions workflow. The daily pipeline makes no LLM calls.

- `index.html`: upcoming and recent matches, each with predicted score, win/draw/loss %, and the result or "TBD". Tap a card for xG, corners, SOT, cards, lineups, props, and value vs FanDuel.
- `value.html`: value bets for today and the coming days, sorted by edge.
- `performance.html`: Brier score, log loss, calibration, ROI, CLV, biggest misses, and the backtest.
- `data/predictions.json`: machine-readable output for other tools.

> Phase 1 MVP. The probabilities are model estimates. Read the backtest before trusting any edge.

## Quick start

```bash
pip install -r requirements.txt
export ODDS_API_KEY=...        # the-odds-api.com (free: 500 credits/month)
export API_FOOTBALL_KEY=...    # api-football.com (free: 100 req/day)
python -m pytest -q                         # model math + pipeline tests (offline)
python -m matchpredictor run --mode daily   # fetch → predict → value → track → site/data/*.json
python -m matchpredictor backtest           # walk-forward backtest → site/data/backtest.json
python -m http.server -d site 8000          # view the site at http://localhost:8000
```

### Deploying (GitHub)

1. Add the repo secrets `ODDS_API_KEY` and `API_FOOTBALL_KEY`.
2. Settings → Pages → Source: **GitHub Actions**.
3. Merge to the default branch. Scheduled workflows only run there. Then trigger **predict** once
   from the Actions tab (`mode: daily`) to build the database and site.

The workflow ticks every hour at :07. A stdlib-only gate (`python -m matchpredictor gate`) picks
the mode for each tick:

| Mode | When | API cost |
|---|---|---|
| `daily` | 09:xx US Eastern | history CSVs, fixtures (free), FanDuel odds for the next 7 days (2 credits/league), scores if needed (2 credits/league) |
| `prekickoff` | a kickoff is 45–105 min away | FanDuel odds for leagues kicking off in the next 3 h (2 credits/league), lineups |
| `skip` | otherwise | none (exits before installing dependencies) |

Estimated usage is about 300–350 Odds API credits a month (budget: 500). After each run the workflow commits `data/match.db` and `site/data/` and deploys Pages. On Mondays
it also re-runs the backtest.

## Data sources and free-tier limits

| Source | Used for | Notes |
|---|---|---|
| football-data.co.uk | 5 seasons + current: results, shots, SOT, corners, cards, fouls, referee (England only), closing odds | If unreachable, falls back to a GitHub mirror of the same CSVs, which has **no odds**. Mirror seasons are re-fetched until the primary source succeeds. |
| The Odds API | fixtures (`/events`, free), FanDuel `h2h` + `totals` (region `us`), scores | Responses are cached in SQLite. Calls stop when remaining credits drop under `reserve_credits` (40). In Oct 2026 FanDuel listed only `h2h` for these leagues. Totals are picked up automatically once listed. |
| API-Football | fixture IDs, referees, injuries, lineups, player stats (top scorers) | **The free plan only allows seasons 2022–2024**, so for the current season it returns a plan error. The client detects this on the first call, turns itself off for the run, and the site shows "lineups / props unavailable". A paid plan enables injuries, lineups and player props with no code changes. |

## Model

**Goals: Dixon-Coles.** `λ_home = exp(c + home_adv + attack_home + defence_away)`,
`μ_away = exp(c + attack_away + defence_home)`, with the Dixon-Coles low-score correction `ρ`.
Matches are weighted by `exp(−ξ·days_ago)`, with ξ tuned per league (EPL 0.005 ≈ 140-day half-life,
La Liga 0.0019 ≈ 1 year). A ridge penalty (3.0) shrinks ratings toward 0, and newly promoted teams
are shrunk toward a weaker prior (attack −0.25, defence +0.20). The fit uses L-BFGS-B with an
analytic gradient, which the tests check against finite differences.

**Markets from the score matrix (0–10 goals):** 1X2, double chance, O/U 0.5–4.5, BTTS, team totals
0.5/1.5/2.5, and the top 5 correct scores. Fair odds = 1/p for every market.

**Absences:** each missing player's attacking share is ½·(goal share) + ½·(shot share). Shots stand
in for xG, which football-data doesn't publish. Expected goals are scaled by
`max(0.75, 1 − Σshare × (1 − 0.5))`, where 0.5 is how much of the output a replacement recovers.

**Corners, shots, SOT, cards:** log-linear rating models per stat, with separate home and away
means and opponent adjustment (`for`/`against` ratings), the same time decay, and a negative
binomial whose dispersion comes from the method of moments. Cards are cards = yellow + red, and
get a shrunk referee multiplier when the referee is known.

**Player props:** `λ = team expected goals (or SOT) × player per-90 share × expected minutes/90`;
anytime scorer = `1 − e^(−λ_goals)`, 1+ SOT = `1 − e^(−λ_sot)`.

**Value:** FanDuel prices are de-vigged proportionally (2-way and 3-way markets).
`edge = p_model / p_novig − 1`. A selection is flagged as **value** when edge ≥ 5%, EV at the real
price > 0, and odds are between 1.3 and 10. Stake = ¼ Kelly, capped at 2% of bankroll. Edges above
35% (`max_edge`) are marked **suspect** and not staked, because an edge that large is far more often
model error than a mispriced line. Set `max_edge` to `null` to turn this off. With one bookmaker this
is value betting, not arbitrage.

**Tracking:** every prediction run is logged (`prediction_log`), and the last pre-kickoff
prediction is frozen per match (`predictions`). The first price at which a bet is flagged is locked
as *odds taken*. *Closing odds* = the last FanDuel snapshot before kickoff (fallback: football-data
closing odds). Bets settle from results.

## Backtest: season 2025-26

Walk-forward: the model is refit every 7 days using only matches played before that week (5 prior
seasons plus the season so far). Time decay was tuned on 2024-25, and the ridge settings were chosen
on 2024-25 as well, never on the test season. Baseline = home/draw/away (and over/BTTS) frequencies
from earlier seasons.

| League | n | Brier 1X2 (model / baseline) | Log loss 1X2 (model / baseline) | Favourite hit rate | Brier O2.5 (model / baseline) | Brier BTTS (model / baseline) |
|---|---|---|---|---|---|---|
| Premier League | 380 | **0.620** / 0.656 | **1.031** / 1.085 | 46.8% / 42.6% | 0.256 / 0.248 | 0.247 / 0.246 |
| La Liga | 380 | **0.579** / 0.632 | **0.976** / 1.050 | 52.1% / 49.0% | 0.247 / 0.251 | 0.250 / 0.249 |

Count models (match totals; "constant" = always predicting that season's actual over rate, a
benchmark that peeks at the answer):

| League | Stat | MAE | Mean pred / actual | Brier over line (model / constant) |
|---|---|---|---|---|
| EPL | corners (9.5) | 2.65 | 9.96 / 10.00 | 0.251 / 0.246 |
| EPL | cards (3.5) | 1.62 | 3.88 / 3.85 | 0.245 / 0.246 |
| EPL | SOT (8.5) | 2.31 | 8.57 / 8.37 | 0.252 / 0.249 |
| La Liga | corners (9.5) | 2.69 | 9.55 / 9.67 | 0.246 / 0.250 |
| La Liga | cards (3.5) | 1.97 | 4.86 / 4.68 | 0.229 / 0.230 |
| La Liga | SOT (8.5) | 2.32 | 8.43 / 8.67 | 0.244 / 0.250 |

What the backtest says:

- **1X2 has real signal:** a clear gain over the baseline in both leagues, and calibration is good
  between 10% and 60%. Above 60% the model is slightly overconfident (bucket 0.6–0.7: predicted
  63%, observed 57%).
- **Totals and BTTS have about zero skill.** Model xG totals barely correlated with actual EPL
  totals in 2025-26 (r ≈ 0.05). Treat O/U and BTTS edges with suspicion.
- **Count models get the averages right** but barely beat a constant on over/under lines. La Liga
  corners and SOT do slightly better. EPL corners and SOT don't.
- **Biggest misses** are favourites losing at home: Real Madrid 0-1 Getafe (8.6%), Arsenal 1-2
  Bournemouth (12.9%). See `performance.html`.
- **ROI and CLV are not in this table.** The local backtest ran on the mirror CSVs, which have no
  odds, because football-data.co.uk was blocked from the build sandbox. The workflow's weekly
  backtest runs against football-data with closing odds and adds model-vs-market Brier and a
  simulated value-bet ROI at closing prices to `site/data/backtest.json` and the performance page.
  **Until that shows a non-negative ROI, treat flagged value bets as paper trades.**

Reproduce: `python -m matchpredictor history && python -m matchpredictor backtest`.

## Limitations / Phase 2

- Single bookmaker (FanDuel, US region), so no line shopping or arbitrage.
- No xG data: shots are the proxy. Player props and injuries need a paid API-Football plan.
- The SQLite DB is committed on every run (~1 MB, so the repo grows over time). Moving it to an
  orphan data branch is the easy fix if that becomes a problem.
- Phase 2: a news/rumour LLM layer, more leagues, BTTS/alt totals from per-event odds endpoints.
