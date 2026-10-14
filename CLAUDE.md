# CLAUDE.md — match-predictor

Football W/D/L + scoreline predictor. Python pipeline → SQLite → JSON → static GitHub Pages site.
Pages: Result & Score, Corners, Cards, Shots, Players (+ Model accuracy). Value bets and odds polling
are *paused* (code kept: `betting.py`, `value.py`). No LLM calls in the daily pipeline.

**One file per market and per page** (keep it that way):
- models: `models/corners.py`, `models/cards.py`, `models/shots.py`, `models/players.py` on the shared
  count engine in `models/counts.py`; `match_markets.py` assembles them into each prediction.
- site: `assets/core.js` (shared helpers) + `assets/pages/<page>.js` (results, corners, cards, shots,
  players, accuracy). Each page registers `MP.pages.<id>`; `assets/shell.js` merges the pages listed
  in `<body data-pages="...">` into tabs (index.html) or runs a single page (corners.html etc.).
  A new page = a new `pages/<id>.js` + add the id to index.html's data-pages and a standalone html.

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
python -m matchpredictor detail --load --download --export   # Understat players/shots -> detail.db + archives
python -m matchpredictor warehouse           # rebuild data/warehouse.db (docs/DATA_MODEL.md)
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
As of 2026-10-13 api.clubelo.com also times out from GitHub Actions, and the API-Football account
reports "suspended"; both paths degrade gracefully (joint model / no absences).

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
    openfootball.py   Football.TXT parser (90-minute scores, CET kickoffs) used to build European seed CSVs
    api_football_history.py  bulk history for the team list (data/teams_<season>.json): ids, competitions +
                      coverage, all fixtures, match detail x20/request (team stats incl. xG, lineups, player
                      match stats, events), injuries, squads + season stats, transfers -> detail.db af_* tables
  models/
    dixon_coles.py    DC fit; Poisson targets may be a goals/xG blend, tau uses real scores
    elo.py            Elo→goals (per-competition HFA), geometric blend, DC→Elo map, rest feature, 1-D fits
    injuries.py       absence_effect(): npxG/xA/goal shares, first-choice GK, regular defenders
    markets.py        score matrix → 1X2, scorelines (top 3), totals, BTTS
  predict.py       Predictor.components() (raw sources) + combine() (blend + factors) → payload
  absences.py      injuries/suspensions/lineups → per-team effects (fresh-absence rule)
  detail.py        data/detail.db (gitignored, kept in the Actions cache): Understat player_matches + shots and
                   API-Football af_* tables; finished seasons archived as deterministic csv.gz in
                   data/history/understat/ and data/history/api_football/ (`detail --load` rebuilds in seconds)
  warehouse.py     data/warehouse.db: clean unified copy of everything (our names + match ids, one player id per
                   person, best source per column, cross-source checks) - see docs/DATA_MODEL.md
  backtest_markets.py  walk-forward accuracy of corners/cards/shots/SOT main lines vs a baseline
  match_markets.py corners/cards/shots/players blocks for a prediction payload
  backtest.py      collect → tune (2024-25) → ablation on 2025-26 → bookmaker benchmark → UCL section;
                   rolling_tune() daily re-optimisation, current_config() = settings + adopted tune
  pipeline.py      run(): history → fixtures → results → API-Football enrich → predict shown matches → site
  tracking.py      Brier, log loss, calibration, result / exact / top-3 hit rates on live predictions
  site.py          site/data/predictions.json (window) + accuracy.json (live tracking)
  schedule.py      stdlib-only cron gate (keep stdlib-only: runs before deps are installed)
  betting.py       PAUSED value-bet helpers (odds polling, de-vig evaluation, bet logging)
