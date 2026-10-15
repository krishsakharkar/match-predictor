# Data model

Three SQLite files, each with one job:

| File | In git? | What it is |
|---|---|---|
| `data/match.db` | yes | The live pipeline's database: results, fixtures, predictions, tracking. Small. |
| `data/detail.db` | no (Actions cache) | Raw per-match detail exactly as the sources give it: Understat players/shots, API-Football `af_*` tables. Rebuilt from `data/history/{understat,api_football}/*.csv.gz` with `python -m matchpredictor detail --load`. Every season is archived, the current one included (re-written on each backfill run). |
| `data/warehouse.db` | no | **The clean copy to analyse and model from - and the only data the model reads** (`matchpredictor/model_data.py`; every predicting run and backtest rebuilds it first, ~2 min). Rebuilt from the two above with `python -m matchpredictor warehouse`. Never edit it by hand: fix the source or the build. |

## Conventions (warehouse)

- **Club names** are football-data.co.uk spellings (`Man City`, `Ath Madrid`, `Nott'm Forest`). An API-Football
  club takes one of our names by evidence first: its matches in our competitions line up with ours on date (±1
  day) and score at least 3 times (≥ 70% of its aligned matches); on a busy matchday with several games of that score
  the opponent's already-known name decides. Otherwise a name match counts only when the
  countries agree, and never for youth / reserve / women's sides (*Manchester United U21* is not *Man United*).
  Each of our names belongs to one API club; any other club keeps its own name (made unique with its country).
- **Only played (or scheduled) fixtures** are merged: cancelled, postponed, abandoned and awarded API fixtures are
  skipped. A league game matched once cannot be claimed again (a play-off between the same clubs is its own match).
- **Match ids** follow `db.match_id`: top-level leagues `COMP:season:home:away`, everything else (cups,
  Europe, extra leagues) `COMP:date:home:away`. Competitions we don't configure are `AF<league id>`.
- **Seasons** are July-June codes: `2526` = 2025-26. API-Football's season year 2025 is `2526`.
- **Scores** are 90-minute scores. Extra time and penalties are separate columns.
- **xG** is the sum of Understat's shot xG wherever we have the shots (one definition for every season: Understat
  re-rated old shots but not its old league-page totals, which differ by > 0.1 in ~10% of 2021-23 matches; the page
  value is kept in `home_xg_page` / `away_xg_page`), else Understat's page value, else API-Football's xG.
- **Relegation / promotion play-offs** that API-Football files under a top league get their own competition
  (`BUNDESLIGA_PO`, `LIGUE1_PO`, ...): they are not league games and must not mix 2nd-division clubs into a league.
- Impossible provider values (e.g. a player with -86 minutes) are blanked, and the audit says how many.
- **Player ids**: `af:<API-Football id>` (one per real person). Understat players are linked to it by name agreement
  in the same match and team (majority vote over all shared matches); Understat-only players are `us:<id>`.
- **Times** are UTC in the data; the site shows US Eastern.

## Tables

| Table | Key | Contents |
|---|---|---|
| `competitions` | comp | code, name, type (league / extra / europe / cup / ...), country, API league id |
| `teams` | team | country, API team id, `tracked` (team list or settings.tracked_teams), main domestic league |
| `matches` | match_id | date, kickoff, round, teams, status, 90-min score, HT / ET / penalties, xG (+ source), referee, venue, closing odds (football-data), source ids |
| `team_match_stats` | match_id, team | goals, conceded, xG for/against, shots, on target, inside box, corners, fouls, offsides, cards, possession, passes, pass %, saves |
| `players` | player_id | name (full name preferred), birth date, nationality, position, API + Understat ids |
| `player_match_stats` | match_id, team, player_id | started, minutes, position, rating, goals, assists, shots, on target, key passes, xG, xA, xGChain, xGBuildup, passes, tackles, interceptions, duels, dribbles, fouls, cards, penalties, saves |
| `lineups` | match_id, team, player_id | starting XI + bench, shirt number, position, grid, formation, coach |
| `shots` | shot_id | minute, player, result, x/y location, xG, situation, shot type, assist, last action (Understat) |
| `events` | match_id, seq | goals, cards, substitutions, VAR, with minute, player and assist |
| `injuries` | match_id, player_id | players missing or doubtful for a match, with reason |
| `squads` | season, team, comp, player_id | season totals per competition: appearances, starts, minutes, rating, goals, assists, shots, key passes, cards |
| `transfers` | player_id, date, teams | transfer history of everyone who played for a tracked club |
| `build_log` | step | row counts per table and the cross-source checks of the last build |

