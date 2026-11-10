"""Single place that loads the item lists and hub definitions from data_sets/."""
from __future__ import annotations

from data_sets.markets import DEFAULT_MARKET, MARKETS
from data_sets.tracked_items import ITEM_GROUPS, ITEM_LISTS, REGION_OVERRIDES
from eve_market import storage

__all__ = ["DEFAULT_MARKET", "MARKETS", "ITEM_GROUPS", "ITEM_LISTS", "REGION_OVERRIDES", "EXTRA_REGIONS", "load_history"]

EXTRA_REGIONS = tuple(sorted(set(REGION_OVERRIDES.values())))


def load_history(conn, list_name: str, market: str = DEFAULT_MARKET, since: str | None = None):
    """Daily history for one item list at one hub (plus the global PLEX market)."""
    return storage.read_history(conn, list(ITEM_LISTS[list_name].values()),
                                MARKETS[market]["region_id"], EXTRA_REGIONS, since)
