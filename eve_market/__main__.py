"""Command line entry point.
    This is the main workflow.
    python -m eve_market collect              # fetch history + order snapshots for the watchlist
    python -m eve_market backtest             # walk-forward evaluation of the models
    python -m eve_market forecast             # predicted % change over the horizon, per item
    python -m eve_market backfill             # older history (back to 2003) from the EVE Ref archive
    python -m eve_market login                # log in a character (citadel markets)
    python -m eve_market hubs --security low  # where the tracked items trade, incl. citadels
    python -m eve_market health               # market health score and warning flags per item
    python -m eve_market dashboard            # local website at http://127.0.0.1:5000
    python -m eve_market site                 # static copy of the dashboard in site/ (for GitHub Pages)
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from eve_market import storage
from eve_market.collect import collect, history_is_stale
from eve_market.config import DEFAULT_MARKET, ITEM_LISTS, MARKETS, REGION_OVERRIDES, load_history
from eve_market.forecast import DEFAULT_MIN_ISK, apply_health_floor, backtest, build_features, predict_latest

# this is our main function which stores all the return data in our database and then our websites use this data.
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eve_market")
    p.add_argument("--db", default=str(storage.DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--list", choices=ITEM_LISTS, default="all", help="which item list to collect (default: all)")
    c.add_argument("--no-snapshots", action="store_true", help="skip live order-book snapshots")
    c.add_argument("--snapshots-only", action="store_true", help="only order-book snapshots (skip daily history)")
    c.add_argument("--auto", action="store_true",
                   help="for scheduled runs: always snapshot, refresh history only if it's over 12h old")
    c.add_argument("--log-file", help="append log output to this file")
    c.add_argument("--market", nargs="+", choices=MARKETS, default=list(MARKETS), help="hubs to collect (default: all)")

    for name in ("backtest", "forecast"):
        s = sub.add_parser(name)
        s.add_argument("--horizon", type=int, default=7, help="days ahead to forecast")
        s.add_argument("--list", choices=ITEM_LISTS, default="core",
                       help="core = PLEX/injectors/ships, commodities = minerals/ore/components")
        s.add_argument("--market", choices=MARKETS, default=DEFAULT_MARKET, help="trade hub (default: Jita)")
        s.add_argument("--min-isk", type=float, default=DEFAULT_MIN_ISK,
                       help="health floor: leave out items trading less ISK per day than this (0 = keep all)")
        s.add_argument("--station", action="store_true",
                       help="model the hub station's own price (needs ~45 days of hourly snapshots)")
    sub.choices["forecast"].add_argument("--model", choices=["gbm", "ridge"], default="gbm")
    sub.choices["forecast"].add_argument("--out", help="write predictions to this CSV")

    b = sub.add_parser("backfill", help="load older daily history from the EVE Ref archive")
    b.add_argument("--list", choices=ITEM_LISTS, default="all")
    b.add_argument("--start", default="2003-10-01", help="first day (YYYY-MM-DD), default: earliest available")
    b.add_argument("--end", help="last day (default: today)")
    b.add_argument("--workers", type=int, default=4, help="parallel downloads")
    b.add_argument("--market", nargs="+", choices=MARKETS, default=list(MARKETS), help="hubs to backfill (default: all)")
    b.add_argument("--force", action="store_true", help="re-fetch days already backfilled")

    sub.add_parser("login", help="log in an EVE character (needed for citadel markets)")
    sub.add_parser("whoami", help="show the logged-in character")
    h = sub.add_parser("hubs", help="rank where the tracked items trade, including citadels")
    h.add_argument("--security", choices=["high", "low", "null"], help="only show this security band")
    h.add_argument("--top", type=int, default=80)

    hl = sub.add_parser("health", help="market health score and warning flags per item")
    hl.add_argument("--list", choices=ITEM_LISTS, default="all")
    hl.add_argument("--market", choices=MARKETS, default=DEFAULT_MARKET)
    hl.add_argument("--min-isk", type=float, default=DEFAULT_MIN_ISK, help="ISK/day below which an item is illiquid")

    d = sub.add_parser("dashboard", help="serve the local dashboard")
    d.add_argument("--port", type=int, default=5000)
    d.add_argument("--host", default="127.0.0.1")

    st = sub.add_parser("site", help="build a static copy of the dashboard (for GitHub Pages)")
    st.add_argument("--out", default="site", help="output folder (default: site)")
    st.add_argument("--refresh-hours", type=float, default=6, help="how often the site is rebuilt, shown in the footer")
    st.add_argument("--market", nargs="+", choices=MARKETS, help="only these hubs (default: all)")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pd.set_option("display.width", 160, "display.max_rows", 200)

    if args.cmd == "collect":
        if args.log_file:
            fh = logging.FileHandler(args.log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(fh)
        history = not args.snapshots_only and (not args.auto or history_is_stale(args.db))
        collect(ITEM_LISTS[args.list], {m: MARKETS[m] for m in args.market}, REGION_OVERRIDES, args.db,
                snapshots=not args.no_snapshots, history=history)
        return

    if args.cmd in ("login", "whoami"):
        from eve_market import auth
        try:
            tok = auth.login() if args.cmd == "login" else auth.load_token()
            print(f"Logged in as {tok['character_name']} ({tok['character_id']}), scopes: {', '.join(tok['scopes']) if isinstance(tok['scopes'], list) else tok['scopes']}")
        except auth.NotLoggedIn as exc:
            print(exc)
        return

    if args.cmd == "hubs":
        from eve_market.hubs import print_hubs, scan
        print_hubs(scan(top=args.top), args.security)
        return

    if args.cmd == "backfill":
        from eve_market.backfill import backfill
        backfill(ITEM_LISTS[args.list], REGION_OVERRIDES, [MARKETS[m]["region_id"] for m in args.market], args.start, args.end, args.db, args.workers, args.force)
        return

    if args.cmd == "health":
        from eve_market.health import health_table, print_health
        print_health(health_table(storage.connect(args.db), args.market, args.list, args.min_isk))
        return

    if args.cmd == "site":
        from eve_market.site import build_site
        build_site(args.out, args.db, args.refresh_hours, markets=args.market)
        return

    if args.cmd == "dashboard":
        from eve_market.dashboard import run
        print(f"Dashboard at http://{args.host}:{args.port}")
        run(args.host, args.port, args.db)
        return

    conn = storage.connect(args.db)
    feats, excluded = apply_health_floor(build_features(load_history(conn, args.list, args.market), args.horizon),
                                         args.min_isk)
    if excluded:
        print(f"Left out as illiquid (under {args.min_isk / 1e9:g}B ISK/day): {', '.join(excluded)}\n")
    if feats.empty:
        print("No item at this hub is above the health floor. Lower it with --min-isk.")
        return
    if args.station:
        from eve_market.forecast import MIN_STATION_DAYS, build_station_features, predict_station, station_days
        st = storage.read_station_prices(conn, MARKETS[args.market]["station_id"],
                                         list(ITEM_LISTS[args.list].values())).dropna(subset=["mid"])
        days = station_days(st)
        res = predict_station(build_station_features(feats, st, args.horizon), "ridge" if args.cmd == "backtest"
                              else args.model, args.horizon) if days >= MIN_STATION_DAYS else None
        if res is None:
            print(f"Station model not ready: {days} of {MIN_STATION_DAYS} days of snapshots at "
                  f"{MARKETS[args.market]['station']}. The hourly task keeps adding them.")
            return
        metrics, latest = res
        if args.cmd == "backtest":
            print(metrics.groupby("model")[["mae", "direction_acc"]].mean().sort_values("mae").to_string())
        else:
            latest["pred_pct_change"] = (np.exp(latest["pred"]) - 1) * 100
            latest["pred_station_price"] = latest["mid"] * np.exp(latest["pred"])
            print(latest[["type_id", "name", "date", "mid", "pred_station_price", "pred_pct_change"]]
                  .sort_values("pred_pct_change", ascending=False).to_string(index=False))
        return
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
