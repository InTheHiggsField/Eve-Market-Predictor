"""Find where the tracked items actually trade, including player-owned citadels.

    python -m eve_market hubs --security low

Scans the public regional order books for a sample of tracked items across every known-space
region and ranks locations by order count. Public citadel orders appear in these books, but a
citadel's name and system can only be looked up with a logged-in character (see auth.py);
without a login they're listed by ID only.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import auth
from .esi import ESIClient, is_structure

log = logging.getLogger(__name__)

# Liquid items that trade nearly everywhere: minerals, injectors, a T2 component, popular ships.
SAMPLE_TYPES = [34, 35, 36, 11399, 40520, 40519, 25619, 52312, 57486, 17715, 28710]


def security_class(sec: float) -> str:
    # EVE shows security to one decimal, but any system above 0.0 counts as low-sec (0.03 shows as 0.1).
    return "high" if round(sec, 1) >= 0.5 else "low" if sec > 0.0 else "null"


def scan(client: ESIClient | None = None, workers: int = 10, top: int = 60) -> list[dict]:
    client = client or ESIClient()
    regions = client.regions()
    agg = defaultdict(lambda: {"orders": 0, "sell_isk": 0.0, "types": set(), "region_id": None})

    def fetch(job):
        region, type_id = job
        try:
            return region, type_id, client.market_orders(type_id, region)
        except Exception:
            return region, type_id, []

    log.info("Scanning %d regions x %d items…", len(regions), len(SAMPLE_TYPES))
    with ThreadPoolExecutor(workers) as pool:
        for region, type_id, orders in pool.map(fetch, [(r, t) for r in regions for t in SAMPLE_TYPES]):
            for o in orders:
                a = agg[o["location_id"]]
                a["orders"] += 1
                a["types"].add(type_id)
                a["region_id"] = region
                if not o["is_buy_order"]:
                    a["sell_isk"] += o["price"] * o["volume_remain"]

    token = None
    if auth.has_login():
        token = auth.access_token()
    else:
        log.info("Not logged in: citadels will be listed by ID only. Run `python -m eve_market login` to see names.")

    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["orders"])[:top]
    system_cache: dict[int, dict] = {}

    def describe(item):
        loc, a = item
        row = {"location_id": loc, "orders": a["orders"], "types": len(a["types"]), "sell_isk": a["sell_isk"],
               "region_id": a["region_id"], "kind": "citadel" if is_structure(loc) else "station",
               "name": None, "system": None, "security": None}
        try:
            info = client.structure(loc, token) if row["kind"] == "citadel" and token else (
                client.station(loc) if row["kind"] == "station" else None)
        except Exception:
            info = None  # no docking access, or structure no longer exists
        if info:
            row["name"] = info["name"]
            sys_id = info.get("system_id") or info.get("solar_system_id")
            sysinfo = system_cache.get(sys_id) or system_cache.setdefault(sys_id, client.system(sys_id))
            row["system"], row["security"] = sysinfo["name"], sysinfo["security_status"]
        return row

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(describe, ranked))


def print_hubs(rows: list[dict], security: str | None = None) -> None:
    for r in rows:
        cls = security_class(r["security"]) if r["security"] is not None else "?"
        if security and cls not in (security, "?"):
            continue
        name = r["name"] or f"(citadel {r['location_id']}; log in to see name)"
        sec = f"{r['security']:.2f}" if r["security"] is not None else "  ? "
        print(f"{cls:>4} {sec:>5}  {r['orders']:5d} orders  {r['types']:2d}/{len(SAMPLE_TYPES)} items  "
              f"region {r['region_id']}  id {r['location_id']}  {name}")
