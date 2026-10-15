"""Data audit: is the data complete, accurate and consistent? `python -m matchpredictor audit`

Reads data/warehouse.db (clean copy) and data/detail.db (raw sources). Every check prints a pass rate and
the first failing examples, so a problem is visible instead of hidden. Two kinds of checks:

completeness  - every finished API-Football fixture of a tracked club is in the warehouse; detail
                (team stats, lineups, player rows, events) is present wherever API-Football's coverage
                flags say it exists; big-five matches have Understat xG and player rows
accuracy      - sources agree with each other and with themselves: score = goal events = sum of player
                goals (+ own goals), 11 starters per side, sensible minutes, Understat xG = sum of its shots
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

BIG5 = ("EPL", "LALIGA", "BUNDESLIGA", "SERIEA", "LIGUE1")


class Audit:
    def __init__(self, warehouse_path=None, detail_path=None):
        from .detail import DETAIL_DB
        from .warehouse import WAREHOUSE_DB
        self.w = sqlite3.connect(str(warehouse_path or WAREHOUSE_DB))
        self.w.execute("ATTACH DATABASE ? AS d", (str(detail_path or DETAIL_DB),))
        self.results: list[dict] = []

    def check(self, area: str, name: str, ok: int, total: int, examples=None, expect_all=True, note: str = ""):
        self.results.append({"area": area, "check": name, "ok": ok, "total": total,
                             "rate": (ok / total) if total else None, "examples": list(examples or [])[:5],
                             "pass": (ok == total) if expect_all else None, "note": note})

    def q(self, sql: str, params=()):
        return self.w.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- completeness
    def completeness(self) -> None:
        tracked = [r[0] for r in self.q("SELECT af_team_id FROM teams WHERE tracked=1 AND af_team_id IS NOT NULL")]
        ph = ",".join("?" * len(tracked)) or "NULL"
        fx = self.q(f"""SELECT f.fixture_id, f.date, f.home, f.away, c.name, f.detail_at IS NOT NULL,
                c.events, c.lineups, c.stats_fixtures, c.stats_players
            FROM d.af_fixtures f JOIN d.af_competitions c ON c.league_id=f.league_id AND c.season=f.season
            WHERE f.status IN ('FT','AET','PEN') AND (f.home_id IN ({ph}) OR f.away_id IN ({ph}))""", (*tracked, *tracked))
        in_wh = {r[0] for r in self.q("SELECT af_fixture_id FROM matches WHERE af_fixture_id IS NOT NULL")}
        miss = [f"{r[1]} {r[2]} v {r[3]} ({r[4]})" for r in fx if r[0] not in in_wh]
        self.check("completeness", "tracked clubs: finished API fixtures present in the warehouse",
                   len(fx) - len(miss), len(fx), miss)
        covered = [r for r in fx if r[6] or r[7] or r[8] or r[9]]
        nod = [f"{r[1]} {r[2]} v {r[3]} ({r[4]})" for r in covered if not r[5]]
        self.check("completeness", "tracked clubs: match detail downloaded where API-Football covers it",
                   len(covered) - len(nod), len(covered), nod)
        for label, flag_idx, table in (("lineups", 7, "af_lineups"), ("player stats", 9, "af_player_match"),
                                       ("team stats", 8, "af_team_stats"), ("events", 6, "af_events")):
            have = {r[0] for r in self.q(f"SELECT DISTINCT fixture_id FROM d.{table}")}
            want = [r for r in covered if r[flag_idx] and r[5]]
            gap = [f"{r[1]} {r[2]} v {r[3]} ({r[4]})" for r in want if r[0] not in have]
            self.check("completeness", f"tracked clubs: {label} present where covered", len(want) - len(gap),
                       len(want), gap, expect_all=False,
                       note="API-Football's coverage flag is per league-season; a few matches can still lack it")
        # big five: Understat xG and player rows
        ph5 = ",".join("?" * len(BIG5))
        big = self.q(f"SELECT match_id, home_xg IS NOT NULL, xg_source FROM matches WHERE status='FT' AND comp IN ({ph5}) "
                     "AND season >= '2122'", BIG5)      # league games only: play-offs are their own competition
        nox = [r[0] for r in big if not r[1]]
        self.check("completeness", "big five: match xG present", len(big) - len(nox), len(big), nox)
        us = {r[0] for r in self.q("SELECT DISTINCT match_id FROM d.player_matches")}
        # gaps at the source: Understat lists the match without any data, or doesn't list it at all
        gap = {r[0] for r in self.q("SELECT match_id FROM d.detail_matches WHERE source='understat-empty'")} | \
            {r[0] for r in self.q(f"SELECT match_id FROM matches WHERE status='FT' AND comp IN ({ph5}) AND season >= '2122' "
                                  "AND understat_id IS NULL", BIG5)}
        und = [r[0] for r in big if r[0] not in us and r[0] not in gap]
        gaps = sorted(m for m in gap if m not in us)
        self.check("completeness", "big five: Understat player rows downloaded (where Understat has them)",
                   len(big) - len(gaps) - len(und), len(big) - len(gaps), und[-5:],
                   note="download still running" if und else "")
        self.check("completeness", "big five: matches Understat has no player data for (gap at the source; xG from "
                   "API-Football)", len(gaps), 0, gaps, expect_all=False)
        # competitions shown in full: every finished match has a result
        for comp in ("EPL", "UCL"):
            rows = self.q("SELECT match_id, home_goals FROM matches WHERE comp=? AND status='FT' AND season >= '2122'", (comp,))
            bad = [r[0] for r in rows if r[1] is None]
            self.check("completeness", f"{comp}: finished matches have a score", len(rows) - len(bad), len(rows), bad)
        cur = config.season_code(__import__("datetime").date.today())
        ucl = self.q("SELECT COUNT(*) FROM matches WHERE comp='UCL' AND season=? AND status='FT'", (cur,))[0][0]
        afu = self.q("SELECT COUNT(*) FROM d.af_fixtures WHERE league_id=2 AND season=? AND status IN ('FT','AET','PEN') "
                     "AND round LIKE 'League Stage%'", (2000 + int(cur[:2]),))[0][0]
        self.check("completeness", f"UCL {cur}: league-phase results stored (vs API-Football)", min(ucl, afu), afu)

    # ---------------------------------------------------------------- accuracy / consistency
    def accuracy(self) -> None:
        # 1. API-Football: final score (incl. extra time) = goal events (no shoot-out) per side
        rows = self.q("""SELECT f.fixture_id, f.date, f.home, f.away, f.home_id, f.home_goals, f.away_goals,
                SUM(CASE WHEN e.team_id=f.home_id THEN 1 ELSE 0 END), SUM(CASE WHEN e.team_id=f.away_id THEN 1 ELSE 0 END)
            FROM d.af_fixtures f JOIN d.af_events e ON e.fixture_id=f.fixture_id
            WHERE f.status IN ('FT','AET','PEN') AND e.type='Goal' AND e.detail <> 'Missed Penalty'
              AND COALESCE(e.comments,'') NOT LIKE '%Shootout%' GROUP BY f.fixture_id""")
        bad = [f"{r[1]} {r[2]} {r[5]}-{r[6]} {r[3]} (events {r[7]}-{r[8]})" for r in rows if (r[5], r[6]) != (r[7], r[8])]
        self.check("accuracy", "API-Football: score = goal events", len(rows) - len(bad), len(rows), bad, expect_all=False,
                   note="small gaps are the provider's event feed; scores themselves are cross-checked below")
        # 2. API-Football: team goals = its players' goals + opponent own goals (from events)
        rows = self.q("""SELECT f.fixture_id, f.date, f.home, f.away, f.home_goals, f.away_goals,
                (SELECT COALESCE(SUM(goals),0) FROM d.af_player_match p WHERE p.fixture_id=f.fixture_id AND p.team_id=f.home_id),
                (SELECT COALESCE(SUM(goals),0) FROM d.af_player_match p WHERE p.fixture_id=f.fixture_id AND p.team_id=f.away_id),
                (SELECT COUNT(*) FROM d.af_events e WHERE e.fixture_id=f.fixture_id AND e.type='Goal' AND e.detail='Own Goal' AND e.team_id=f.home_id),
                (SELECT COUNT(*) FROM d.af_events e WHERE e.fixture_id=f.fixture_id AND e.type='Goal' AND e.detail='Own Goal' AND e.team_id=f.away_id)
            FROM d.af_fixtures f WHERE f.status='FT' AND EXISTS (SELECT 1 FROM d.af_player_match p WHERE p.fixture_id=f.fixture_id)""")
        # API-Football attributes an own goal event to the team that benefits
        bad = [f"{r[1]} {r[2]} {r[4]}-{r[5]} {r[3]} (players {r[6]}+og {r[8]} / {r[7]}+og {r[9]})" for r in rows
               if (r[4], r[5]) not in ((r[6] + r[8], r[7] + r[9]), (r[6], r[7]))]
        self.check("accuracy", "API-Football: score = players' goals (+ own goals)", len(rows) - len(bad), len(rows), bad,
                   expect_all=False)
        # 3. lineups: 11 starters per side
        rows = self.q("SELECT fixture_id, team_id, SUM(started) FROM d.af_lineups GROUP BY fixture_id, team_id")
        bad = [f"fixture {r[0]} team {r[1]}: {r[2]} starters" for r in rows if r[2] != 11]
        self.check("accuracy", "lineups: 11 starters per side", len(rows) - len(bad), len(rows), bad, expect_all=False)
        # 4. minutes are plausible
        raw = self.q("SELECT COUNT(*) FROM d.af_player_match WHERE minutes IS NOT NULL AND minutes NOT BETWEEN 0 AND 130")[0][0]
        rows = self.q("SELECT COUNT(*), SUM(minutes BETWEEN 0 AND 130) FROM player_match_stats WHERE minutes IS NOT NULL")[0]
        self.check("accuracy", "player minutes between 0 and 130 (warehouse)", rows[1] or 0, rows[0] or 0,
                   note=f"{raw} impossible raw value(s) from the provider blanked" if raw else "")
        # 5. Understat: team goals = players' goals + opponent own goals, and = our result
        rows = self.q("""SELECT m.match_id, m.home_goals, m.away_goals,
                SUM(CASE WHEN p.side='h' THEN p.goals ELSE 0 END) + SUM(CASE WHEN p.side='a' THEN p.own_goals ELSE 0 END),
                SUM(CASE WHEN p.side='a' THEN p.goals ELSE 0 END) + SUM(CASE WHEN p.side='h' THEN p.own_goals ELSE 0 END)
            FROM matches m JOIN d.player_matches p ON p.match_id=m.match_id GROUP BY m.match_id""")
        bad = [f"{r[0]} result {r[1]}-{r[2]}, Understat players {r[3]}-{r[4]}" for r in rows if (r[1], r[2]) != (r[3], r[4])]
        self.check("accuracy", "Understat: our score = players' goals + own goals", len(rows) - len(bad), len(rows), bad)
        # 6. xG: one definition everywhere (Understat shot sum); report how far the old page totals were off
        rows = self.q("SELECT match_id, xg_source, home_xg, away_xg, home_xg_page, away_xg_page FROM matches "
                      "WHERE status='FT' AND comp IN ('EPL','LALIGA','BUNDESLIGA','SERIEA','LIGUE1') AND home_xg IS NOT NULL")
        shots = [r for r in rows if r[1] == "understat-shots"]
        self.check("accuracy", "big five: match xG from Understat shots (one definition for every season)",
                   len(shots), len(rows), [r[0] for r in rows if r[1] != "understat-shots"][-5:], expect_all=False,
                   note="the rest wait for the Understat download")
        off = [f"{r[0]} page {r[4]:.2f}-{r[5]:.2f} vs shots {r[2]:.2f}-{r[3]:.2f}" for r in shots
               if r[4] is not None and (abs(r[2] - r[4]) > 0.1 or abs(r[3] - r[5]) > 0.1)]
        self.check("accuracy", "Understat page xG within 0.1 of the shot sum (old-model gap, fixed by using shots)",
                   len(shots) - len(off), len(shots), off, expect_all=False)
        # 7. cross-source: scores agree (from the warehouse build log), duplicates, spelling splits
        for step, rows_, note in self.q("SELECT step, rows, note FROM build_log WHERE step LIKE 'check_%' ORDER BY step"):
            self.check("accuracy", note.split(":")[0], 0, 0, [note.split(":", 1)[1].strip()] if ":" in note else [],
                       expect_all=False, note="from the warehouse build")
        # 8. player identity: Understat players linked to an API-Football id (where both sources have the match)
        rows = self.q("""SELECT COUNT(*), SUM(player_id LIKE 'af:%') FROM player_match_stats WHERE sources LIKE '%understat%'
                         AND match_id IN (SELECT match_id FROM matches WHERE af_fixture_id IS NOT NULL)""")[0]
        self.check("accuracy", "players: Understat rows linked to one API-Football id", rows[1] or 0, rows[0] or 0,
                   expect_all=False, note="unlinked rows are mostly brief substitute appearances the API lists differently")

    def run(self) -> list[dict]:
        self.completeness()
        self.accuracy()
        return self.results

    @staticmethod
    def markdown(results: list[dict]) -> str:
        out = ["| | Check | OK / total | Rate |", "|---|---|---|---|"]
        for r in results:
            mark = "✅" if r["pass"] else ("❌" if r["pass"] is False else "ℹ️")
            rate = f"{r['rate']:.2%}" if r["rate"] is not None else ""
            tot = f"{r['ok']:,} / {r['total']:,}" if r["total"] else (r["examples"][0] if r["examples"] else "")
            out.append(f"| {mark} | {r['check']} | {tot} | {rate} |")
        lines = ["\n".join(out), ""]
        for r in results:
            if r["examples"] and r["total"] and r["ok"] < r["total"]:
                lines.append(f"- **{r['check']}** - e.g. " + "; ".join(r["examples"][:3]) + (f" ({r['note']})" if r["note"] else ""))
        return "\n".join(lines)


def run(warehouse_path=None, detail_path=None) -> tuple[list[dict], str]:
    try:
        res = Audit(warehouse_path, detail_path).run()
    except sqlite3.OperationalError as e:
        msg = f"warehouse.db does not match this code ({e}): rebuild it with `python -m matchpredictor warehouse`"
        return [{"area": "setup", "check": msg, "ok": 0, "total": 0, "rate": None, "examples": [], "pass": False,
                 "note": ""}], msg
    return res, Audit.markdown(res)
