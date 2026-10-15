"""The model's data: the clean warehouse (data/warehouse.db, docs/DATA_MODEL.md) as one DataFrame.

Every model calculation (Dixon-Coles, the joint leagues + Europe model, Elo, rest days, referee rates,
corners / cards / shots, the backtests) reads this frame; match.db stays the pipeline's operational store
(fixtures, live results, predictions, tracking) and is one of the warehouse's inputs.

Columns keep the names the models always used (load_matches): fthg/ftag, xg_home/xg_away, hs/as_,
hst/ast, hc/ac, hf/af, hy/ay, hr/ar, close_*, referee, plus xg_source, pens_home/pens_away (penalties
awarded, from API-Football events) and comp_type (league / extra / europe / cup, API leagues 'league').
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from . import config

QUERY = """
SELECT m.match_id AS id, m.comp AS league, m.season, m.date, m.kickoff_utc, m.round, m.home, m.away,
       CASE WHEN m.status = 'NS' THEN 'SCHEDULED' ELSE m.status END AS status,
       m.home_goals AS fthg, m.away_goals AS ftag, m.home_xg AS xg_home, m.away_xg AS xg_away, m.xg_source,
       m.referee, m.close_home AS close_h, m.close_draw AS close_d, m.close_away AS close_a,
       m.close_over25 AS close_o25, m.close_under25 AS close_u25, m.understat_id, m.af_fixture_id, m.sources,
       h.shots AS hs, a.shots AS as_, h.shots_on_target AS hst, a.shots_on_target AS ast,
       h.corners AS hc, a.corners AS ac, h.fouls AS hf, a.fouls AS af, h.yellow AS hy, a.yellow AS ay,
       h.red AS hr, a.red AS ar, c.type AS comp_type
FROM matches m
LEFT JOIN team_match_stats h ON h.match_id = m.match_id AND h.side = 'h'
LEFT JOIN team_match_stats a ON a.match_id = m.match_id AND a.side = 'a'
LEFT JOIN competitions c ON c.comp = m.comp
"""

# penalties awarded in normal / extra time (a shootout is not a penalty award)
PENS = """
SELECT e.match_id, SUM(e.team = m.home) AS ph, SUM(e.team = m.away) AS pa
FROM events e JOIN matches m ON m.match_id = e.match_id
WHERE e.type = 'Goal' AND e.detail IN ('Penalty', 'Missed Penalty')
  AND NOT (m.pen_home IS NOT NULL AND e.minute >= 120)
GROUP BY e.match_id
"""


def warehouse_path() -> Path:
    from .warehouse import WAREHOUSE_DB
    return Path(WAREHOUSE_DB)


def load_frame(path: str | Path | None = None) -> pd.DataFrame:
    """All warehouse matches in the models' schema. Raises FileNotFoundError if the warehouse is missing:
    the model never falls back to older data."""
    from .ingest.api_football import normalise_referee
    path = Path(path or warehouse_path())
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - build it first (python -m matchpredictor warehouse)")
    w = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(QUERY, w)
        with_events = {r[0] for r in w.execute("SELECT DISTINCT match_id FROM events")}
        pens = pd.read_sql_query(PENS, w).set_index("match_id")
    finally:
        w.close()
    df["date"] = pd.to_datetime(df["date"])
    df["cards_h"] = df["hy"] + df["hr"]
    df["cards_a"] = df["ay"] + df["ar"]
    # penalties: 0 where the match has events but no penalty, unknown where it has no events at all
    has_ev = df.id.isin(with_events) & (df.status == "FT")
    df["pens_home"] = df.id.map(pens.ph).where(has_ev).fillna(0).where(has_ev)
    df["pens_away"] = df.id.map(pens.pa).where(has_ev).fillna(0).where(has_ev)
    df["referee"] = [normalise_referee(r) if isinstance(r, str) else None for r in df.referee]
    our = {c: v.get("type") for c, v in config.competitions(enabled_only=False).items()}
    df["comp_type"] = [our.get(c) or t for c, t in zip(df.league, df.comp_type)]
    return df


def comp_types(df: pd.DataFrame) -> dict[str, str]:
    """{comp: type} for every competition in the frame (ours from competitions.json, API ones from the
    warehouse: an API 'League' is a domestic league, e.g. a second division)."""
    return dict(zip(df.league, df.comp_type.fillna("cup")))


def prepare(notes: list[str] | None = None, main_path: str | Path | None = None) -> pd.DataFrame:
    """Bring the warehouse up to date with match.db and return the model frame.

    detail.db (Actions cache) gets the committed archives whenever a newer backfill wrote them
    (data/history/last_backfill.txt), then the warehouse is rebuilt from match.db + detail.db (~2 min),
    so live results stored this run are in the frame."""
    import time

    from . import detail, warehouse
    t0 = time.time()
    marker_file = config.DATA_DIR / "history" / "last_backfill.txt"
    marker = "archives:" + (marker_file.read_text().strip() if marker_file.exists() else "none")
    dconn = detail.connect()
    try:
        if not dconn.execute("SELECT 1 FROM af_progress WHERE key=?", (marker,)).fetchone():
            detail.load_archive(dconn)
            detail.load_af(dconn)
            dconn.execute("INSERT OR REPLACE INTO af_progress VALUES (?, datetime('now'))", (marker,))
            dconn.commit()
    finally:
        dconn.close()
    warehouse.build(main_path or config.DB_PATH)
    df = load_frame()
    if notes is not None and time.time() - t0 > 600:
        notes.append(f"Model data: warehouse rebuild took {time.time() - t0:.0f} s")
    return df
