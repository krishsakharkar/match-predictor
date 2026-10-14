"""Per-match detail: who played, their match stats, and every shot. Kept in its own SQLite file
(data/detail.db, gitignored) so the committed data/match.db stays small.

Durable copy: one gzipped CSV pair per finished league-season in data/history/understat/
(<LEAGUE>_<season>.players.csv.gz / .shots.csv.gz). Finished seasons never change, so they are
committed once; `load_archive` rebuilds detail.db from them in seconds, and only the current season
is downloaded again.
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
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DETAIL_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


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
    does not have yet. Resumable: raw responses are cached, so an interrupted run loses nothing."""
    have = {r[0] for r in dconn.execute("SELECT match_id FROM detail_matches")}
    ph = ",".join("?" * len(seasons))
    todo = [r for r in conn.execute(
        f"SELECT id, league, season, date, home, away, understat_id FROM matches WHERE status='FT' "
        f"AND understat_id IS NOT NULL AND season IN ({ph}) ORDER BY date", seasons) if r["id"] not in have]
    out = {"todo": len(todo), "stored": 0, "downloaded": 0, "failed": 0}
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
        _insert(dconn, {"match_id": r["id"], "league": r["league"], "season": r["season"], "date": r["date"],
                        "home": r["home"], "away": r["away"], "source": "understat",
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
