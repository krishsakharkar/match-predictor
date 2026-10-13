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
            rep = backtest.run(conn, season=args.season, notes=notes)
            from . import backtest_markets
            rep["markets"] = backtest_markets.run(conn, season=args.season)
            with open(args.out, "w") as f:
                json.dump(rep, f, separators=(",", ":"), default=str)
            md = backtest.to_markdown(rep)
            if args.markdown:
                with open(args.markdown, "w") as f:
                    f.write(md)
            print(md)
        elif args.cmd == "site":
            from .site import build
            build(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
