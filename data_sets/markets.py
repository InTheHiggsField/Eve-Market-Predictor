"""Trade hubs the predictor tracks.

ESI keeps price history per region, so each hub's history is its whole region's history.
Order-book snapshots (best bid/ask, depth) are taken at the hub station itself.
Picked from live order counts across all regions (Nov 2026); the five high-sec hubs
carry far more orders for the tracked items than anywhere else.
"""

MARKETS = {
    "Jita": {"region_id": 10000002, "region": "The Forge", "station_id": 60003760,
             "station": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "security": "high"},
    "Amarr": {"region_id": 10000043, "region": "Domain", "station_id": 60008494,
              "station": "Amarr VIII (Oris) - Emperor Family Academy", "security": "high"},
    "Dodixie": {"region_id": 10000032, "region": "Sinq Laison", "station_id": 60011866,
                "station": "Dodixie IX - Moon 20 - Federation Navy Assembly Plant", "security": "high"},
    "Hek": {"region_id": 10000042, "region": "Metropolis", "station_id": 60005686,
            "station": "Hek VIII - Moon 12 - Boundless Creation Factory", "security": "high"},
    "Rens": {"region_id": 10000030, "region": "Heimatar", "station_id": 60004588,
             "station": "Rens VI - Moon 8 - Brutor Tribe Treasury", "security": "high"},
}

DEFAULT_MARKET = "Jita"
