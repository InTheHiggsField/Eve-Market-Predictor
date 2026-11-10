"""Items the predictor tracks: in-game name -> type_id.

Chosen for high value and high daily ISK turnover (approved list, Nov 2026).
Only these items are collected and stored.
"""

TRACKED_ITEMS = {
    # PLEX & skill trading
    "PLEX": 44992,
    "Large Skill Injector": 40520,
    "Skill Extractor": 40519,
    "Small Skill Injector": 45635,
    "Daily Alpha Injector": 46375,
    # High-volume commodities
    "Logic Circuit": 25619,
    # Marauders
    "Paladin": 28659,
    "Vargur": 28665,
    "Golem": 28710,
    "Kronos": 28661,
    # Other high-turnover ships
    "Gila": 17715,
    "Loki": 29990,
}

# PLEX trades on its own global market rather than in Jita (The Forge).
GLOBAL_PLEX_MARKET = 19000001
REGION_OVERRIDES = {"PLEX": GLOBAL_PLEX_MARKET}
