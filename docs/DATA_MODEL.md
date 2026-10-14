# Data model

Three SQLite files, each with one job:

| File | In git? | What it is |
|---|---|---|
| `data/match.db` | yes | The live pipeline's database: results, fixtures, predictions, tracking. Small. |
| `data/detail.db` | no (Actions cache) | Raw per-match detail exactly as the sources give it: Understat players/shots, API-Football `af_*` tables. Rebuilt from `data/history/{understat,api_football}/*.csv.gz` with `python -m matchpredictor detail --load`. |
| `data/warehouse.db` | no | **The clean copy to analyse and model from.** Rebuilt from the two above with `python -m matchpredictor warehouse` (seconds to minutes). Never edit it by hand: fix the source or the build. |

## Conventions (warehouse)

- **Club names** are football-data.co.uk spellings (`Man City`, `Ath Madrid`, `Nott'm Forest`). An API-Football
  club takes one of our names by evidence first: its matches in our competitions line up with ours on date (±1
  day) and score at least 3 times (≥ 70% of its aligned matches). Otherwise a name match counts only when the
  countries agree, and never for youth / reserve / women's sides (*Manchester United U21* is not *Man United*).
  Each of our names belongs to one API club; any other club keeps its own name (made unique with its country).
- **Only played (or scheduled) fixtures** are merged: cancelled, postponed, abandoned and awarded API fixtures are
  skipped. A league game matched once cannot be claimed again (a play-off between the same clubs is its own match).
- **Match ids** follow `db.match_id`: top-level leagues `COMP:season:home:away`, everything else (cups,
  Europe, extra leagues) `COMP:date:home:away`. Competitions we don't configure are `AF<league id>`.
- **Seasons** are July-June codes: `2526` = 2025-26. API-Football's season year 2025 is `2526`.
- **Scores** are 90-minute scores. Extra time and penalties are separate columns.
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

## Checks run on every build (`build_log`)

- scores corrected to 90 minutes (old seeds that included extra time, fixed from API-Football's 90-minute score);
- score disagreements between our results and API-Football's 90-minute score (expected: a handful of awarded
  games, where football-data has the official awarded score and API-Football the score on the pitch);
- possible duplicate matches (same competition, date and home team);
- one API club matching two of our names (a spelling split in a source: add an alias);
- tracked clubs without an API-Football id.

Last full build (2026-10-14): 56.5k matches, 950k player-match rows, 1.58M lineup rows, 618k events, 125k injury
records; 5 disagreements (all awarded games), 5 possible duplicates (amateur cup ties), 0 spelling splits.

A non-zero count is a data problem to look at, not something to ignore.
