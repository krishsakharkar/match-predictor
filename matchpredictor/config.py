"""Configuration loading. Everything tunable lives in config/*.json."""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
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
def competitions(enabled_only: bool = True) -> dict:
    """Every competition we show matches for (leagues, European cups, domestic cups)."""
    raw = json.loads((CONFIG_DIR / "competitions.json").read_text())
    return {k: v for k, v in raw.items() if v.get("enabled", True) or not enabled_only}


def leagues() -> dict:
    """Domestic leagues with football-data.co.uk history (the ones a Dixon-Coles model is fitted for)."""
    return {k: v for k, v in competitions().items() if v["type"] == "league" and v.get("football_data_code")}


def extra_leagues() -> dict:
    """Results-only leagues (Portugal, Netherlands, ...) that feed the joint cross-league model so clubs
    from outside the five big leagues are rated from their domestic games. Never displayed."""
    return {k: v for k, v in competitions().items() if v["type"] == "extra"}


def tracked_teams() -> set[str]:
    return set(settings()["tracked_teams"])


def is_displayed(comp: str, home: str, away: str) -> bool:
    """Full-coverage competitions show every match; the rest only matches of tracked teams."""
    c = competitions().get(comp)
    if not c:
        return False
    if c["coverage"] == "full":
        return True
    if c["coverage"] == "none":
        return False
    t = tracked_teams()
    return home in t or away in t


@lru_cache
def _aliases() -> dict:
    raw = json.loads((CONFIG_DIR / "team_aliases.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache
def elo_aliases() -> dict:
    raw = json.loads((CONFIG_DIR / "elo_aliases.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# Club-type affixes that differ between providers ("VfB Stuttgart" vs "Stuttgart").
_AFFIXES = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "rc", "rcd", "fsv", "vfb", "vfl", "tsg", "bc", "ud",
            "cd", "ca", "sd", "club", "1", "jk", "fk", "sk", "calcio", "de", "the"}


def normalise(name: str) -> str:
    s = name.lower().replace("ø", "o").replace("æ", "ae").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(t for t in s.split() if t not in _AFFIXES)


def match_name(name: str, known: set[str] | list[str], cutoff: float = 0.85) -> str | None:
    """Best match of `name` among `known`: exact, normalised, unique token containment, then fuzzy."""
    if not known:
        return None
    known = set(known)
    if name in known:
        return name
    norm = {normalise(k): k for k in known}
    n = normalise(name)
    if n in norm:
        return norm[n]
    toks = set(n.split())
    # containment only with names that normalise losslessly: "Paris FC" -> "paris" must not swallow
    # "Paris Saint-Germain"
    full = {nk: k for nk, k in norm.items() if len(nk.split()) == len(re.sub(r"[^a-z0-9 ]+", " ", k.lower()).split())}
    contained = [k for nk, k in full.items() if nk and set(nk.split()) <= toks]
    if len(contained) == 1:
        return contained[0]
    containing = [k for nk, k in full.items() if toks and toks <= set(nk.split())]
    if len(containing) == 1:
        return containing[0]
    m = difflib.get_close_matches(n, list(norm), n=1, cutoff=cutoff)
    return norm[m[0]] if m else None


def canonical_team(name: str, known: set[str] | None = None) -> str:
    """Map a provider team name onto the football-data.co.uk name (or keep it if no match)."""
    name = name.strip()
    aliases = _aliases()
    if name in aliases:
        return aliases[name]
    if known:
        m = match_name(name, known)
        if m:
            return m
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
