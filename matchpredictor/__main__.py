"""CLI: python -m matchpredictor <command>"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config, db


def main(argv=None):
    ap = argparse.ArgumentParser(prog="matchpredictor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch data, predict, settle, build site")
    r.add_argument("--mode", choices=["auto", "daily", "prekickoff"], default="daily")
    sub.add_parser("history", help="download/refresh historical CSVs only")
    b = sub.add_parser("backtest", help="walk-forward backtest on last completed season")
    b.add_argument("--season", default=None)
    b.add_argument("--no-tune", action="store_true")
    b.add_argument("--out", default=str(config.SITE_DIR / "data" / "backtest.json"))
    sub.add_parser("site", help="rebuild site JSON from the database")
    sub.add_parser("gate", help="print the mode to run now: daily | prekickoff | skip")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "gate":
        from .schedule import gate
        print(gate())
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
            print(json.dumps(run(conn, mode), indent=1))
        elif args.cmd == "history":
            from .ingest import football_data
            notes: list[str] = []
            from datetime import date
            print(football_data.update_history(conn, config.season_code(date.today()), notes))
            print("\n".join(notes))
        elif args.cmd == "backtest":
            from . import backtest
            rep = backtest.run(conn, season=args.season, tune=not args.no_tune)
            with open(args.out, "w") as f:
                json.dump(rep, f, indent=1, default=str)
            print(backtest.to_markdown(rep))
        elif args.cmd == "site":
            from .site import build
            build(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
