"""Items the predictor tracks: in-game name -> type_id.

Chosen for high value and high daily ISK turnover (approved list, Nov 2026).
Only these items are collected and stored.
"""
#Tracked high volume items
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

# Faction ship hulls (navy and pirate, metaGroupID 4) whose Jita 4-4 price was above 20M ISK.
# Generated on 2026-11-20 with the 20M ISK Jita cutoff by scripts/find_faction_ships.py; per-ship prices
# and 30-day ISK volume are in data/faction_ships_20m.csv. Left out: hulls with no Jita sell orders and no
# trades in 30 days (AT/tournament prizes, faction capitals, Imperial/State/Federate/Tribal Issue), the
# limited-edition yachts, campaign buses, corvettes and shuttle, and the navy frigates (all under 10M).
# Not collected hourly: backfill it with `eve backfill --list faction_ships`.
FACTION_SHIPS = {
    # Pirate frigates
    "Astero": 33468,
    "Cruor": 17926,
    "Daredevil": 17928,
    "Dramiel": 17932,
    "Echelon": 3532,
    "Garmur": 33816,
    "Succubus": 17924,
    "Worm": 17930,
    # Navy cruisers
    "Augoror Navy Issue": 29337,
    "Caracal Navy Issue": 17634,
    "Exequror Navy Issue": 29344,
    "Omen Navy Issue": 17709,
    "Osprey Navy Issue": 29340,
    "Scythe Fleet Issue": 29336,
    "Stabber Fleet Issue": 17713,
    "Vexor Navy Issue": 17843,
    # Pirate cruisers
    "Ashimmu": 17922,
    "Cynabal": 17720,
    "Gila": 17715,
    "Orthrus": 33818,
    "Phantasm": 17718,
    "Stratios": 33470,
    "Vigilant": 17722,
    # Navy battlecruisers
    "Brutix Navy Issue": 33151,
    "Drake Navy Issue": 33153,
    "Harbinger Navy Issue": 33155,
    "Hurricane Fleet Issue": 33157,
    # Navy battleships
    "Apocalypse Navy Issue": 17726,
    "Armageddon Navy Issue": 32305,
    "Dominix Navy Issue": 32307,
    "Megathron Navy Issue": 17728,
    "Raven Navy Issue": 17636,
    "Scorpion Navy Issue": 32309,
    "Tempest Fleet Issue": 17732,
    "Typhoon Fleet Issue": 32311,
    # Pirate battleships
    "Barghest": 33820,
    "Bhaalgorn": 17920,
    "Machariel": 17738,
    "Nestor": 33472,
    "Nightmare": 17736,
    "Rattlesnake": 17918,
    "Vindicator": 17740,
    # Expedition command ships (Sisters of EVE)
    "Odysseus": 89607,
}

ITEM_LISTS = {
    "core": TRACKED_ITEMS,
    "commodities": COMMODITY_ITEMS,
    # "all" is what the hourly collect task pulls, so faction ships stay out of it.
    "all": {**TRACKED_ITEMS, **COMMODITY_ITEMS},
    "faction_ships": FACTION_SHIPS,
}

# Every item on any list. The dashboard and the health metrics use this, so faction ships show up there
# once backfilled even though they are not part of "all" (the hourly collect set).
EVERY_ITEM = {name: tid for lst in ITEM_LISTS.values() for name, tid in lst.items()}

# PLEX trades on its own global market rather than in Jita (The Forge).
GLOBAL_PLEX_MARKET = 19000001
REGION_OVERRIDES = {"PLEX": GLOBAL_PLEX_MARKET}


def _group(names: str, label: str) -> dict[str, str]:
    return {n.strip(): label for n in names.split(",")}


# Display groups for the dashboard.
#This is a dictionary that groups different items by their general category set by CCP
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
    # Faction ships (Gila stays under "Ships" above)
    **_group("Astero, Cruor, Daredevil, Dramiel, Echelon, Garmur, Succubus, Worm", "Faction frigates"),
    **_group("Augoror Navy Issue, Caracal Navy Issue, Exequror Navy Issue, Omen Navy Issue, Osprey Navy Issue, "
             "Scythe Fleet Issue, Stabber Fleet Issue, Vexor Navy Issue, Ashimmu, Cynabal, Orthrus, Phantasm, "
             "Stratios, Vigilant", "Faction cruisers"),
    **_group("Brutix Navy Issue, Drake Navy Issue, Harbinger Navy Issue, Hurricane Fleet Issue", "Faction battlecruisers"),
    **_group("Apocalypse Navy Issue, Armageddon Navy Issue, Dominix Navy Issue, Megathron Navy Issue, Raven Navy Issue, "
             "Scorpion Navy Issue, Tempest Fleet Issue, Typhoon Fleet Issue, Barghest, Bhaalgorn, Machariel, Nestor, "
             "Nightmare, Rattlesnake, Vindicator", "Faction battleships"),
    **_group("Odysseus", "Faction command ships"),
}
