"""One-off: find every faction ship hull (metaGroupID 4) whose Jita price is above a cutoff.

Walks ESI's Ships category (6) -> groups -> types, keeps published types with a market group and dogma
attribute 1692 (metaGroupID) == 4, then reads Jita 4-4 best sell/buy and the last 30 days of The Forge
history. Type info is cached under data/cache/types so reruns are fast.

    .venv\\Scripts\\python scripts\\find_faction_ships.py              # writes data/faction_ships_20m.csv
    .venv\\Scripts\\python scripts\\find_faction_ships.py --cutoff 15e6

Prints a FACTION_SHIPS dict ready to paste into data_sets/tracked_items.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eve_market.esi import JITA_4_4, THE_FORGE, ESIClient  # noqa: E402

SHIPS_CATEGORY = 6
META_GROUP_ATTR = 1692
FACTION_META = 4
CACHE = ROOT / "data" / "cache" / "types"
CAPITAL_GROUPS = ("Carrier", "Dreadnought", "Force Auxiliary", "Supercarrier", "Titan", "Freighter",
                  "Jump Freighter", "Capital Industrial Ship", "Lancer Dreadnought")

# Limited-edition novelty hulls: meta group 4 in the SDE but not faction combat ships (YC128 campaign
# buses, luxury yachts, the 5B+ special-edition corvettes, the Guristas Shuttle).
NOVELTY_GROUPS = ("Special Edition Yachts", "Corvette", "Shuttle")
# Hull class order for the FACTION_SHIPS listing.
CLASS_ORDER = ("Frigate", "Cruiser", "Combat Battlecruiser", "Battleship", "Expedition Command Ship")
NAVY_MARKERS = ("Navy Issue", "Fleet Issue", "State Issue", "Imperial Issue", "Federate Issue", "Tribal Issue")

esi = ESIClient()


def cached(name: str, path: str) -> dict:
    f = CACHE / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    data = esi._get(path).json()
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def faction_hulls() -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    groups = cached(f"category_{SHIPS_CATEGORY}", f"/universe/categories/{SHIPS_CATEGORY}/")["groups"]
    with ThreadPoolExecutor(12) as pool:
        group_info = list(pool.map(lambda g: cached(f"group_{g}", f"/universe/groups/{g}/"), groups))
        type_ids = [(t, g["name"]) for g in group_info if g.get("published") for t in g["types"]]
        types = list(pool.map(lambda t: (cached(f"type_{t[0]}", f"/universe/types/{t[0]}/"), t[1]), type_ids))
    out = []
    for t, group in types:
        meta = {a["attribute_id"]: a["value"] for a in t.get("dogma_attributes", [])}.get(META_GROUP_ATTR)
        if t.get("published") and t.get("market_group_id") and meta == FACTION_META:
            out.append({"name": t["name"], "type_id": t["type_id"], "group": group})
    return out


def jita_stats(ship: dict) -> dict:
    tid = ship["type_id"]
    orders = [o for o in esi.market_orders(tid, THE_FORGE) if o["location_id"] == JITA_4_4]
    sells = [o["price"] for o in orders if not o["is_buy_order"]]
    buys = [o["price"] for o in orders if o["is_buy_order"]]
    hist = pd.DataFrame(esi.market_history(tid, THE_FORGE))
    units = isk = 0.0
    if not hist.empty:
        hist["date"] = pd.to_datetime(hist["date"])
        last30 = hist[hist["date"] > pd.Timestamp.now("UTC").tz_localize(None).normalize() - pd.Timedelta(days=30)]
        # Days with no trades are missing from ESI history; average over all 30 calendar days.
        units = float(last30["volume"].sum()) / 30
        isk = float((last30["volume"] * last30["average"]).sum()) / 30
    return {**ship, "jita_sell": min(sells) if sells else None, "jita_buy": max(buys) if buys else None,
            "avg_daily_units_30d": round(units, 2), "avg_daily_isk_30d": round(isk),
            "above_1b_isk_day": isk >= 1e9, "capital": ship["group"] in CAPITAL_GROUPS}


def ref_price(r: dict) -> float | None:
    """Best sell, or the bid if nobody is selling but the hull still traded in the last 30 days.

    A hull with no sell orders and no trades (tournament prizes, AT ships, capitals that can't sit in
    Jita) only has speculative lowball bids; it has no market price, so it gets None and is skipped."""
    if r["jita_sell"] is not None:
        return r["jita_sell"]
    return r["jita_buy"] if r["avg_daily_units_30d"] > 0 else None


def is_navy(name: str) -> bool:
    return any(m in name for m in NAVY_MARKERS)


def label(r: dict) -> str:
    """Comment heading for FACTION_SHIPS, e.g. "Navy cruisers", "Pirate battleships"."""
    cls = {"Combat Battlecruiser": "battlecruisers", "Expedition Command Ship": "expedition command ships"}.get(
        r["group"], r["group"].lower() + "s")
    return f"{'Navy' if is_navy(r['name']) else 'Pirate'} {cls}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", type=float, default=20e6)
    ap.add_argument("--out", default=str(ROOT / "data" / "faction_ships_20m.csv"))
    args = ap.parse_args()

    hulls = faction_hulls()
    print(f"{len(hulls)} published faction hulls with a market group", file=sys.stderr)
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(jita_stats, hulls))
    (ROOT / "data" / "cache" / "faction_ship_stats.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")

    no_market = [r for r in rows if ref_price(r) is None]
    novelty = [r for r in rows if r["group"] in NOVELTY_GROUPS and (ref_price(r) or 0) > args.cutoff]
    kept = sorted((r for r in rows if (ref_price(r) or 0) > args.cutoff and r["group"] not in NOVELTY_GROUPS),
                  key=lambda r: (CLASS_ORDER.index(r["group"]) if r["group"] in CLASS_ORDER else 99,
                                 not is_navy(r["name"]), r["name"]))
    near = [r for r in rows if 0.75 * args.cutoff <= (ref_price(r) or 0) <= args.cutoff]
    below = sorted((r for r in rows if 0 < (ref_price(r) or 0) <= args.cutoff), key=ref_price, reverse=True)

    cols = ["name", "type_id", "group", "jita_sell", "jita_buy", "avg_daily_units_30d", "avg_daily_isk_30d",
            "above_1b_isk_day"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    print(f"kept {len(kept)} over {args.cutoff:,.0f}; {sum(not r['above_1b_isk_day'] for r in kept)} under 1B ISK/day; "
          f"{len(near)} just below (75-100% of cutoff); {len(no_market)} with no Jita sells and no trades in 30 days (skipped)", file=sys.stderr)
    for r in kept:
        flag = " [capital]" if r["capital"] else ""
        print(f"  {r['group']:<22} {r['name']:<34} price {ref_price(r) / 1e6:>9.1f}M  "
              f"{r['avg_daily_isk_30d'] / 1e6:>9.1f}M ISK/day{flag}", file=sys.stderr)
    print("skipped novelty hulls:", ", ".join(r["name"] for r in novelty), file=sys.stderr)
    print("FACTION_SHIPS = {")
    heading = None
    for r in kept:
        if label(r) != heading:
            heading = label(r)
            print(f"    # {heading}")
        print(f'    "{r["name"]}": {r["type_id"]},')
    print("}")
    print("below cutoff:", ", ".join(f"{r['name']} ({ref_price(r) / 1e6:.1f}M)" for r in below), file=sys.stderr)
    print("near cutoff:", ", ".join(f"{r['name']} ({ref_price(r) / 1e6:.1f}M)" for r in near), file=sys.stderr)
    print("skipped, no market:", ", ".join(r["name"] for r in no_market), file=sys.stderr)


if __name__ == "__main__":
    main()
