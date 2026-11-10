"""Command line entry point.

    python -m eve_market collect              # fetch history + order snapshots for the watchlist
    python -m eve_market backtest             # walk-forward evaluation of the models
    python -m eve_market forecast             # predicted % change over the horizon, per item
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from . import storage
from .collect import collect
from .esi import JITA_4_4, THE_FORGE
from .forecast import backtest, build_features, predict_latest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_sets.ImportantItems import ITEM_IDS  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="eve_market")
    p.add_argument("--db", default=str(storage.DEFAULT_DB))
    p.add_argument("--region", type=int, default=THE_FORGE)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--types", type=int, nargs="*", help="type IDs (default: data_sets/ImportantItems.py)")
    c.add_argument("--no-snapshots", action="store_true", help="skip live order-book snapshots")
    c.add_argument("--location", type=int, default=JITA_4_4, help="station for order snapshots (default Jita 4-4)")

    for name in ("backtest", "forecast"):
        s = sub.add_parser(name)
        s.add_argument("--horizon", type=int, default=7, help="days ahead to forecast")
    sub.choices["forecast"].add_argument("--model", choices=["gbm", "ridge"], default="gbm")
    sub.choices["forecast"].add_argument("--out", help="write predictions to this CSV")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pd.set_option("display.width", 160, "display.max_rows", 200)

    if args.cmd == "collect":
        collect(args.types or ITEM_IDS, args.region, args.location, args.db, not args.no_snapshots)
        return

    conn = storage.connect(args.db)
    feats = build_features(storage.read_history(conn, args.region), args.horizon)
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