site/  index.html (all pages as tabs), results/corners/cards/shots/players.html (standalone), accuracy.html;
       assets/core.js + assets/pages/*.js + assets/shell.js render site/data/*.json
.github/workflows/daily.yml   hourly tick → gate → run → (main only) commit DB + JSON → deploy Pages
```

### How a prediction is made (`predict.combine`)
1. Same-league game (league match, or domestic cup between two clubs of one modelled league):
   Dixon-Coles on `xg_weight·xG + (1−xg_weight)·goals`, blended geometrically with the Elo model.
2. Cross-league (Champions League, cups vs. foreign/lower clubs): the **joint model** (`fit_joint`) —
   one DC fit over all leagues + European results (`data/history/ucl_results*.csv` seeds: 2022-25 from
   API-Football, 2025-26 from openfootball via `ingest/openfootball.py`; + live results). The most recent
   European season matters most: adding 2025-26 cut log loss on the first 18 2026-27 UCL games 1.035 → 0.922;
   European games link the leagues. Results-only extra leagues (`type: "extra"`: POR, NED, TUR, …) are stored
   but kept out of the fit (`model.joint_extra_leagues=false`: they worsened 2024-25 UCL log loss 0.915 → 0.924).
   `model.cross_league_shrink` then calibrates confidence by matchup category (top / mixed / outside). Unseen clubs get the mean rating of Europe-only clubs and are
   flagged `thin_data`. If ClubElo is available and `factors.elo` is on, Elo is blended in; without the
   joint model, ClubElo alone (DC strength mapped onto the Elo scale) is the fallback. No rating at all
   → no prediction (never a guess).
3. Factors (each only if `settings.factors[...]` is on): rest-day difference, absences (attack ×,
   opponent ×), referee penalty-rate shift. Confirmed lineups (~1 h before kickoff) replace injury lists.

### Key data rules
- League match id = `league:season:home:away` (pairing happens once per season → sources merge even
  if rescheduled). Cup/European ids include the date (two legs, league phase + knockouts).
- Team names are football-data names; map others in `team_aliases.json`. Unmatched names are skipped
  (never stored under a new name) and reported as a data note. A club plays one UEFA competition per
  season: a fuzzy match onto a club already in another one keeps the provider's name (`pipeline._europe_name`).
- Predictions are only made for future kickoffs; the stored payload is the last pre-kickoff one.
- Backtests must only use data before each match week (`fit_league(as_of=...)`, Elo params fitted on
  seasons before the evaluated one, absences use the previous season's player shares).
- ClubElo intervals: `From` = day after a match, so the interval containing a match date is the
  pre-match rating and interval starts give every club's match dates in all competitions (rest days).

### Free-tier realities
- API-Football: PRO plan since 2026-10-14 (7,500 req/day, all seasons/endpoints; `netcheck` prints plan and
  quota). The full history runs in Actions: `workflow_dispatch` mode `backfill` on main (commits archives),
  `afsample` to check parsing on ~10 requests. The sandbox cannot use the key (proxy drops the header).
  The client turns itself off for the run on the first plan/quota error.
- Odds API: fixtures via `/events` are free; `/scores` costs 2 credits per competition-day, only called
  when a shown match finished in the last 3 days. Odds polling is off (`odds_api.fetch_odds=false`).
- ClubElo is a free hobby service: history refreshed at most monthly, 0.3 s pause, disk-cached.

## Conventions
- Tunables live in `config/settings.json`; don't hard-code thresholds.
- Tune on the season *before* the test season; a factor stays on only if it improves tuning-season
  log loss by > 0.001 (`backtest.KEEP_THRESHOLD`). settings.json holds the reviewed defaults; the daily
  `rolling_tune` (last 365 days, walk-forward) may override them via `model_params.tuned` only when it
  beats the configuration in use by > KEEP_THRESHOLD — every attempt is logged in `tuning_log`.
- Seed history in `data/history/*.csv` is loaded on every refresh (source `seed`); add files there for
  competitions with no live historical source (UCL 2021-26, UEL/UECL 2021-25 from openfootball: adding the
  Europa/Conference League cut UCL log loss 0.907 -> 0.888 on 2024-25).
- Model math changes need tests (`tests/test_model_math.py`, `tests/test_phase_a.py`); pipeline/tracking
  changes → `tests/test_pipeline.py`.
- Site is plain HTML/CSS/JS (no build step, no external libraries); dark theme, mobile-first.

### Data quirks
- GitHub's hourly cron is unreliable (2026-10-14 fired only 3 times), so the gate's results window is 2-12 h.
- Understat multi-club player titles ("Club A,Club B") are alphabetical, not chronological, and were
  corrupted for Ligue 1 2026-27 (31 PSG/Rennes players merged): such players are skipped, and a team
  whose stored player minutes cover < 70% of its games (`absences.covered`) falls back to last season.
