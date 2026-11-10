"""Single place that loads the item lists and hub definitions from data_sets/."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_sets.markets import DEFAULT_MARKET, MARKETS  # noqa: E402,F401
from data_sets.tracked_items import ITEM_GROUPS, ITEM_LISTS, REGION_OVERRIDES  # noqa: E402,F401

from . import storage  # noqa: E402

EXTRA_REGIONS = tuple(sorted(set(REGION_OVERRIDES.values())))


def load_history(conn, list_name: str, market: str = DEFAULT_MARKET):
    """Daily history for one item list at one hub (plus the global PLEX market)."""
    return storage.read_history(conn, list(ITEM_LISTS[list_name].values()),
                                MARKETS[market]["region_id"], EXTRA_REGIONS)
