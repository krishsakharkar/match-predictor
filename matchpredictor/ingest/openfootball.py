"""openfootball (github.com/openfootball) Football.TXT results, e.g. champions-league/2025-26/cl.txt.

Used to build seed history (data/history/*.csv) for European seasons no API covers on our plan.
Times in the files are Central European local time; knockout scores use the 90-minute result
(extra time and penalties are dropped, as in football-data / API-Football full-time scores)."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

URL = "https://raw.githubusercontent.com/openfootball/champions-league/master/{season}/cl.txt"
CET = ZoneInfo("Europe/Paris")
_DATE = re.compile(r"^\s+[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{1,2})(?: (\d{4}))?\s*$")
_MATCH = re.compile(r"^\s+(?:(\d{1,2}:\d{2})\s+)?(.+?) \([A-Z]{3}\)\s+v\s+(.+?) \([A-Z]{3}\)\s+(\d+)-(\d+)\s*(.*)$")
_NINETY = re.compile(r"a\.e\.t\.\s*\((\d+)-(\d+)")


def parse(text: str, first_year: int) -> list[dict]:
    """[{kickoff_utc, date, home, away, home_goals, away_goals}] in file order. Dates without a year
    take the season's year: months Jul-Dec -> first_year, Jan-Jun -> first_year + 1."""
    out, day, time = [], None, "20:00"
    for line in text.splitlines():
        d = _DATE.match(line)
        if d:
            mon = datetime.strptime(d.group(1), "%b").month
            year = int(d.group(3)) if d.group(3) else first_year + (mon < 7)
            day, time = (year, mon, int(d.group(2))), "20:00"
            continue
        m = _MATCH.match(line)
        if not m or day is None:
            continue
        time = m.group(1) or time
        hg, ag = int(m.group(4)), int(m.group(5))
        ninety = _NINETY.search(m.group(6))
        if ninety:
            hg, ag = int(ninety.group(1)), int(ninety.group(2))
        hh, mm = map(int, time.split(":"))
        ko = datetime(*day, hh, mm, tzinfo=CET).astimezone(ZoneInfo("UTC"))
        out.append({"kickoff_utc": ko.isoformat(), "date": ko.strftime("%Y-%m-%d"),
                    "home": m.group(2).strip(), "away": m.group(3).strip(), "home_goals": hg, "away_goals": ag})
    return out
