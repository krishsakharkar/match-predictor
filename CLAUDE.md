# CLAUDE.md — match-predictor

Football W/D/L + scoreline predictor. Python pipeline → SQLite → JSON → static GitHub Pages site.
**Phase A** = result & scoreline accuracy only. Value bets, odds polling and corners/cards/shots/
player props are *paused* (code kept: `betting.py`, `models/counts.py`, `models/props.py`, `value.py`).
No LLM calls in the daily pipeline.

## Scope (config, not code)
- Every match of the competitions with `"coverage": "full"` in `config/competitions.json`
  (Premier League, Champions League).
- Every match, any competition, of `settings.tracked_teams` (football-data spellings):
  Barcelona, Real Madrid, Ath Madrid, Bayern Munich, Paris SG, Dortmund, Inter, Man United,
  Man City, Liverpool, Arsenal, Chelsea, Tottenham. `config.is_displayed()` is the single rule.
- Site window: last 7 days (with results) + next 7 days (`settings.display`).

## How to run

```bash
pip install -r requirements.txt
python -m pytest -q                          # always before committing (offline, ~2s)
python -m matchpredictor netcheck            # which data hosts are reachable from here
python -m matchpredictor run --mode daily    # or prekickoff | auto (auto = ask the gate)
python -m matchpredictor history [--backtest-data]   # football-data, Understat, ClubElo (+API-Football history)
python -m matchpredictor backtest --refresh  # walk-forward ablation → site/data/backtest.json (+ markdown)
python -m matchpredictor site                # rebuild site JSON from DB
python -m http.server -d site 8000           # preview
```

Env: `ODDS_API_KEY`, `API_FOOTBALL_KEY`, optional `MATCH_DB` (default `data/match.db`; point it at a
scratch copy when experimenting). Raw source caches live in `data/cache/` (gitignored, persisted in
Actions with `actions/cache`).

**Sandbox note:** this Claude environment blocks api.clubelo.com, football-data.co.uk and github.io
(Understat works from Python `requests` even though `curl` is refused). Anything needing them runs in
GitHub Actions: `workflow_dispatch` the `predict` workflow on your branch with `mode: backtest` or
`mode: netcheck` — branch runs never commit or deploy; read the job summary / artifact.

## Architecture

```
config/
  competitions.json  every competition: type league|europe|cup, coverage full|teams, source ids
  settings.json      all knobs: factors on/off, xg_weight, elo blend, rest coef, injuries, display
  team_aliases.json  provider name -> football-data name;  elo_aliases.json  our name -> ClubElo name
matchpredictor/
  config.py        settings, competitions(), leagues() (DC leagues), is_displayed(), name matching
  db.py            schema + migrations (ALTER TABLE for new columns), upsert_match, match_id rules
  history.py       assembles data: football-data, Understat, ClubElo snapshots/backfill, API-Football history,
                   fit_elo_params()
  ingest/
    football_data.py  results/stats/closing odds (5 leagues) → GitHub mirror fallback (no odds)
    understat.py      match xG + player npxG/xA (JSON or HTML page format), disk-cached
    clubelo.py        ratings snapshots + club history, EloBook (point-in-time rating, last match date)
    api_football.py   fixtures/referees, injuries, lineups, penalties (events), season history; disk cache
                      for historical calls, 6.5 s pacing + one retry (free plan: 10/min, 100/day)
    odds_api.py       /events (free fixtures for every competition), /scores (results)
  models/
    dixon_coles.py    DC fit; Poisson targets may be a goals/xG blend, tau uses real scores
    elo.py            Elo→goals (per-competition HFA), geometric blend, DC→Elo map, rest feature, 1-D fits
    injuries.py       absence_effect(): npxG/xA/goal shares, first-choice GK, regular defenders
    markets.py        score matrix → 1X2, scorelines (top 3), totals, BTTS
  predict.py       Predictor.components() (raw sources) + combine() (blend + factors) → payload
  absences.py      injuries/suspensions/lineups → per-team effects (fresh-absence rule)
  backtest.py      collect → tune (2024-25) → ablation on 2025-26 → bookmaker benchmark → UCL section
  pipeline.py      run(): history → fixtures → results → API-Football enrich → predict shown matches → site
  tracking.py      Brier, log loss, calibration, result / exact / top-3 hit rates on live predictions
  site.py          site/data/predictions.json (window) + accuracy.json (live tracking)
  schedule.py      stdlib-only cron gate (keep stdlib-only: runs before deps are installed)
  betting.py       PAUSED value-bet helpers (odds polling, de-vig evaluation, bet logging)
site/  index.html (matches by date) + accuracy.html (backtest + live tracking), assets/app.js renders JSON
.github/workflows/daily.yml   hourly tick → gate → run → (main only) commit DB + JSON → deploy Pages
```

### How a prediction is made (`predict.combine`)
1. Same-league game (league match, or domestic cup between two clubs of one modelled league):
   Dixon-Coles on `xg_weight·xG + (1−xg_weight)·goals`, blended geometrically with the Elo model.
2. Cross-league (Champions League, cups vs. foreign/lower clubs): each team's DC strength is mapped onto
   the Elo scale (per-league least squares), blended with its ClubElo rating, then Elo→goals with that
   competition's home advantage. No rating at all → no prediction (never a guess).
3. Factors (each only if `settings.factors[...]` is on): rest-day difference, absences (attack ×,
   opponent ×), referee penalty-rate shift. Confirmed lineups (~1 h before kickoff) replace injury lists.

### Key data rules
- League match id = `league:season:home:away` (pairing happens once per season → sources merge even
  if rescheduled). Cup/European ids include the date (two legs, league phase + knockouts).
- Team names are football-data names; map others in `team_aliases.json`. Unmatched names are skipped
  (never stored under a new name) and reported as a data note.
- Predictions are only made for future kickoffs; the stored payload is the last pre-kickoff one.
- Backtests must only use data before each match week (`fit_league(as_of=...)`, Elo params fitted on
  seasons before the evaluated one, absences use the previous season's player shares).
- ClubElo intervals: `From` = day after a match, so the interval containing a match date is the
  pre-match rating and interval starts give every club's match dates in all competitions (rest days).

### Free-tier realities
- API-Football free plan: seasons 2022-2024 only, 100 req/day, 10 req/min. Current-season injuries,
  lineups, referees and penalties need Pro; the client turns itself off on the first plan error.
- Odds API: fixtures via `/events` are free; `/scores` costs 2 credits per competition-day, only called
  when a shown match finished in the last 3 days. Odds polling is off (`odds_api.fetch_odds=false`).
- ClubElo is a free hobby service: history refreshed at most monthly, 0.3 s pause, disk-cached.

## Conventions
- Tunables live in `config/settings.json`; don't hard-code thresholds.
- Tune on the season *before* the test season; a factor stays on only if it improves tuning-season
  log loss by > 0.001 (`backtest.KEEP_THRESHOLD`). Copy `recommended_settings` from the backtest into
  settings.json by hand (reviewable diff), never automatically.
- Model math changes need tests (`tests/test_model_math.py`, `tests/test_phase_a.py`); pipeline/tracking
  changes → `tests/test_pipeline.py`.
- Site is plain HTML/CSS/JS (no build step, no external libraries); dark theme, mobile-first.