## Source priority (first available wins)

| Field | 1st | 2nd | 3rd |
|---|---|---|---|
| Result, date | football-data / Understat / seeds (match.db) | API-Football | |
| Match xG | Understat | API-Football `expected_goals` | |
| Shots, on target, corners, fouls, cards | football-data | API-Football | |
| Possession, passes, offsides, saves, shots inside box | API-Football | | |
| Closing odds | football-data | | |
| Player xG, xA, xGChain, xGBuildup | Understat | | |
| Player rating, passes, tackles, duels, dribbles | API-Football | | |
| Player goals, minutes, cards | API-Football | Understat | |
| Lineups, events, injuries, squads, transfers | API-Football | | |

Football-data stays first for match stats because the corners / cards / shots models were tuned on it; Understat
stays first for xG because the result model was tuned on it (`model.xg_weight`).

## Coverage (what exists where)

- **Big five leagues** (EPL, La Liga, Bundesliga, Serie A, Ligue 1): everything above, 2021-22 onwards, including
  Understat player xG and every shot.
- **Other domestic leagues of tracked clubs** (Portugal, Netherlands, Turkey, Belgium, Greece, Norway, Austria,
  Azerbaijan, Ukraine, Czechia, Slovakia ...): results + match stats + lineups + player match stats + events +
  injuries where API-Football's coverage flags say so (see `d.af_competitions`); no Understat xG.
- **Cups and Europe**: API-Football detail for every European match and every cup match of a tracked club;
  results seeds back to 2021-22 (`data/history/*_results*.csv`).

## Audit

`python -m matchpredictor audit` (also in every `backfill` run's job summary) reports completeness - every finished
API fixture of a tracked club present, detail where covered, big-five xG and Understat rows - and accuracy: score =
goal events = players' goals, 11 starters, sane minutes, cross-source agreement, player-identity links.

## Checks run on every build (`build_log`)

- scores corrected to 90 minutes (old seeds that included extra time, fixed from API-Football's 90-minute score);
- score disagreements between our results and API-Football's 90-minute score (expected: a handful of awarded
  games, where football-data has the official awarded score and API-Football the score on the pitch);
- possible duplicate matches (same competition, date and home team);
- one API club matching two of our names (a spelling split in a source: add an alias);
- tracked clubs without an API-Football id.

Last full build (2026-10-15, from the archives alone): 61.7k matches, 993k player-match rows, 234k shots, 1.65M
lineup rows, 645k events, 131k injury records; Understat detail for every big-five match 2021-22 onwards except two
gaps at the source (Holstein Kiel v Bochum 2025-02-09: listed without data, its page 'xG' a 2-2 placeholder; Rennes v
Paris SG 2026-08-23: not listed) - both use API-Football xG; 5 disagreements (all awarded games), 2 possible duplicates
(inside API-Football's own amateur-cup data), 0 spelling splits.

A `backfill` runs every Monday 06:00-08:59 ET on its own (the hourly gate; if GitHub skips those ticks it catches up
once the last one is 8 days old): API-Football's current season (~320 requests of the 7,500/day plan, ~10 min) and
new Understat matches (`detail --download`), then the warehouse rebuild and the audit in the job summary.

A non-zero count is a data problem to look at, not something to ignore.
