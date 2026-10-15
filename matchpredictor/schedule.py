"""Decide what an hourly cron tick should do. Standard library only (runs before deps install).

    daily       -> at the configured hour in US Eastern time (09:xx ET)
    prekickoff  -> a kickoff falls 45-105 minutes from now (hourly ticks hit each kickoff once)
    backfill    -> weekly data refresh (API-Football current season + new Understat matches): Mondays in the
                   backfill hours (06:00-08:59 ET); if GitHub skipped those ticks, the same hours on any later day
                   once the last backfill (data/history/last_backfill.txt) is 8+ days old
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


def backfill_due(now: datetime, marker: Path, sched: dict) -> bool:
    et = now.astimezone(ZoneInfo("America/New_York"))
    lo, hi = sched.get("backfill_hours_et", [6, 8])
    if not lo <= et.hour <= hi:
        return False
    try:
        last = datetime.fromisoformat(marker.read_text().strip().replace("Z", "+00:00"))
    except (OSError, ValueError):
        last = None
    if last and now - last < timedelta(hours=20):
        return False                      # already ran today
    if et.weekday() == sched.get("backfill_weekday", 0):
        return True
    return last is not None and now - last >= timedelta(days=sched.get("backfill_every_days", 7) + 1)


def gate(now: datetime | None = None, db_path: str | Path | None = None) -> str:
    from . import config
    now = now or datetime.now(timezone.utc)
    sched = _settings()["schedule"]
    if now.astimezone(ZoneInfo("America/New_York")).hour == sched["daily_hour_et"]:
        return "daily"
    db_path = Path(db_path or config.DB_PATH)
    marker = db_path.parent / "history" / "last_backfill.txt"
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
    if backfill_due(now, marker, sched):
        return "backfill"
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
