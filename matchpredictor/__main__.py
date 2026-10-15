"""CLI: python -m matchpredictor <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config, db

NETCHECK_URLS = {
    "football-data.co.uk": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
    "understat.com": "https://understat.com/league/EPL/2025",
    "api.clubelo.com (https)": "https://api.clubelo.com/Barcelona",
    "api.clubelo.com (http)": "http://api.clubelo.com/Barcelona",
    "api.the-odds-api.com": "https://api.the-odds-api.com/v4/sports",
    "v3.football.api-sports.io": "https://v3.football.api-sports.io/status",
    "raw.githubusercontent.com": "https://raw.githubusercontent.com/datasets/football-datasets/main/README.md",
}


def netcheck() -> dict:
    """HTTP status per data host (no API keys sent, so 401s from keyed APIs still prove reachability)."""
    import requests
    out = {}
    for name, url in NETCHECK_URLS.items():
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "match-predictor netcheck"})
            out[name] = f"reachable (HTTP {r.status_code})"
        except requests.RequestException as e:
            out[name] = f"UNREACHABLE ({type(e).__name__})"
    import os
    if os.environ.get("API_FOOTBALL_KEY"):      # plan + quota for the configured key (the key is never printed)
        try:
            b = requests.get("https://v3.football.api-sports.io/status", timeout=20,
                             headers={"x-apisports-key": os.environ["API_FOOTBALL_KEY"]}).json()
            r = b.get("response") if isinstance(b.get("response"), dict) else {}
            sub, req = r.get("subscription") or {}, r.get("requests") or {}
            out["API-Football plan"] = (f"{sub.get('plan')} active={sub.get('active')} until {sub.get('end')}, "
                                        f"requests today {req.get('current')}/{req.get('limit_day')}"
                                        if sub else f"no plan info: {b.get('errors')}")
        except (requests.RequestException, ValueError) as e:
            out["API-Football plan"] = f"status check failed ({type(e).__name__})"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="matchpredictor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch data, predict, track, build site")
    r.add_argument("--mode", choices=["auto", "daily", "prekickoff", "results"], default="daily")
    h = sub.add_parser("history", help="refresh football-data, Understat, ClubElo (and API-Football history)")
    h.add_argument("--backtest-data", action="store_true", help="also fetch API-Football history for the backtest")
    b = sub.add_parser("backtest", help="walk-forward accuracy backtest with ablation")
    b.add_argument("--season", default=None)
    b.add_argument("--refresh", action="store_true", help="refresh all history sources first")
    b.add_argument("--out", default=str(config.SITE_DIR / "data" / "backtest.json"))
    b.add_argument("--markdown", default=None, help="also write the markdown report here")
    d = sub.add_parser("detail", help="per-match players and shots (data/detail.db)")
    d.add_argument("--load", action="store_true", help="load the committed archives into detail.db first")
    d.add_argument("--download", action="store_true", help="fetch missing matches from Understat")
    d.add_argument("--apifootball", action="store_true", help="fetch API-Football history for the team list")
    d.add_argument("--apifootball-sample", action="store_true", help="~8-request real-data parsing check")
    d.add_argument("--export", action="store_true", help="write archives for finished seasons")
    d.add_argument("--seasons", type=int, default=None, help="past seasons to cover (default: history_seasons)")
    d.add_argument("--pause", type=float, default=1.0, help="seconds between Understat downloads")
    d.add_argument("--limit", type=int, default=None, help="at most this many matches per run")
    sub.add_parser("warehouse", help="rebuild data/warehouse.db (clean unified data) from match.db + detail.db")
    sub.add_parser("audit", help="completeness + accuracy report for warehouse.db / detail.db (markdown)")
    sub.add_parser("site", help="rebuild site JSON from the database")
    sub.add_parser("gate", help="print the mode to run now: daily | prekickoff | skip")
    sub.add_parser("netcheck", help="check which data hosts are reachable from here")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "gate":
        from .schedule import gate
        print(gate())
        return 0
    if args.cmd == "netcheck":
        for k, v in netcheck().items():
            print(f"{k:32s} {v}")
        return 0

    if args.cmd == "audit":
        from . import audit
        print(audit.run()[1])
        return 0
    if args.cmd == "warehouse":
        from . import warehouse
        print(json.dumps(warehouse.build(), indent=1))
        return 0

    with db.session() as conn:
        if args.cmd == "run":
            from .pipeline import run
            mode = args.mode
            if mode == "auto":
                from .schedule import gate
                mode = gate()
                if mode == "skip":
                    print("nothing to do")
                    return 0
            print(json.dumps(run(conn, mode), indent=1, default=str))
        elif args.cmd == "history":
            from datetime import date

            from . import history
            notes: list[str] = []
            out = history.refresh(conn, config.season_code(date.today()), notes, backtest=args.backtest_data)
            print(json.dumps(out, indent=1, default=str))
            print("\n".join(notes))
        elif args.cmd == "backtest":
            from datetime import date

            from . import backtest, history
            notes: list[str] = []
            if args.refresh:
                history.refresh(conn, config.season_code(date.today()), notes, backtest=True)
                conn.commit()
            from . import backtest_markets, model_data
            mdf = model_data.prepare(notes)          # the model reads the warehouse, rebuilt from match.db now
            rep = backtest.run(conn, season=args.season, notes=notes, df=mdf)
            rep["markets"] = backtest_markets.run(conn, season=args.season, df=mdf)
            with open(args.out, "w") as f:
                json.dump(rep, f, separators=(",", ":"), default=str)
            md = backtest.to_markdown(rep)
            if args.markdown:
                with open(args.markdown, "w") as f:
                    f.write(md)
            print(md)
        elif args.cmd == "detail":
            from datetime import date

            from . import detail
            current = config.season_code(date.today())
            seasons = config.previous_seasons(current, args.seasons or config.settings()["history_seasons"]) + [current]
            notes: list[str] = []
            dconn = detail.connect()
            out = {}
            af_current = 2000 + int(current[:2])            # API-Football season = start year
            if args.load:
                out["loaded"] = detail.load_archive(dconn)
                out["loaded_api_football"] = len(detail.load_af(dconn))
            if args.download:
                out["download"] = detail.sync_understat(conn, dconn, seasons, notes, pause=args.pause, limit=args.limit)
            if args.apifootball_sample:
                from .ingest import api_football_history as afh
                out["api_football_sample"] = afh.Backfill(conn, dconn, notes).sample(afh.load_team_list(), af_current)
            if args.apifootball:
                from .ingest import api_football_history as afh
                out["api_football"] = afh.Backfill(conn, dconn, notes).run(afh.load_team_list(), af_current)
            if args.export:
                # every season, the current one included (re-written each run: it is the data that matters most)
                out["exported"] = {f"{lg}_{s}": detail.export_archive(dconn, lg, s)
                                   for lg, s in dconn.execute("SELECT DISTINCT league, season FROM detail_matches")}
                out["exported_api_football"] = detail.export_af(dconn, finished_before=af_current + 1)
            dconn.close()
            print(json.dumps(out, indent=1, default=str))
            print("\n".join(notes))
        elif args.cmd == "site":
            from .site import build
            build(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
