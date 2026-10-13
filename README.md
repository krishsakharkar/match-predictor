# match-predictor

Win/draw/loss probabilities and scoreline predictions for:

- every **Premier League** match,
- every **Champions League** match,
- every match, in any competition, of **Barcelona, Real Madrid, Atlético Madrid, Bayern, PSG,
  Dortmund, Inter, Man United, Man City, Liverpool, Arsenal, Chelsea and Tottenham**.

Live site: **https://krishsakharkar.github.io/match-predictor/**. It's a static GitHub Pages site that
updates itself through one GitHub Actions workflow, with no LLM calls in the daily pipeline. Value
bets are paused.

`index.html` merges every page as a tab. Each page also stands alone, and each has its own code file.

| Page | File | Shows |
|---|---|---|
| Result & Score | `results.html`, `assets/pages/results.js` | W/D/L %; most likely score, top 3, and the 6 most likely; **Bankers** (favourite ≥ 70%); absences; referee; result with hit/miss marks |
| Corners | `corners.html`, `assets/pages/corners.js` | expected corners per team; over/under lines for the total and each team |
| Cards | `cards.html`, `assets/pages/cards.js` | expected cards (referee-adjusted); over/under lines |
| Shots | `shots.html`, `assets/pages/shots.js` | expected shots and shots on target per team; over/under lines |
| Players | `players.html`, `assets/pages/players.js` | per team: anytime goal, assist, 1+ shot on target, 2+ shots |
| Model accuracy | `accuracy.html`, `assets/pages/accuracy.js` | backtests, factor table, bankers, markets, calibration, match-by-match tables |

`data/predictions.json` is machine-readable output for other tools.

Each market also has its own model file: `matchpredictor/models/corners.py`, `cards.py`, `shots.py`
and `players.py`.

**Markets backtest (2025-26, refitted every 14 days):**

| Market | Main line | More likely side won | Brier, model vs baseline |
|---|---|---|---|
| Cards | 4.5 | 62% | 0.230 vs 0.238 |
| Shots | 24.5 | 58% | 0.240 vs 0.250 |
| Corners | 9.5 | 57% | 0.247 vs 0.250 |
| Shots on target | 8.5 | 55% | 0.247 vs 0.250 |

