# CLAUDE.md — match-predictor

Football prediction + value-bet tracker (EPL, La Liga). Python pipeline → SQLite → JSON → static site.
Phase 1 MVP: keep it simple; no LLM calls in the daily pipeline.

## How to run

```bash
pip install -r requirements.txt
python -m pytest -q                          # always run before committing (offline, ~2s)
python -m matchpredictor run --mode daily    # or prekickoff | auto (auto = ask the gate)
python -m matchpredictor history             # refresh football-data CSVs only
python -m matchpredictor backtest            # → site/data/backtest.json + markdown table
python -m matchpredictor site                # rebuild site JSON from DB
python -m matchpredictor gate                # prints daily | prekickoff | skip
python -m http.server -d site 8000           # preview
```

Env: `ODDS_API_KEY`, `API_FOOTBALL_KEY`, optional `MATCH_DB` (defaults to `data/match.db`;
point it at a scratch file when experimenting so you don't touch the committed DB).

## Architecture

```
config/            leagues.json (add leagues here), settings.json (all knobs), team_aliases.json
matchpredictor/
  config.py        settings loading, canonical_team() name mapping, season codes ('2627')
  db.py            SQLite schema + upsert_match (COALESCE merge; FT never regresses) + save_prediction
  ingest/
    football_data.py  historical CSVs (primary) → GitHub mirror fallback (no odds); fixtures.csv referees
    odds_api.py       /events (free), /odds (FanDuel), /scores; credit reserve guard
    api_football.py   fixtures/injuries/lineups/topscorers; self-disables on plan/quota errors
    http_cache.py     api_cache + api_usage tables
  models/
    dixon_coles.py    DC fit (analytic gradient, time decay, ridge, priors), score_matrix
    markets.py        score matrix → 1X2, DC, totals, BTTS, team totals, correct score; fair odds
    counts.py         corners/shots/SOT/cards rating models, NB dispersion, referee factor
    injuries.py       absence multiplier from attacking share
    props.py          anytime scorer / 1+ SOT
  predict.py       fit_league() (as-of date, no leakage) + predict_match() → payload dict
  value.py         remove_vig, edge, EV, Kelly, assess() (min/max edge, odds range)
  pipeline.py      run(): history → fixtures → results → odds → enrich → predict → bets → settle → site
  tracking.py      scoring rules, calibration, bet settlement, closing odds, performance()
  backtest.py      walk-forward weekly refits, xi tuning, baseline + market comparison, bet sim
  schedule.py      stdlib-only cron gate (runs before deps are installed — keep it stdlib-only)
  site.py          writes site/data/predictions.json + performance.json
site/              static HTML + assets/app.js renders everything client-side from site/data/*.json
.github/workflows/daily.yml   hourly tick → gate → run → commit DB + JSON → deploy Pages
```

### Key data rules
- Match id = `league:season:home:away` (a pairing happens once per season, so results/fixtures from
  different sources merge regardless of kickoff date changes). Team names are football-data names;
  map provider names via `config/team_aliases.json` (pipeline adds a data note for unmatched names).
- Predictions are only (re)made for future kickoffs, so the stored payload is the last pre-kickoff
  prediction. `prediction_log` keeps every run.
- Bets: first flagged price is locked (`UNIQUE` index uses `COALESCE(line,-1)` because SQLite NULLs
  are distinct). Closing = last FanDuel snapshot ≤ kickoff; settled from FT scores.
- Odds snapshots are stored only when a price changes.
- Backtests must never use data on/after the predicted match date (`fit_league(as_of=...)`).

### Free-tier realities
- API-Football free plan rejects the current season → lineups/injuries/props are empty; the code
  path exists and is exercised by tests with fixtures.
- Odds API: 500 credits/month. Daily ≈ 4–8 credits; each pre-kickoff run ≈ 2 per league kicking off
  within 3h (~300–350/month total).
  `reserve_credits` stops spending near the limit.
- football-data.co.uk may be blocked in sandboxes; the mirror fallback has no odds, so ROI/CLV and
  market comparisons need the primary source (available in GitHub Actions).

## Conventions
- Tunables live in `config/settings.json`; don't hard-code thresholds.
- Tune hyperparameters on the season *before* the backtest season, never on the test season.
- Model math changes need a test in `tests/test_model_math.py` (probabilities sum to 1, fair-odds
  inversion, gradient check, etc.). Pipeline/tracking changes → `tests/test_pipeline.py`.
- Site is plain HTML/CSS/JS (no build step, no external libraries); dark theme, mobile-first.
