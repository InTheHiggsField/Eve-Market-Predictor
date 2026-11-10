"""Minimal client for the public EVE ESI market endpoints (no auth needed)."""
from __future__ import annotations

import time

import requests

BASE_URL = "https://esi.evetech.net"
USER_AGENT = "Eve-Market-Predictor/0.1 (+https://github.com/InTheHiggsField/Eve-Market-Predictor)"

THE_FORGE = 10000002  # region containing Jita
JITA_4_4 = 60003760  # Jita IV - Moon 4 - Caldari Navy Assembly Plant

#Use when auth is not necessary
class ESIClient:
    def __init__(self, session: requests.Session | None = None, max_retries: int = 4):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.max_retries = max_retries

    def _get(self, path: str, params: dict | None = None, token: str | None = None) -> requests.Response:
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else None
        for attempt in range(self.max_retries + 1):
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            # ESI error limiting: back off before we get banned.
            remain = resp.headers.get("X-ESI-Error-Limit-Remain")
            if remain is not None and int(remain) < 10:
                time.sleep(int(resp.headers.get("X-ESI-Error-Limit-Reset", "60")))
            if resp.status_code == 200:
                return resp
            if resp.status_code in (401, 403, 404):
                resp.raise_for_status()
            if resp.status_code in (420, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(int(resp.headers.get("Retry-After", 2 ** (attempt + 1))))
                continue
            resp.raise_for_status()
        raise RuntimeError(f"ESI request failed after retries: {url}")

    def market_prices(self) -> list[dict]:
        """Universe-wide average/adjusted prices for every tradable type."""
        return self._get("/markets/prices").json()

    def market_history(self, type_id: int, region_id: int = THE_FORGE) -> list[dict]:
        """Daily history (about 13 months): date, average, highest, lowest, volume, order_count."""
        return self._get(f"/markets/{region_id}/history", {"type_id": type_id}).json()

    def market_orders(self, type_id: int, region_id: int = THE_FORGE, order_type: str = "all") -> list[dict]:
        """All live orders for a type in a region, following pagination."""
        params = {"type_id": type_id, "order_type": order_type, "page": 1}
        resp = self._get(f"/markets/{region_id}/orders", params)
        orders = resp.json()
        pages = int(resp.headers.get("X-Pages", "1"))
        for page in range(2, pages + 1):
            params["page"] = page
            orders.extend(self._get(f"/markets/{region_id}/orders", params).json())
        return orders

    # ---- public lookups ----
    def station(self, station_id: int) -> dict:
        return self._get(f"/universe/stations/{station_id}").json()

    def system(self, system_id: int) -> dict:
        return self._get(f"/universe/systems/{system_id}").json()

    def regions(self) -> list[int]:
        """Known-space region IDs (no wormhole or abyssal regions)."""
        return [r for r in self._get("/universe/regions").json() if r < 11000000]

    # ---- need a logged-in character (see auth.py) ----
    def structure(self, structure_id: int, token: str) -> dict:
        """Name and solar system of a player-owned structure the character can dock at."""
        return self._get(f"/universe/structures/{structure_id}", token=token).json()

    def structure_orders(self, structure_id: int, token: str) -> list[dict]:
        """Every live order in a player-owned structure's market, following pagination."""
        resp = self._get(f"/markets/structures/{structure_id}", {"page": 1}, token=token)
        orders = resp.json()
        for page in range(2, int(resp.headers.get("X-Pages", "1")) + 1):
            orders.extend(self._get(f"/markets/structures/{structure_id}", {"page": page}, token=token).json())
        return orders


def is_structure(location_id: int) -> bool:
    """NPC stations have IDs around 60,000,000; player structures are above 1,000,000,000,000."""
    return location_id >= 1_000_000_000_000
