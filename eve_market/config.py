"""Single place that loads the item lists and hub definitions from data_sets/."""
from __future__ import annotations

from data_sets.markets import DEFAULT_MARKET, MARKETS
from data_sets.tracked_items import EVERY_ITEM, ITEM_GROUPS, ITEM_LISTS, REGION_OVERRIDES
from eve_market import storage

__all__ = ["DEFAULT_MARKET", "MARKETS", "ITEM_GROUPS", "ITEM_LISTS", "EVERY_ITEM", "REGION_OVERRIDES", "EXTRA_REGIONS", "load_history"]

EXTRA_REGIONS = tuple(sorted(set(REGION_OVERRIDES.values())))


def load_history(conn, list_name: str | dict[str, int], market: str = DEFAULT_MARKET, since: str | None = None):
    """Daily history for one item list (an ITEM_LISTS name, or a {name: type_id} dict such as
    EVERY_ITEM) at one hub (plus the global PLEX market)."""
    items = ITEM_LISTS[list_name] if isinstance(list_name, str) else list_name
    return storage.read_history(conn, list(items.values()),
                                MARKETS[market]["region_id"], EXTRA_REGIONS, since)
