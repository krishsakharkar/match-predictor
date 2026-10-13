"""football-data.co.uk historical results + stats + closing odds.

Primary: https://www.football-data.co.uk/mmz4281/{season}/{code}.csv
Fallback: the github.com/datasets/football-datasets mirror (same stats, no odds),
used only when the primary host is unreachable.
"""
from __future__ import annotations

import io
import logging
import math

import pandas as pd
import requests

from .. import config, db

log = logging.getLogger(__name__)

PRIMARY = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FIXTURES = "https://www.football-data.co.uk/fixtures.csv"
MIRROR = ("https://raw.githubusercontent.com/datasets/football-datasets/main/datasets/"
          "{slug}/season-{season}.csv")

STAT_COLS = {
    "FTHG": "fthg", "FTAG": "ftag", "HS": "hs", "AS": "as_", "HST": "hst", "AST": "ast",
    "HC": "hc", "AC": "ac", "HF": "hf", "AF": "af", "HY": "hy", "AY": "ay", "HR": "hr", "AR": "ar",
}
# closing-odds column preference: Pinnacle closing, then market-average closing
CLOSE_1X2 = [("PSCH", "PSCD", "PSCA"), ("AvgCH", "AvgCD", "AvgCA"), ("B365CH", "B365CD", "B365CA")]
CLOSE_OU = [("PC>2.5", "PC<2.5"), ("AvgC>2.5", "AvgC<2.5"), ("B365C>2.5", "B365C<2.5")]


def _get(url: str, timeout: int = 30) -> str | None:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        raw = r.content
        for enc in ("utf-8-sig", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    except requests.RequestException as e:
        log.info("fetch failed %s: %s", url, e)
    return None


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _int(v):
    f = _num(v)
    return None if f is None else int(f)


def parse_csv(text: str, league: str, season: str, source: str) -> list[dict]:
    df = pd.read_csv(io.StringIO(text), on_bad_lines="skip")
    df = df.dropna(subset=["HomeTeam", "AwayTeam"])
    rows = []
    for _, r in df.iterrows():
        d = pd.to_datetime(str(r["Date"]), dayfirst="/" in str(r["Date"]), errors="coerce")
        if pd.isna(d):
            continue
        row = {
            "league": league, "season": season, "date": d.strftime("%Y-%m-%d"),
            "home": str(r["HomeTeam"]).strip(), "away": str(r["AwayTeam"]).strip(),
            "source": source,
            "close_h": None, "close_d": None, "close_a": None, "close_o25": None, "close_u25": None,
        }
        for src, dst in STAT_COLS.items():
            row[dst] = _int(r.get(src))
        ref = r.get("Referee")
        row["referee"] = str(ref).strip() if isinstance(ref, str) and ref.strip() else None
        row["status"] = "FT" if row["fthg"] is not None and row["ftag"] is not None else "SCHEDULED"
        for cols in CLOSE_1X2:
            vals = [_num(r.get(c)) for c in cols]
            if all(v and v > 1 for v in vals):
                row["close_h"], row["close_d"], row["close_a"] = vals
                break
        for cols in CLOSE_OU:
            vals = [_num(r.get(c)) for c in cols]
            if all(v and v > 1 for v in vals):
                row["close_o25"], row["close_u25"] = vals
                break
        rows.append(row)
    return rows


def fetch_season(league_key: str, league_cfg: dict, season: str) -> tuple[list[dict], str]:
    text = _get(PRIMARY.format(season=season, code=league_cfg["football_data_code"]))
    if text and "HomeTeam" in text:
        return parse_csv(text, league_key, season, "football-data"), "football-data"
    slug = league_cfg.get("mirror_slug")
    if slug:
        text = _get(MIRROR.format(slug=slug, season=season))
        if text and "HomeTeam" in text:
            return parse_csv(text, league_key, season, "mirror"), "mirror"
    return [], "unavailable"


def season_complete(conn, league: str, season: str) -> bool:
    n_teams = conn.execute(
        "SELECT COUNT(DISTINCT home) FROM matches WHERE league=? AND season=? AND status='FT'",
        (league, season)).fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM matches WHERE league=? AND season=? AND status='FT'",
                     (league, season)).fetchone()[0]
    from_mirror = conn.execute("SELECT COUNT(*) FROM matches WHERE league=? AND season=? AND source='mirror'",
                               (league, season)).fetchone()[0]
    # mirror rows lack closing odds, so keep retrying the primary source for them
    return n_teams >= 18 and n >= n_teams * (n_teams - 1) and from_mirror == 0


def update_history(conn, current_season: str, notes: list[str], refresh_completed=False) -> dict:
    n_seasons = config.settings()["history_seasons"]
    seasons = config.previous_seasons(current_season, n_seasons) + [current_season]
    summary = {}
    for key, lg in config.leagues().items():
        for s in seasons:
            if s != current_season and not refresh_completed and season_complete(conn, key, s):
                continue
            rows, src = fetch_season(key, lg, s)
            for row in rows:
                db.upsert_match(conn, row)
            summary[f"{key}:{s}"] = (len(rows), src)
            if src == "mirror":
                notes.append(f"{lg['name']} {s}: football-data.co.uk unreachable, used GitHub mirror (no closing odds)")
            elif src == "unavailable" and s != current_season:
                notes.append(f"{lg['name']} {s}: no historical data available")
            elif src == "unavailable":
                notes.append(f"{lg['name']} {s}: current-season results unavailable - "
                             "ratings use previous seasons only")
    return summary


def fetch_fixtures_csv() -> list[dict]:
    """Upcoming fixtures (with referee for English leagues). Empty if unreachable."""
    text = _get(FIXTURES)
    if not text or "HomeTeam" not in text:
        return []
    df = pd.read_csv(io.StringIO(text), on_bad_lines="skip")
    code_to_league = {v["football_data_code"]: k for k, v in config.leagues().items()}
    out = []
    for _, r in df.iterrows():
        lg = code_to_league.get(str(r.get("Div")))
        if not lg:
            continue
        d = pd.to_datetime(str(r["Date"]), dayfirst=True, errors="coerce")
        if pd.isna(d):
            continue
        ref = r.get("Referee")
        out.append({
            "league": lg, "date": d.strftime("%Y-%m-%d"), "home": str(r["HomeTeam"]).strip(),
            "away": str(r["AwayTeam"]).strip(),
            "referee": str(ref).strip() if isinstance(ref, str) and ref.strip() else None,
        })
    return out
