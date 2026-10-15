"""Per-match detail: who played, their match stats, and every shot. Kept in its own SQLite file
(data/detail.db, gitignored) so the committed data/match.db stays small.

Durable copy: gzipped CSVs per league-season in data/history/understat/ and per season in
data/history/api_football/. Finished seasons never change (same bytes on every export, so git stores them
once); the current season is re-exported on every backfill run. `load_archive` / `load_af` rebuild detail.db
in seconds, and only new matches are downloaded again.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .ingest import understat

log = logging.getLogger(__name__)
DETAIL_DB = config.DATA_DIR / "detail.db"
ARCHIVE_DIR = config.DATA_DIR / "history" / "understat"

PLAYER_COLS = ["match_id", "team", "side", "player_id", "player", "position", "started", "minutes", "goals",
               "own_goals", "assists", "shots", "key_passes", "xg", "xa", "xg_chain", "xg_buildup", "yellow", "red"]
SHOT_COLS = ["match_id", "team", "shot_id", "minute", "player_id", "player", "result", "x", "y", "xg", "situation",
             "shot_type", "assisted_by", "last_action"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS detail_matches (
    match_id TEXT PRIMARY KEY, league TEXT, season TEXT, date TEXT, home TEXT, away TEXT,
    source TEXT, source_id TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS player_matches (
    match_id TEXT, team TEXT, side TEXT, player_id INTEGER, player TEXT, position TEXT, started INTEGER,
    minutes INTEGER, goals INTEGER, own_goals INTEGER, assists INTEGER, shots INTEGER, key_passes INTEGER,
    xg REAL, xa REAL, xg_chain REAL, xg_buildup REAL, yellow INTEGER, red INTEGER,
    PRIMARY KEY (match_id, player_id));
CREATE INDEX IF NOT EXISTS ix_pm_player ON player_matches (player_id);
CREATE INDEX IF NOT EXISTS ix_pm_team ON player_matches (team);
CREATE TABLE IF NOT EXISTS shots (
    match_id TEXT, team TEXT, shot_id INTEGER PRIMARY KEY, minute INTEGER, player_id INTEGER, player TEXT,
    result TEXT, x REAL, y REAL, xg REAL, situation TEXT, shot_type TEXT, assisted_by TEXT, last_action TEXT);
CREATE INDEX IF NOT EXISTS ix_shots_match ON shots (match_id);

-- API-Football (ingest/api_football_history.py); season = the API's season start year
CREATE TABLE IF NOT EXISTS af_progress (key TEXT PRIMARY KEY, done_at TEXT);
CREATE TABLE IF NOT EXISTS af_teams (team_id INTEGER PRIMARY KEY, name TEXT, our_name TEXT, country TEXT, code TEXT,
    founded INTEGER);
CREATE TABLE IF NOT EXISTS af_competitions (league_id INTEGER, season INTEGER, name TEXT, type TEXT, country TEXT,
    events INTEGER, lineups INTEGER, stats_fixtures INTEGER, stats_players INTEGER, injuries INTEGER, players INTEGER,
    PRIMARY KEY (league_id, season));
CREATE TABLE IF NOT EXISTS af_team_competitions (team_id INTEGER, season INTEGER, league_id INTEGER,
    PRIMARY KEY (team_id, season, league_id));
CREATE TABLE IF NOT EXISTS af_fixtures (fixture_id INTEGER PRIMARY KEY, league_id INTEGER, season INTEGER, round TEXT,
    date TEXT, kickoff_utc TEXT, referee TEXT, venue TEXT, status TEXT, home_id INTEGER, away_id INTEGER, home TEXT,
    away TEXT, home_goals INTEGER, away_goals INTEGER, ht_home INTEGER, ht_away INTEGER, et_home INTEGER,
    et_away INTEGER, pen_home INTEGER, pen_away INTEGER, detail_at TEXT, ft_home INTEGER, ft_away INTEGER);
CREATE INDEX IF NOT EXISTS ix_aff_league ON af_fixtures (league_id, season);
CREATE TABLE IF NOT EXISTS af_team_stats (fixture_id INTEGER, season INTEGER, team_id INTEGER, stat TEXT, value TEXT,
    PRIMARY KEY (fixture_id, team_id, stat));
CREATE TABLE IF NOT EXISTS af_lineups (fixture_id INTEGER, season INTEGER, team_id INTEGER, formation TEXT,
    coach_id INTEGER, coach TEXT, player_id INTEGER, player TEXT, number INTEGER, pos TEXT, grid TEXT, started INTEGER,
    PRIMARY KEY (fixture_id, team_id, player_id));
CREATE TABLE IF NOT EXISTS af_player_match (fixture_id INTEGER, season INTEGER, team_id INTEGER, player_id INTEGER,
    player TEXT, minutes INTEGER, number INTEGER, position TEXT, rating REAL, captain INTEGER, substitute INTEGER,
    offsides INTEGER, shots INTEGER, shots_on INTEGER, goals INTEGER, conceded INTEGER, assists INTEGER, saves INTEGER,
    passes INTEGER, key_passes INTEGER, pass_accuracy INTEGER, tackles INTEGER, blocks INTEGER, interceptions INTEGER,
    duels INTEGER, duels_won INTEGER, dribbles INTEGER, dribbles_won INTEGER, dribbled_past INTEGER,
    fouls_drawn INTEGER, fouls_committed INTEGER, yellow INTEGER, red INTEGER, pen_won INTEGER, pen_committed INTEGER,
    pen_scored INTEGER, pen_missed INTEGER, pen_saved INTEGER, PRIMARY KEY (fixture_id, team_id, player_id));
CREATE INDEX IF NOT EXISTS ix_afpm_player ON af_player_match (player_id);
CREATE TABLE IF NOT EXISTS af_events (fixture_id INTEGER, season INTEGER, seq INTEGER, team_id INTEGER, minute INTEGER,
    extra INTEGER, type TEXT, detail TEXT, player_id INTEGER, player TEXT, assist_id INTEGER, assist TEXT,
    comments TEXT, PRIMARY KEY (fixture_id, seq));
CREATE TABLE IF NOT EXISTS af_injuries (fixture_id INTEGER, season INTEGER, league_id INTEGER, team_id INTEGER,
    player_id INTEGER, player TEXT, type TEXT, reason TEXT, date TEXT, PRIMARY KEY (fixture_id, player_id));
CREATE TABLE IF NOT EXISTS af_player_seasons (season INTEGER, player_id INTEGER, team_id INTEGER, league_id INTEGER,
    player TEXT, firstname TEXT, lastname TEXT, birth_date TEXT, nationality TEXT, height TEXT, weight TEXT,
    position TEXT, appearances INTEGER, lineups INTEGER, minutes INTEGER, rating REAL, goals INTEGER, assists INTEGER,
    shots INTEGER, shots_on INTEGER, passes INTEGER, key_passes INTEGER, tackles INTEGER, duels_won INTEGER,
    dribbles_won INTEGER, fouls_committed INTEGER, yellow INTEGER, red INTEGER, pen_scored INTEGER, pen_missed INTEGER,
    PRIMARY KEY (season, player_id, team_id, league_id));
CREATE TABLE IF NOT EXISTS af_transfers (player_id INTEGER, player TEXT, date TEXT, type TEXT, team_in_id INTEGER,
    team_in TEXT, team_out_id INTEGER, team_out TEXT, PRIMARY KEY (player_id, date, team_in_id, team_out_id));
"""

