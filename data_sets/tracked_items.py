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

# Commodities: minerals, compressed ore, and ship components (approved list, Nov 2026).
COMMODITY_ITEMS = {
    # Standard minerals
    "Tritanium": 34,
    "Pyerite": 35,
    "Mexallon": 36,
    "Isogen": 37,
    "Nocxium": 38,
    "Zydrine": 39,
    "Megacyte": 40,
    "Morphite": 11399,
    # Specialty minerals (thinly traded; forecasts will be noisy)
    "Crystalline Polycrase": 92570,
    "Crystalline Moissanite": 92571,
    "Crystalline Raspite": 92569,
    "Crystalline Kangite": 92572,
    "Neo-Jadarite": 76374,
    "Eleutrium": 88087,
    "Chromodynamic Tricarboxyls": 48927,
    # Compressed ore
    "Compressed Gneiss IV-Grade": 62555,
    "Compressed Zeolites": 62463,
    "Compressed Veldspar": 62516,
    "Compressed Scordite": 62520,
    "Compressed Bitumens": 62454,
    # T2 ship components
    "Zero-Point Field Manipulator": 52312,
    "Auto-Integrity Preservation Seal": 57478,
    "Life Support Backup Unit": 57486,
    "Photon Microprocessor": 11541,
    "Quantum Microprocessor": 11540,
    "Core Temperature Regulator": 57479,
    # Capital components
    "Neurolink Protection Cell": 57488,
    "Capital Core Temperature Regulator": 57487,
    "Capital Construction Parts": 21037,
}

ITEM_LISTS = {
    "core": TRACKED_ITEMS,
    "commodities": COMMODITY_ITEMS,
    "all": {**TRACKED_ITEMS, **COMMODITY_ITEMS},
}

# PLEX trades on its own global market rather than in Jita (The Forge).
GLOBAL_PLEX_MARKET = 19000001
REGION_OVERRIDES = {"PLEX": GLOBAL_PLEX_MARKET}


def _group(names: str, label: str) -> dict[str, str]:
    return {n.strip(): label for n in names.split(",")}


# Display groups for the dashboard.
ITEM_GROUPS = {
    **_group("PLEX, Large Skill Injector, Skill Extractor, Small Skill Injector, Daily Alpha Injector", "PLEX & skills"),
    **_group("Logic Circuit", "Salvage"),
    **_group("Paladin, Vargur, Golem, Kronos, Gila, Loki", "Ships"),
    **_group("Tritanium, Pyerite, Mexallon, Isogen, Nocxium, Zydrine, Megacyte, Morphite", "Minerals"),
    **_group("Crystalline Polycrase, Crystalline Moissanite, Crystalline Raspite, Crystalline Kangite, "
             "Neo-Jadarite, Eleutrium, Chromodynamic Tricarboxyls", "Specialty minerals"),
    **_group("Compressed Gneiss IV-Grade, Compressed Zeolites, Compressed Veldspar, Compressed Scordite, "
             "Compressed Bitumens", "Compressed ore"),
    **_group("Zero-Point Field Manipulator, Auto-Integrity Preservation Seal, Life Support Backup Unit, "
             "Photon Microprocessor, Quantum Microprocessor, Core Temperature Regulator", "T2 components"),
    **_group("Neurolink Protection Cell, Capital Core Temperature Regulator, Capital Construction Parts",
             "Capital components"),
}
