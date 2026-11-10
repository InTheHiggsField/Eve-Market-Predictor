"""Command line entry point.

    python -m eve_market collect              # fetch history + order snapshots for the watchlist
    python -m eve_market backtest             # walk-forward evaluation of the models
    python -m eve_market forecast             # predicted % change over the horizon, per item
    python -m eve_market backfill             # older history (back to 2003) from the EVE Ref archive
    python -m eve_market dashboard            # local website at http://127.0.0.1:5000
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd

from . import storage
from .collect import collect
from .config import DEFAULT_MARKET, ITEM_LISTS, MARKETS, REGION_OVERRIDES, load_history
from .forecast import backtest, build_features, predict_latest


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eve_market")
    p.add_argument("--db", default=str(storage.DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--list", choices=ITEM_LISTS, default="all", help="which item list to collect (default: all)")
    c.add_argument("--no-snapshots", action="store_true", help="skip live order-book snapshots")
    c.add_argument("--market", nargs="+", choices=MARKETS, default=list(MARKETS), help="hubs to collect (default: all)")

    for name in ("backtest", "forecast"):
        s = sub.add_parser(name)
        s.add_argument("--horizon", type=int, default=7, help="days ahead to forecast")
        s.add_argument("--list", choices=ITEM_LISTS, default="core",
                       help="core = PLEX/injectors/ships, commodities = minerals/ore/components")
        s.add_argument("--market", choices=MARKETS, default=DEFAULT_MARKET, help="trade hub (default: Jita)")
    sub.choices["forecast"].add_argument("--model", choices=["gbm", "ridge"], default="gbm")
    sub.choices["forecast"].add_argument("--out", help="write predictions to this CSV")

    b = sub.add_parser("backfill", help="load older daily history from the EVE Ref archive")
    b.add_argument("--list", choices=ITEM_LISTS, default="all")
    b.add_argument("--start", default="2003-10-01", help="first day (YYYY-MM-DD), default: earliest available")
    b.add_argument("--end", help="last day (default: today)")
    b.add_argument("--workers", type=int, default=4, help="parallel downloads")
    b.add_argument("--market", nargs="+", choices=MARKETS, default=list(MARKETS), help="hubs to backfill (default: all)")
    b.add_argument("--force", action="store_true", help="re-fetch days already backfilled")

    d = sub.add_parser("dashboard", help="serve the local dashboard")
    d.add_argument("--port", type=int, default=5000)
    d.add_argument("--host", default="127.0.0.1")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pd.set_option("display.width", 160, "display.max_rows", 200)

    if args.cmd == "collect":
        collect(ITEM_LISTS[args.list], {m: MARKETS[m] for m in args.market}, REGION_OVERRIDES, args.db, not args.no_snapshots)
        return

    if args.cmd == "backfill":
        from .backfill import backfill
        backfill(ITEM_LISTS[args.list], REGION_OVERRIDES, [MARKETS[m]["region_id"] for m in args.market], args.start, args.end, args.db, args.workers, args.force)
        return

    if args.cmd == "dashboard":
        from .dashboard import run
        print(f"Dashboard at http://{args.host}:{args.port}")
        run(args.host, args.port, args.db)
        return

    conn = storage.connect(args.db)
    feats = build_features(load_history(conn, args.list, args.market), args.horizon)
    if args.cmd == "backtest":
        res = backtest(feats, args.horizon)
        print(res.to_string(index=False))
        print("\nMean over folds:")
        print(res.groupby("model")[["mae", "direction_acc"]].mean().sort_values("mae").to_string())
    else:
        preds = predict_latest(feats, args.model)
        print(preds.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
        if args.out:
            preds.to_csv(args.out, index=False)


if __name__ == "__main__":
    main()
