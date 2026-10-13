"""SQLite storage. One file (data/match.db) holds matches, odds, predictions, bets and API caches."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,               -- league:season:home:away (each pairing occurs once per season)
    league TEXT NOT NULL,
    season TEXT NOT NULL,
    date TEXT NOT NULL,                -- YYYY-MM-DD
    kickoff_utc TEXT,                  -- ISO8601, when known
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SCHEDULED',   -- SCHEDULED | FT
    fthg INTEGER, ftag INTEGER,
    hs INTEGER, as_ INTEGER, hst INTEGER, ast INTEGER,
    hc INTEGER, ac INTEGER, hf INTEGER, af INTEGER,
    hy INTEGER, ay INTEGER, hr INTEGER, ar INTEGER,
    referee TEXT,
    close_h REAL, close_d REAL, close_a REAL, close_o25 REAL, close_u25 REAL,
    xg_home REAL, xg_away REAL,        -- Understat expected goals
    understat_id TEXT,
    pens_home INTEGER, pens_away INTEGER,   -- penalties awarded (API-Football events, when available)
    odds_api_id TEXT,
    api_football_id INTEGER,
    source TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_matches_league_date ON matches(league, date);
CREATE INDEX IF NOT EXISTS idx_matches_home_date ON matches(home, date);
CREATE INDEX IF NOT EXISTS idx_matches_away_date ON matches(away, date);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market TEXT NOT NULL,              -- h2h | totals | btts
    selection TEXT NOT NULL,           -- home | draw | away | over | under | yes | no
    line REAL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(match_id, fetched_at);

CREATE TABLE IF NOT EXISTS predictions (
    match_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    payload TEXT NOT NULL              -- full prediction JSON; frozen once kickoff passes
);

CREATE TABLE IF NOT EXISTS prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    p_home REAL, p_draw REAL, p_away REAL,
    xg_home REAL, xg_away REAL,
    p_over25 REAL, p_btts REAL
);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL,
    model_prob REAL NOT NULL,
    market_prob REAL,                  -- vig-free book probability when the bet was flagged
    odds_taken REAL NOT NULL,
    edge REAL NOT NULL,
    ev REAL NOT NULL,
    stake REAL NOT NULL,               -- fraction of bankroll
    taken_at TEXT NOT NULL,
    closing_odds REAL,
    result TEXT,                       -- win | loss | void
    pnl REAL                           -- in bankroll fractions
);
-- one bet per selection; NULL lines (1X2, BTTS) must collide too, hence COALESCE
CREATE UNIQUE INDEX IF NOT EXISTS uq_bets_selection ON bets(match_id, market, selection, COALESCE(line, -1));

CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    remaining INTEGER,
    PRIMARY KEY(provider, day)
);

CREATE TABLE IF NOT EXISTS player_stats (
    league TEXT NOT NULL,
    season TEXT NOT NULL,
    team TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    player TEXT NOT NULL,
    apps INTEGER, minutes INTEGER, goals INTEGER, shots INTEGER, sot INTEGER, assists INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(league, season, player_id)
);

CREATE TABLE IF NOT EXISTS absences (
    match_id TEXT NOT NULL,
    team TEXT NOT NULL,
    player_id INTEGER,
    player TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY(match_id, team, player)
);

CREATE TABLE IF NOT EXISTS lineups (
    match_id TEXT NOT NULL,
    team TEXT NOT NULL,
    payload TEXT NOT NULL,             -- JSON list of {name, number, pos}
    PRIMARY KEY(match_id, team)
);

CREATE TABLE IF NOT EXISTS understat_players (
    league TEXT NOT NULL,
    season TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    player TEXT NOT NULL,
    team TEXT NOT NULL,
    position TEXT,                     -- Understat position string, 'GK' for goalkeepers
    games INTEGER, minutes INTEGER, goals INTEGER, xg REAL, npxg REAL, assists INTEGER, xa REAL, shots INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(league, season, player_id)
);

CREATE TABLE IF NOT EXISTS elo_ratings (
    club TEXT NOT NULL,                -- ClubElo club name
    country TEXT,
    level INTEGER,
    elo REAL NOT NULL,
    date_from TEXT NOT NULL,           -- rating valid from..to inclusive (ClubElo intervals)
    date_to TEXT NOT NULL,
    PRIMARY KEY(club, date_from)
);
CREATE INDEX IF NOT EXISTS idx_elo_club_to ON elo_ratings(club, date_to);

CREATE TABLE IF NOT EXISTS model_params (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,               -- JSON
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    notes TEXT
);
"""


