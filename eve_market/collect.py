"""Pull market history and live order-book snapshots for the watchlist."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .esi import ESIClient, JITA_4_4, THE_FORGE
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


def collect(items: dict[str, int], region_id: int = THE_FORGE, location_id: int | None = JITA_4_4,
            region_overrides: dict[str, int] | None = None, db_path=storage.DEFAULT_DB,
            snapshots: bool = True, client: ESIClient | None = None) -> None:
    """Fetch history (and optionally an order snapshot) for each `{name: type_id}` item.

    Items listed in `region_overrides` (e.g. PLEX on its global market) are fetched from that
    region, and their order snapshot covers the whole region instead of one station.
    """
    client = client or ESIClient()
    region_overrides = region_overrides or {}
    conn = storage.connect(db_path)
    storage.save_type_names(conn, items)
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, (name, type_id) in enumerate(items.items(), 1):
        region = region_overrides.get(name, region_id)
        location = None if name in region_overrides else location_id
        try:
            n = storage.upsert_history(conn, region, type_id, client.market_history(type_id, region))
            msg = f"[{i}/{len(items)}] {name} ({type_id}): {n} history rows"
            if snapshots:
                summary = summarize_orders(client.market_orders(type_id, region), location)
                storage.insert_snapshot(conn, {"taken_at": taken_at, "region_id": region,
                                               "location_id": location, "type_id": type_id, **summary})
                msg += f", bid {summary['best_bid']} / ask {summary['best_ask']}"
            log.info(msg)
        except Exception as exc:  # keep going; one bad type shouldn't kill the run
            log.warning("%s (%s) failed: %s", name, type_id, exc)
    conn.close()
