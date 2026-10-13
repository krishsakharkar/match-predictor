"""Tiny SQLite-backed response cache + per-provider usage tracking."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def cache_get(conn, key: str, ttl: timedelta):
    row = conn.execute("SELECT fetched_at, body FROM api_cache WHERE key=?", (key,)).fetchone()
    if row and now_utc() - parse_iso(row["fetched_at"]) < ttl:
        return json.loads(row["body"])
    return None


def cache_put(conn, key: str, body) -> None:
    conn.execute(
        "INSERT INTO api_cache (key, fetched_at, body) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET fetched_at=excluded.fetched_at, body=excluded.body",
        (key, iso(now_utc()), json.dumps(body, separators=(",", ":"))),
    )
    conn.commit()


def record_usage(conn, provider: str, used_delta: int, remaining: int | None) -> None:
    day = now_utc().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO api_usage (provider, day, used, remaining) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider, day) DO UPDATE SET used = used + excluded.used, "
        "remaining = COALESCE(excluded.remaining, remaining)",
        (provider, day, used_delta, remaining),
    )
    conn.commit()


def usage_today(conn, provider: str) -> int:
    row = conn.execute("SELECT used FROM api_usage WHERE provider=? AND day=?",
                       (provider, now_utc().strftime("%Y-%m-%d"))).fetchone()
    return row["used"] if row else 0


def last_remaining(conn, provider: str) -> int | None:
    row = conn.execute("SELECT remaining FROM api_usage WHERE provider=? AND remaining IS NOT NULL "
                       "ORDER BY day DESC LIMIT 1", (provider,)).fetchone()
    return row["remaining"] if row else None
