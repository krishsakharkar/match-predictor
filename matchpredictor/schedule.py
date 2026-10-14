"""Decide what an hourly cron tick should do. Standard library only (runs before deps install).

    daily       -> at the configured hour in US Eastern time (09:xx ET)
    prekickoff  -> a kickoff falls 45-105 minutes from now (hourly ticks hit each kickoff once)
    results     -> a shown match kicked off 2-12 hours ago and has no result yet (GitHub often skips
                   scheduled ticks for hours, so the window is wide; /scores responses are cached)
    skip        -> otherwise
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _settings():
    return json.loads((ROOT / "config" / "settings.json").read_text())


def gate(now: datetime | None = None, db_path: str | Path | None = None) -> str:
    from . import config
    now = now or datetime.now(timezone.utc)
    sched = _settings()["schedule"]
    if now.astimezone(ZoneInfo("America/New_York")).hour == sched["daily_hour_et"]:
        return "daily"
    db_path = Path(db_path or config.DB_PATH)
    if not db_path.exists():
        return "skip"
    lo, hi = sched["prekickoff_window_minutes"]
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = (now + timedelta(minutes=lo)).astimezone(timezone.utc).strftime(fmt)
    end = (now + timedelta(minutes=hi)).astimezone(timezone.utc).strftime(fmt)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT league, home, away FROM matches WHERE status='SCHEDULED' AND kickoff_utc >= ? "
                            "AND kickoff_utc < ?", (start, end)).fetchall()
        n = sum(1 for lg, h, a in rows if config.is_displayed(lg, h, a))
    except sqlite3.OperationalError:
        n = 0
    finally:
        conn.close()
    if n:
        return "prekickoff"
    # a shown match kicked off 2-12 hours ago and still has no result -> fetch scores
    r_start = (now - timedelta(hours=12)).astimezone(timezone.utc).strftime(fmt)
    r_end = (now - timedelta(hours=2)).astimezone(timezone.utc).strftime(fmt)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT league, home, away FROM matches WHERE status='SCHEDULED' AND kickoff_utc >= ? "
                            "AND kickoff_utc < ?", (r_start, r_end)).fetchall()
        pending = sum(1 for lg, h, a in rows if config.is_displayed(lg, h, a))
    except sqlite3.OperationalError:
        pending = 0
    finally:
        conn.close()
    return "results" if pending else "skip"