# archived API-Football tables: per-season tables -> <table>_<season>.csv.gz, the rest -> <table>.csv.gz
AF_SEASON_TABLES = ("af_competitions", "af_team_competitions", "af_fixtures", "af_team_stats", "af_lineups",
                    "af_player_match", "af_events", "af_injuries", "af_player_seasons")
AF_OTHER_TABLES = ("af_teams", "af_transfers")
AF_ARCHIVE_DIR = config.DATA_DIR / "history" / "api_football"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DETAIL_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")    # the warehouse build can read while a download writes
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


# columns added after the first archives were written (load_af tolerates archives without them)
MIGRATIONS = [("af_fixtures", "ft_home", "INTEGER"), ("af_fixtures", "ft_away", "INTEGER")]


def _insert(dconn, meta: dict, players: list[dict], shots: list[dict]) -> None:
    dconn.execute("DELETE FROM player_matches WHERE match_id=?", (meta["match_id"],))
    dconn.execute("DELETE FROM shots WHERE match_id=?", (meta["match_id"],))
    dconn.executemany(f"INSERT OR REPLACE INTO player_matches ({','.join(PLAYER_COLS)}) VALUES "
                      f"({','.join('?' * len(PLAYER_COLS))})", [[p[c] for c in PLAYER_COLS] for p in players])
    dconn.executemany(f"INSERT OR REPLACE INTO shots ({','.join(SHOT_COLS)}) VALUES "
                      f"({','.join('?' * len(SHOT_COLS))})", [[s[c] for c in SHOT_COLS] for s in shots])
    dconn.execute("INSERT OR REPLACE INTO detail_matches VALUES (?,?,?,?,?,?,?,?,?)",
                  (meta["match_id"], meta["league"], meta["season"], meta["date"], meta["home"], meta["away"],
                   meta["source"], meta["source_id"], meta["fetched_at"]))