All four beat the baseline (last season's over rate), modestly. Player markets aren't backtested,
because that needs per-match player data.

**Bankers (2025-26):**

| Favourite at least | Share of matches | Correct |
|---|---|---|
| 60% | 19% | 74% |
| 70% | 6% | 80% |
| 80% | 1% | 85% |

The actual score fell inside the 6 most likely scorelines in 57% of matches.

## Backtest results (2025-26)

Method: walk-forward over 1,752 matches in the five big leagues. Before every match week the model
is refitted on earlier matches only. Settings were tuned on 2024-25 and never on the test season.
The benchmark is closing bookmaker odds with the margin removed, used as an accuracy yardstick only.

**In plain English**

- The model's favourite won **52.7%** of matches. The bookmakers' closing favourite won **53.4%**.
- The single most likely scoreline was exactly right **12.6%** of the time. The real score was in
  the model's top 3 in **33.4%** of matches.
- On probability quality (log loss **0.993** vs **0.978**, lower is better), it is a little worse
  than closing prices. Those are the toughest public benchmark: they already include team news and
  heavy betting volume.
- **xG helps** and is switched on. Ratings are fitted on 75% Understat xG and 25% actual goals.
- **Rest days and injuries did not help**, so they are switched off. The ClubElo blend could not be
  tested because ClubElo was unreachable.

| Variant | Result hit | Exact score | Top-3 score | Brier | Log loss | Δ log loss |
|---|---|---|---|---|---|---|
| Base: Dixon-Coles on goals | 52.1% | 12.8% | 32.0% | 0.595 | 0.997 | – |
| **+ xG (Understat), the model in use** | **52.7%** | 12.6% | **33.4%** | **0.592** | **0.993** | **−0.004** |
| + Rest days | 52.1% | 13.0% | 32.1% | 0.595 | 0.998 | +0.001 |
| Closing bookmaker odds | 53.4% | – | – | 0.582 | 0.978 | |

By league. These figures are from the CI run's selected model (xG + rest days); the model in use drops rest days, which moves totals by under 0.001. The bookmaker figure is closing-odds log loss:

| League | Result hit | Exact | Top-3 | Log loss | Bookmaker |
|---|---|---|---|---|---|
| Premier League | 50.3% | 10.8% | 31.8% | 1.027 | 1.014 |
| La Liga | 52.1% | 18.7% | 39.5% | 0.977 | 0.965 |
| Bundesliga | 54.6% | 10.5% | 27.1% | 0.972 | 0.950 |
| Serie A | 52.4% | 10.8% | 36.3% | 1.001 | 0.978 |
| Ligue 1 | 54.3% | 10.8% | 30.4% | 0.988 | 0.977 |

**Champions League 2024-25** (189 matches, joint model, refitted weekly on earlier results): the
favourite won **61%**, the exact score was right **5%** of the time, the real score was in the top 3
**21%** of the time, and log loss was 0.915.

**Injuries** were tested on 2024-25, the latest season API-Football's free plan covers. There were
1,599 matches with injury lists. Only "fresh" absences counted, with the cutoff chosen on the first
half of the season. Over the second half, log loss got 0.002 *worse*, so injuries stay off.
Long-term absentees are already reflected in a team's recent results.

## How a prediction is made

1. **League ratings.** Dixon-Coles, fitted on `0.75·xG + 0.25·goals` with time decay and a ridge
   penalty, plus a weaker starting point for promoted teams. The low-score correction uses real
   scorelines.
2. **Cross-competition strength.** One joint Dixon-Coles fit over all five leagues plus Champions
   League results (seeded for 2022-25 in `data/history/ucl_results.csv`). European games put clubs
   from different leagues on one scale. Clubs with no history get the average rating of clubs that
   only appear in Europe, and the card says "thin data". When api.clubelo.com is reachable, ClubElo
   ratings can be blended in. That path is built and tested, but off until a backtest shows it helps.
3. **Context factors.** Each is switchable and gets its own row in the backtest's factor table:
   - injuries, suspensions and confirmed lineups (players' npxG/xA/goal shares, with the keeper and
     regular defenders weighted separately);
   - rest days;
   - home advantage per competition;
   - the referee's penalty rate.

   Referees are stored for every match. The penalty adjustment switches on once there is enough
   referee data.
4. **Daily re-optimisation.** Ratings are refitted from scratch on every run, so last night's
   results are always included. The daily run also re-tunes the xG weight, the Elo blend, the rest
   coefficient and the injury rule on a rolling 12-month walk-forward window. New settings are
   adopted only if they beat the current ones by a clear margin, and every attempt is logged in
   `model_params.tuning_log`.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q
python -m matchpredictor netcheck             # which data hosts are reachable
python -m matchpredictor run --mode daily     # fetch → predict → site/data/*.json
python -m matchpredictor backtest --refresh   # walk-forward backtest → site/data/backtest.json
python -m http.server -d site 8000
```

Secrets: `ODDS_API_KEY` and `API_FOOTBALL_KEY`. Settings → Pages → Source: **GitHub Actions**. The
workflow ticks hourly. It runs in full at 09:xx ET, re-predicts about an hour before kickoffs, and
otherwise exits immediately. Dispatch it on any branch with `mode: backtest` or `mode: netcheck` to
test without committing.

## Data sources

| Source | Used for | Status |
|---|---|---|
| football-data.co.uk | 5 leagues × 6 seasons: results, stats, closing odds, EPL referees | reachable from Actions |
| Understat | match xG, player npxG/xA | reachable from Actions |
| The Odds API | fixtures for every competition (free), results | reachable; odds polling paused |
| API-Football | injuries, lineups, referees, penalties, Champions League history | **account suspended**, see below |
| ClubElo | cross-league ratings, rest days | **unreachable from GitHub Actions** (timeout) |

- **API-Football** returned "Your account is suspended" during the CI backtest. The free plan's
  10-requests-a-minute limit was probably hit during setup; the client now paces itself at 6.5 s
  per request and stops on the first suspension or plan error. Check
  https://dashboard.api-football.com. Current-season injuries, lineups and referees also need the
  Pro plan.
- **ClubElo** may be down or blocking GitHub's servers. The pipeline falls back to the joint model.

## Limitations

- Exact scorelines are hard: the most likely score is usually only 8–12% likely. Use the W/D/L %
  and the top 3 rather than a single score.
- Clubs outside the five leagues are rated only from their European games, which is thin data.
- The SQLite database is committed on every run (about 1–2 MB), so the repo grows over time.
