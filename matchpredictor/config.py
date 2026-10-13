"""Configuration loading. Everything tunable lives in config/*.json."""
from __future__ import annotations

import difflib
import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
DB_PATH = Path(os.environ.get("MATCH_DB", DATA_DIR / "match.db"))


@lru_cache
def settings() -> dict:
    return json.loads((CONFIG_DIR / "settings.json").read_text())


@lru_cache
def leagues(enabled_only: bool = True) -> dict:
    all_leagues = json.loads((CONFIG_DIR / "leagues.json").read_text())
    return {k: v for k, v in all_leagues.items() if v.get("enabled", True) or not enabled_only}


@lru_cache
def _aliases() -> dict:
    raw = json.loads((CONFIG_DIR / "team_aliases.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def canonical_team(name: str, known: set[str] | None = None) -> str:
    """Map a provider team name onto the football-data.co.uk name.

    Uses the alias table first, then an exact match against known names, then a
    conservative fuzzy match. Falls back to the input unchanged.
    """
    name = name.strip()
    aliases = _aliases()
    if name in aliases:
        return aliases[name]
    if known:
        if name in known:
            return name
        match = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.85)
        if match:
            return match[0]
    return name


def season_code(d: date | datetime | str) -> str:
    """football-data season code: matches from July 2026 onwards are season '2627'."""
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace("Z", "+00:00"))
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def previous_seasons(current: str, n: int) -> list[str]:
    """['2627', 3] -> ['2324', '2425', '2526'] (oldest first, excluding current)."""
    start = int(current[:2])
    out = []
    for i in range(n, 0, -1):
        s = (start - i) % 100
        out.append(f"{s:02d}{(s + 1) % 100:02d}")
    return out