def sync_understat(conn, dconn, seasons: list[str], notes: list[str], pause: float = 1.0,
                   limit: int | None = None) -> dict:
    """Fetch rosters + shots for every finished match with an Understat id in `seasons` that detail.db
    does not have yet, newest first (the current season matters most). Resumable: raw responses are cached,
    so an interrupted run loses nothing."""
    have = {r[0] for r in dconn.execute("SELECT match_id FROM detail_matches")}
    ph = ",".join("?" * len(seasons))
    todo = [r for r in conn.execute(
        f"SELECT id, league, season, date, home, away, understat_id FROM matches WHERE status='FT' "
        f"AND understat_id IS NOT NULL AND season IN ({ph}) ORDER BY date DESC", seasons) if r["id"] not in have]
    out = {"todo": len(todo), "stored": 0, "downloaded": 0, "failed": 0, "empty": 0}
    for r in todo[:limit] if limit else todo:
        try:
            payload, downloaded = understat.fetch_match(r["understat_id"], pause=pause)
        except understat.UnderstatUnavailable as e:
            out["failed"] += 1
            if out["failed"] <= 5:
                notes.append(f"Understat match detail {r['id']}: {e}")
            if out["failed"] >= 25 and out["stored"] == 0:
                notes.append("Understat match detail: 25 failures in a row - stopping")
                break
            continue
        players, shots = understat.match_rows(payload, r["id"], r["home"], r["away"])
        if not players:
            # Understat lists the match but has no rosters / shots (id 27930): recorded as a gap at the
            # source (archived with the season, never re-fetched), with no rows
            out["empty"] += 1
            notes.append(f"Understat has no player data for {r['id']} (understat id {r['understat_id']})")
        _insert(dconn, {"match_id": r["id"], "league": r["league"], "season": r["season"], "date": r["date"],
                        "home": r["home"], "away": r["away"], "source": "understat" if players else "understat-empty",
                        "source_id": str(r["understat_id"]),
                        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, players, shots)
        out["stored"] += 1
        out["downloaded"] += downloaded
        if out["stored"] % 100 == 0:
            dconn.commit()
            log.info("understat detail: %d/%d stored", out["stored"], len(todo))
    dconn.commit()
    return out


def export_archive(dconn, league: str, season: str, out_dir: Path | None = None) -> dict:
    """Write one league-season to <out_dir>/<league>_<season>.{players,shots}.csv.gz (+ a matches file)."""
    out_dir = Path(out_dir or ARCHIVE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = [r[0] for r in dconn.execute("SELECT match_id FROM detail_matches WHERE league=? AND season=? "
                                       "ORDER BY date, match_id", (league, season))]
    counts = {"matches": len(ids)}
    queries = {
        "matches": ("SELECT * FROM detail_matches WHERE league=? AND season=? ORDER BY date, match_id",
                    ["match_id", "league", "season", "date", "home", "away", "source", "source_id", "fetched_at"]),
        "players": (f"SELECT {','.join('p.' + c for c in PLAYER_COLS)} FROM player_matches p JOIN detail_matches d "
                    "USING (match_id) WHERE d.league=? AND d.season=? ORDER BY d.date, p.match_id, p.side, "
                    "p.started DESC, p.player_id", PLAYER_COLS),
        "shots": (f"SELECT {','.join('s.' + c for c in SHOT_COLS)} FROM shots s JOIN detail_matches d "
                  "USING (match_id) WHERE d.league=? AND d.season=? ORDER BY s.shot_id", SHOT_COLS),
    }
    for kind, (sql, cols) in queries.items():
        rows = dconn.execute(sql, (league, season)).fetchall()
        # mtime=0: identical content -> identical bytes, so re-exporting a finished season changes nothing in git
        with gzip.GzipFile(out_dir / f"{league}_{season}.{kind}.csv.gz", "wb", mtime=0) as gz, \
                io.TextIOWrapper(gz, encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([[r[c] for c in cols] for r in rows])
        counts[kind] = len(rows)
    return counts


def load_archive(dconn, in_dir: Path | None = None) -> dict:
    """Load every archived league-season not already in detail.db."""
    in_dir = Path(in_dir or ARCHIVE_DIR)
    have = {r[0] for r in dconn.execute("SELECT match_id FROM detail_matches")}
    out = {}
    for mpath in sorted(in_dir.glob("*.matches.csv.gz")):
        stem = mpath.name[: -len(".matches.csv.gz")]
        with gzip.open(mpath, "rt", encoding="utf-8", newline="") as f:
            metas = [m for m in csv.DictReader(f) if m["match_id"] not in have]
        if not metas:
            continue
        want = {m["match_id"] for m in metas}
        by = {"players": {}, "shots": {}}
        for kind in by:
            with gzip.open(in_dir / f"{stem}.{kind}.csv.gz", "rt", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row["match_id"] in want:
                        by[kind].setdefault(row["match_id"], []).append({k: (v if v != "" else None)
                                                                         for k, v in row.items()})
        for m in metas:
            _insert(dconn, m, by["players"].get(m["match_id"], []), by["shots"].get(m["match_id"], []))
        out[stem] = len(metas)
    dconn.commit()
    return out


def _cols(dconn, table: str) -> list[str]:
    return [r[1] for r in dconn.execute(f"PRAGMA table_info({table})")]


def _pk(dconn, table: str) -> list[str]:
    return [r[1] for r in sorted(dconn.execute(f"PRAGMA table_info({table})"), key=lambda r: r[5]) if r[5]]


def _write_gz(path: Path, cols: list[str], rows) -> int:
    n = 0
    with gzip.GzipFile(path, "wb", mtime=0) as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(list(r))
            n += 1
    return n


def export_af(dconn, finished_before: int, out_dir: Path | None = None) -> dict:
    """Archive API-Football tables: every season < finished_before one file per season (the CLI passes the
    current season + 1, so this season is stored too), plus the season-less tables. Sorted by primary key and written with mtime=0, so unchanged data gives
    unchanged files."""
    out_dir = Path(out_dir or AF_ARCHIVE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for t in AF_SEASON_TABLES:
        cols, order = _cols(dconn, t), ",".join(_pk(dconn, t))
        for (season,) in dconn.execute(f"SELECT DISTINCT season FROM {t} WHERE season < ?", (finished_before,)).fetchall():
            out[f"{t}_{season}"] = _write_gz(out_dir / f"{t}_{season}.csv.gz", cols, dconn.execute(
                f"SELECT {','.join(cols)} FROM {t} WHERE season=? ORDER BY {order}", (season,)))
    for t in AF_OTHER_TABLES:
        cols, order = _cols(dconn, t), ",".join(_pk(dconn, t))
        out[t] = _write_gz(out_dir / f"{t}.csv.gz", cols, dconn.execute(f"SELECT {','.join(cols)} FROM {t} ORDER BY {order}"))
    return out


def load_af(dconn, in_dir: Path | None = None) -> dict:
    """Load archived API-Football tables (INSERT OR REPLACE: rows fetched later win on the next export)."""
    in_dir = Path(in_dir or AF_ARCHIVE_DIR)
    out = {}
    for path in sorted(in_dir.glob("af_*.csv.gz")):
        table = next((t for t in AF_SEASON_TABLES + AF_OTHER_TABLES
                      if path.name == f"{t}.csv.gz" or path.name.startswith(f"{t}_")), None)
        if table is None:
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            cols = next(r)
            rows = [[v if v != "" else None for v in row] for row in r]
        dconn.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
        out[path.name] = len(rows)
    # archived seasons are complete: mark them so a resumed backfill does not fetch them again
    for t, kind in (("af_fixtures", "fixtures"), ("af_injuries", "injuries")):
        dconn.execute(f"INSERT OR IGNORE INTO af_progress SELECT DISTINCT '{kind}:' || league_id || ':' || season, "
                      f"'archive' FROM {t}")
    dconn.execute("INSERT OR IGNORE INTO af_progress SELECT DISTINCT 'leagues:' || team_id || ':' || season, 'archive' "
                  "FROM af_team_competitions")
    dconn.execute("INSERT OR IGNORE INTO af_progress SELECT DISTINCT 'players:' || team_id || ':' || season, 'archive' "
                  "FROM af_player_seasons")
    dconn.commit()
    return out
