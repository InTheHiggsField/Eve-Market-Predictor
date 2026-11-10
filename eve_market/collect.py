"""Pull market history and live order-book snapshots for the watchlist."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .esi import ESIClient
from . import storage

log = logging.getLogger(__name__)


def summarize_orders(orders: list[dict], location_id: int | None) -> dict:
    if location_id is not None:
        orders = [o for o in orders if o["location_id"] == location_id]
    bids = [o for o in orders if o["is_buy_order"]]
    asks = [o for o in orders if not o["is_buy_order"]]
    return {
        "best_bid": max((o["price"] for o in bids), default=None),
        "best_ask": min((o["price"] for o in asks), default=None),
        "bid_volume": sum(o["volume_remain"] for o in bids),
        "ask_volume": sum(o["volume_remain"] for o in asks),
        "bid_orders": len(bids),
        "ask_orders": len(asks),
    }


def collect(items: dict[str, int], markets: dict[str, dict], region_overrides: dict[str, int] | None = None,
            db_path=storage.DEFAULT_DB, snapshots: bool = True, client: ESIClient | None = None,
            workers: int = 6) -> None:
    """Fetch history and order snapshots for every `{name: type_id}` item at every hub in `markets`.

    History is per region, so each (region, item) pair is fetched once. Items in `region_overrides`
    (PLEX on its global market) have one history for all hubs; their order book is still split
    by hub station, since global-market orders carry the station they were placed at.
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

    def fetch(job):
        region, type_id = job
        hist = client.market_history(type_id, region)
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
            n = storage.upsert_history(conn, region, type_id, hist)
            for station in jobs[(region, type_id)] if snapshots else []:
                storage.insert_snapshot(conn, {"taken_at": taken_at, "region_id": region, "location_id": station,
                                               "type_id": type_id, **summarize_orders(orders, station)})
            log.info("[%d/%d] %s in region %s: %d history rows", i, len(jobs), names[type_id], region, n)
    conn.close()
