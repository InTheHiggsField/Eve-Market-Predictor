"""Pull market history and live order-book snapshots for the watchlist."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .esi import ESIClient, JITA_4_4, THE_FORGE
from . import storage

log = logging.getLogger(__name__)
TYPES_CSV = Path(__file__).resolve().parent.parent / "data_sets" / "invTypes_clean.csv"


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


def collect(type_ids: list[int], region_id: int = THE_FORGE, location_id: int | None = JITA_4_4,
            db_path=storage.DEFAULT_DB, snapshots: bool = True, client: ESIClient | None = None) -> None:
    client = client or ESIClient()
    conn = storage.connect(db_path)
    if TYPES_CSV.exists():
        storage.load_type_names(conn, TYPES_CSV)
    taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, type_id in enumerate(type_ids, 1):
        try:
            n = storage.upsert_history(conn, region_id, type_id, client.market_history(type_id, region_id))
            msg = f"[{i}/{len(type_ids)}] type {type_id}: {n} history rows"
            if snapshots:
                summary = summarize_orders(client.market_orders(type_id, region_id), location_id)
                storage.insert_snapshot(conn, {"taken_at": taken_at, "region_id": region_id,
                                               "location_id": location_id, "type_id": type_id, **summary})
                msg += f", bid {summary['best_bid']} / ask {summary['best_ask']}"
            log.info(msg)
        except Exception as exc:  # keep going; one bad type shouldn't kill the run
            log.warning("type %s failed: %s", type_id, exc)
    conn.close()
