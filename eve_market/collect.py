"""Pull market history and live order-book snapshots for the watchlist."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from eve_market import auth, storage
from eve_market.esi import ESIClient, is_structure

log = logging.getLogger(__name__)

RAW_ORDER_BAND = 0.20  # keep individual orders priced within 20% of the best price
RAW_ORDER_DAYS = 90    # and only for this many days


def _top_pct_price(orders: list[dict], highest_first: bool, pct: float = 0.05) -> float | None:
    """Volume-weighted price of the best `pct` of the book's volume. Much steadier than the single
    best order, which can be one unit someone listed as bait. Orders more than 20% away from the
    best price are ignored first, so huge lowball buy orders don't swamp the volume."""
    if not orders:
        return None
    best = max(o["price"] for o in orders) if highest_first else min(o["price"] for o in orders)
    orders = [o for o in orders if (o["price"] >= best * 0.8 if highest_first else o["price"] <= best * 1.2)]
    total = sum(o["volume_remain"] for o in orders)
    if not total:
        return None
    want, got, isk = max(total * pct, 1), 0, 0.0
    for o in sorted(orders, key=lambda o: o["price"], reverse=highest_first):
        take = min(o["volume_remain"], want - got)
        got += take
        isk += take * o["price"]
        if got >= want:
            break
    return isk / got


def near_touch(orders: list[dict], pct: float) -> list[dict]:
    """Orders priced within `pct` of the best price on their own side of the book."""
    out = []
    for buy in (True, False):
        side = [o for o in orders if bool(o["is_buy_order"]) == buy]
        if not side:
            continue
        best = max(o["price"] for o in side) if buy else min(o["price"] for o in side)
        out += [o for o in side if (o["price"] >= best * (1 - pct) if buy else o["price"] <= best * (1 + pct))]
    return out


def summarize_orders(orders: list[dict], location_id: int | None) -> dict:
    if location_id is not None:
        orders = [o for o in orders if o["location_id"] == location_id]
    bids = [o for o in orders if o["is_buy_order"]]
    asks = [o for o in orders if not o["is_buy_order"]]
    touch = near_touch(orders, 0.05)  # depth: ISK resting within 5% of the best price
    return {
        "bid_depth": sum(o["price"] * o["volume_remain"] for o in touch if o["is_buy_order"]),
        "ask_depth": sum(o["price"] * o["volume_remain"] for o in touch if not o["is_buy_order"]),
        "bid_top5": _top_pct_price(bids, highest_first=True),
        "ask_top5": _top_pct_price(asks, highest_first=False),
        "best_bid": max((o["price"] for o in bids), default=None),
        "best_ask": min((o["price"] for o in asks), default=None),
        "bid_volume": sum(o["volume_remain"] for o in bids),
        "ask_volume": sum(o["volume_remain"] for o in asks),
        "bid_orders": len(bids),
        "ask_orders": len(asks),
    }


def collect(items: dict[str, int], markets: dict[str, dict], region_overrides: dict[str, int] | None = None,
            db_path=storage.DEFAULT_DB, snapshots: bool = True, client: ESIClient | None = None,
            workers: int = 6, history: bool = True) -> None:
    """Fetch history and order snapshots for every `{name: type_id}` item at every hub in `markets`.

    History is per region, so each (region, item) pair is fetched once. Items in `region_overrides`
    (PLEX on its global market) have one history for all hubs; their order book is still split
    by hub station, since global-market orders carry the station they were placed at.

    `history=False` skips the daily history (it only changes once a day) for quick hourly snapshots.
    """
    client = client or ESIClient()
    region_overrides = region_overrides or {}
    conn = storage.connect(db_path)
    storage.save_type_names(conn, items)
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # (region, type) -> stations whose snapshot comes from that region's order book
    jobs: dict[tuple[int, int], list[int]] = {}
    names = {t: n for n, t in items.items()}
    for m in markets.values():
        for name, type_id in items.items():
            region = region_overrides.get(name, m["region_id"])
            jobs.setdefault((region, type_id), []).append(m["station_id"])

    # Citadel hubs: with a login, read the structure's full market once (includes orders the
    # region book may not show). Without one, fall back to the public regional book.
    citadel_orders: dict[int, dict[int, list]] = {}
    citadels = [m["station_id"] for m in markets.values() if is_structure(m["station_id"])]
    if citadels and snapshots and auth.has_login():
        token = auth.access_token()
        for sid in citadels:
            try:
                by_type: dict[int, list] = {}
                for o in client.structure_orders(sid, token):
                    by_type.setdefault(o["type_id"], []).append(o)
                citadel_orders[sid] = by_type
            except Exception as exc:
                log.warning("citadel %s market not readable (docking access?): %s", sid, exc)
    elif citadels and snapshots:
        log.info("Not logged in: citadel snapshots use the public regional order book only.")

    def fetch(job):
        region, type_id = job
        hist = client.market_history(type_id, region) if history else []
        orders = client.market_orders(type_id, region) if snapshots else []
        return job, hist, orders

    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(fetch, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                (region, type_id), hist, orders = fut.result()
            except Exception as exc:  # keep going; one bad request shouldn't kill the run
                log.warning("fetch failed: %s", exc)
                continue
            n = storage.upsert_history(conn, region, type_id, hist) if hist else 0
            for station in jobs[(region, type_id)] if snapshots else []:
                src = citadel_orders[station].get(type_id, []) if station in citadel_orders else orders
                storage.insert_snapshot(conn, {"taken_at": taken_at, "region_id": region, "location_id": station,
                                               "type_id": type_id, **summarize_orders(src, station)})
                # Individual orders near the top of the book, for concentration and churn checks.
                here = [o for o in src if o.get("location_id") == station and "order_id" in o]
                storage.insert_raw_orders(conn, taken_at, station, type_id, near_touch(here, RAW_ORDER_BAND))
            log.debug("[%d/%d] %s in region %s: %d history rows", i, len(jobs), names[type_id], region, n)
    kind = "history+snapshots" if history and snapshots else "history" if history else "snapshots"
    if snapshots:
        storage.prune_raw_orders(conn, RAW_ORDER_DAYS)
    storage.log_run(conn, taken_at, kind)
    log.info("Collected %d item/region pairs for %d hubs at %s (%s)", len(jobs), len(markets), taken_at, kind)
    conn.close()

#Checks to see if history is old and needs updating. Really just looks at the database and checks the history table for last update
def history_is_stale(db_path=storage.DEFAULT_DB, hours: float = 12) -> bool:
    """True if history hasn't been refreshed in `hours` (used by --auto for hourly runs)."""
    conn = storage.connect(db_path)
    last = storage.last_run(conn, "history")
    conn.close()
    if last is None:
        return True
    then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() > hours * 3600