# Columns added after the first release; CREATE TABLE IF NOT EXISTS won't add them to an existing DB.
MIGRATIONS = [("player_stats", "assists", "INTEGER"), ("matches", "xg_home", "REAL"), ("matches", "xg_away", "REAL"),
              ("matches", "understat_id", "TEXT"), ("matches", "pens_home", "INTEGER"),
              ("matches", "pens_away", "INTEGER")]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn


@contextmanager
def session(path: Path | str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def match_id(league: str, season: str, home: str, away: str, date: str | None = None) -> str:
    """League pairings happen once per season, so league ids omit the date (sources merge even if a
    game is rescheduled). Cup and European ties can repeat (two legs, league phase + knockouts),
    so their ids include the date."""
    comp = config.competitions(enabled_only=False).get(league)
    if comp and comp["type"] != "league":
        return f"{league}:{date}:{home}:{away}"
    return f"{league}:{season}:{home}:{away}"


def get_param(conn, key: str):
    row = conn.execute("SELECT value FROM model_params WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def set_param(conn, key: str, value, updated_at: str) -> None:
    conn.execute("INSERT INTO model_params (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                 "SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), updated_at))


MATCH_COLUMNS = [
    "league", "season", "date", "kickoff_utc", "home", "away", "status",
    "fthg", "ftag", "hs", "as_", "hst", "ast", "hc", "ac", "hf", "af",
    "hy", "ay", "hr", "ar", "referee",
    "close_h", "close_d", "close_a", "close_o25", "close_u25",
    "odds_api_id", "api_football_id", "source", "xg_home", "xg_away", "understat_id", "pens_home", "pens_away",
]


def upsert_match(conn: sqlite3.Connection, row: dict) -> str:
    """Insert or merge a match. Non-null incoming values overwrite; nulls never erase data."""
    mid = match_id(row["league"], row["season"], row["home"], row["away"], row.get("date"))
    cols = [c for c in MATCH_COLUMNS if c in row]
    placeholders = ", ".join("?" for _ in cols)
    special = {
        # a finished match never goes back to scheduled
        "status": "status = CASE WHEN matches.status = 'FT' THEN 'FT' ELSE excluded.status END",
        # fixture feeds must not relabel rows that came from a results source
        "source": "source = CASE WHEN excluded.source IN ('football-data', 'mirror') OR matches.source IS NULL "
                  "THEN excluded.source ELSE matches.source END",
    }
    updates = ", ".join(special.get(c, f"{c} = COALESCE(excluded.{c}, matches.{c})") for c in cols)
    conn.execute(
        f"INSERT INTO matches (id, {', '.join(cols)}) VALUES (?, {placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at = CURRENT_TIMESTAMP",
        [mid] + [row[c] for c in cols],
    )
    return mid


def save_prediction(conn, match_id_: str, created_at: str, model_version: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO predictions (match_id, created_at, model_version, payload) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(match_id) DO UPDATE SET created_at = excluded.created_at, "
        "model_version = excluded.model_version, payload = excluded.payload",
        (match_id_, created_at, model_version, json.dumps(payload, separators=(",", ":"))),
    )
    m = payload["markets"]
    conn.execute(
        "INSERT INTO prediction_log (match_id, created_at, model_version, p_home, p_draw, p_away, "
        "xg_home, xg_away, p_over25, p_btts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (match_id_, created_at, model_version, m["1x2"]["home"], m["1x2"]["draw"], m["1x2"]["away"],
         payload["xg"]["home"], payload["xg"]["away"], m["totals"]["2.5"]["over"], m["btts"]["yes"]),
    )
