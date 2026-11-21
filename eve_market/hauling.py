"""Hauling: which hub pays the most for an item bought at another hub.

    python -m eve_market hauling --origin Jita            # best runs from Jita, and warm the ESI caches

Two layers, so the same numbers can be worked out in Python (tests, the CLI, /api/hauling) and in the
browser (static/hauler.js, which needs it for the static GitHub Pages build):

* `build_inputs` (I/O): the latest order book at every hub station for each item, the item's volume,
  daily traded volume per hub, and the jumps between hub systems. Volumes, station systems, routes and
  system security come from ESI once and are cached in SQLite.
* `evaluate` / `rank_destinations` / `best_runs` (pure): profit math on those inputs.
* `rank_trips` / `arbitrage` (pure): every buy hub / sell hub pair, optionally starting from the
  hauler's current solar system (an empty first leg to the buy hub, then the loaded leg).

Order books are the individual orders the collector keeps, which are only those priced within 20% of
the best price on each side (collect.RAW_ORDER_BAND). Depth beyond that band is not counted, so a very
large quantity can look less fillable than it really is (and it would be at a much worse price anyway).
"""
from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd

from eve_market.config import ITEM_GROUPS, ITEM_LISTS, MARKETS, REGION_OVERRIDES
from eve_market.esi import ESIClient
from eve_market.hubs import security_class

log = logging.getLogger(__name__)

# NPC station fees before skills and standings. Structures (and Zarzakh's own rules) can differ;
# the same defaults are used everywhere and the page says so.
SALES_TAX = 0.075       # Accounting: -11% of this per level (3.375% at level V)
BROKER_FEE = 0.03       # Broker Relations: -0.3 percentage points per level (1.5% at level V)
ACCOUNTING_STEP = 0.11
BROKER_STEP = 0.003

DEFAULT_BUDGET = 1e9    # ISK; "auto" quantity spends at most this at the origin
AUTO_SELL_DAYS = 7      # and, for listed (patient) sales, is at most this many days of destination volume
ROUTE_MAX_AGE_DAYS = 7
VOLUME_MAX_AGE_DAYS = 90
STALE_HOURS = 6         # flag a book whose snapshot is this much older than the newest one
DAILY_VOLUME_DAYS = 30
LISTING_SANITY = 1.5    # patient mode: a lowest sell order above 1.5x the daily average isn't a real price


# ---------------------------------------------------------------- pure math

def fee_rates(accounting: int = 0, broker_relations: int = 0, sales_tax: float = SALES_TAX,
              broker_fee: float = BROKER_FEE) -> dict:
    """Sales tax and broker fee after skills (levels 0-5)."""
    acc = min(max(int(accounting), 0), 5)
    br = min(max(int(broker_relations), 0), 5)
    return {"sales_tax": sales_tax * (1 - ACCOUNTING_STEP * acc),
            "broker_fee": max(broker_fee - BROKER_STEP * br, 0.0)}


def walk(levels: list, qty: float) -> tuple[float, float]:
    """Fill `qty` units from price levels [[price, volume], ...] sorted best first.
    Returns (units filled, ISK paid or received)."""
    got, isk = 0, 0.0
    for price, vol in levels:
        if got >= qty:
            break
        take = min(vol, qty - got)
        got += take
        isk += take * price
    return got, isk


def depth(levels: list) -> float:
    return sum(v for _, v in levels)


def _tick(price: float) -> float:
    """Market prices keep 4 significant digits; this is the step at `price`'s magnitude."""
    return max(10 ** (math.floor(math.log10(price)) - 3), 0.01)


def undercut(price: float) -> float:
    """Highest valid price below `price` (4 significant digits, 0.01 ISK minimum step)."""
    if price <= 0.01:
        return price
    step = _tick(price)
    below = (math.ceil(price / step - 1e-9) - 1) * step
    if below < 10 ** math.floor(math.log10(price)) and step > 0.01:  # price was a power of ten: finer step below it
        below = price - step / 10
    return round(below, 2)


def overbid(price: float) -> float:
    """Lowest valid price above `price`, for outbidding the best buy order."""
    step = _tick(price)
    return round((math.floor(price / step + 1e-9) + 1) * step, 2)


def max_profitable_units(asks: list, bids: list, buy_mult: float, sell_mult: float) -> float:
    """Units worth carrying when buying up the asks and selling into the bids: keep going while the
    next unit's after-fee sale beats its cost."""
    i = j = 0
    a_left = asks[0][1] if asks else 0
    b_left = bids[0][1] if bids else 0
    units = 0
    while i < len(asks) and j < len(bids):
        if bids[j][0] * sell_mult <= asks[i][0] * buy_mult:
            break
        take = min(a_left, b_left)
        units += take
        a_left -= take
        b_left -= take
        if a_left <= 0:
            i += 1
            a_left = asks[i][1] if i < len(asks) else 0
        if b_left <= 0:
            j += 1
            b_left = bids[j][1] if j < len(bids) else 0
    return units


def auto_qty(item: dict, origin: str, budget: float = DEFAULT_BUDGET) -> int:
    """A sensible quantity: what `budget` ISK buys from the origin's sell orders (at least 1)."""
    book = (item["books"] or {}).get(origin)
    if not book or not book["asks"]:
        return 1
    got, isk = 0, 0.0
    for price, vol in book["asks"]:
        take = min(vol, max(int((budget - isk) // price), 0))
        got += take
        isk += take * price
        if take < vol:
            break
    return max(int(got), 1)


def evaluate(item: dict, origin: str, dest: str, qty: float, fees: dict, mode: str = "instant",
             buy_mode: str = "instant", route: dict | None = None) -> dict:
    """Profit for hauling `qty` units of one item from `origin` to `dest`.

    mode: "instant" sells into the destination's buy orders (walking depth); "patient" lists a sell
    order one tick under the destination's lowest sell order and pays the broker fee on it.
    buy_mode: "instant" buys the origin's sell orders (walking depth); "order" places a buy order one
    tick over the best bid and pays the broker fee (fill time is not estimated)."""
    books = item.get("books") or {}
    ob, db = books.get(origin), books.get(dest)
    flags: list[str] = []
    row = {"destination": dest, "qty": qty, "filled": 0, "buy_price": None, "sell_price": None, "cost": None,
           "revenue": None, "sales_tax": None, "broker": None, "net_profit": None, "profit_per_unit": None,
           "roi": None, "m3": None, "profit_per_m3": None, "profit_per_jump": None,
           "daily_volume": (item.get("daily_volume") or {}).get(dest), "days_to_sell": None,
           "max_profitable": None, "risk": None, "flags": flags}
    rt = route or {}
    row.update({k: rt.get(k) for k in ("jumps", "jumps_secure", "low", "null", "low_secure", "null_secure")})
    if item.get("global_market"):
        flags.append("one global market: nothing to haul")
        return row
    if not ob:
        flags.append(f"no order snapshot at {origin}")
        return row
    if not db:
        flags.append(f"no order snapshot at {dest}")
        return row
    for hub, b in ((origin, ob), (dest, db)):
        if b.get("stale"):
            flags.append(f"{hub} snapshot is old ({b['taken_at']})")
    if qty <= 0:
        flags.append("doesn't fit in the cargo hold")
        return row

    # Buying at the origin
    if buy_mode == "order":
        if not ob["bids"]:
            flags.append(f"no buy orders at {origin} to outbid")
            return row
        buy_price = overbid(ob["bids"][0][0])
        buy_avail = qty
        buy_mult = 1 + fees["broker_fee"]
        flags.append("buy order: fill time not estimated")
    else:
        if not ob["asks"]:
            flags.append(f"no sell orders at {origin}")
            return row
        buy_price, buy_avail, buy_mult = None, depth(ob["asks"]), 1.0

    # Selling at the destination
    if mode == "patient":
        ref = (item.get("ref_price") or {}).get(dest)
        if db["asks"]:
            sell_price = undercut(db["asks"][0][0])
            if ref and sell_price > ref * LISTING_SANITY:
                # The cheapest listing is far above what actually trades: nobody is buying at it.
                flags.append(f"{dest} listings are {sell_price / ref:.1f}x the daily average: priced at the average")
                sell_price = ref
        elif ref:
            sell_price = ref
            flags.append(f"no sell orders at {dest}: priced at its daily average")
        else:
            flags.append(f"no sell orders or price history at {dest}")
            return row
        sell_avail = qty
        sell_mult = 1 - fees["sales_tax"] - fees["broker_fee"]
    else:
        if not db["bids"]:
            flags.append(f"no buy orders at {dest}")
            return row
        sell_price, sell_avail = None, depth(db["bids"])
        sell_mult = 1 - fees["sales_tax"]

    filled = min(qty, buy_avail, sell_avail)
    if filled < qty:
        short = f"{origin} sell orders" if buy_avail < sell_avail else f"{dest} buy orders"
        flags.append(f"only {filled:,.0f} of {qty:,.0f} fillable from {short}")
    if buy_price is None:
        _, gross_cost = walk(ob["asks"], filled)
    else:
        gross_cost = buy_price * filled
    if sell_price is None:
        _, gross_rev = walk(db["bids"], filled)
    else:
        gross_rev = sell_price * filled
    buy_broker = gross_cost * (buy_mult - 1)
    tax = gross_rev * fees["sales_tax"]
    sell_broker = gross_rev * fees["broker_fee"] if mode == "patient" else 0.0
    cost = gross_cost + buy_broker
    net = gross_rev - tax - sell_broker - cost

    row.update(filled=filled, cost=cost, revenue=gross_rev, sales_tax=tax, broker=buy_broker + sell_broker,
               net_profit=net, buy_price=gross_cost / filled if filled else None,
               sell_price=gross_rev / filled if filled else None)
    if filled:
        row["profit_per_unit"] = net / filled
        row["roi"] = net / cost if cost else None
    vol = item.get("volume_m3")
    if vol:
        row["m3"] = vol * filled
        row["profit_per_m3"] = net / row["m3"] if row["m3"] else None
    else:
        flags.append("item volume unknown")
    jumps = row.get("jumps_secure") if row.get("jumps_secure") is not None else row.get("jumps")
    if jumps:
        row["profit_per_jump"] = net / jumps
    elif route is None:
        flags.append("route unknown")
    dv = row["daily_volume"]
    if mode == "patient":
        row["days_to_sell"] = filled / dv if dv else None
        if not dv:
            flags.append(f"no recent trades in {dest}'s region")
    else:
        row["days_to_sell"] = 0.0
    if buy_mode == "instant" and mode == "instant":
        row["max_profitable"] = max_profitable_units(ob["asks"], db["bids"], buy_mult, sell_mult)
    return row


def _sort_key(r: dict):
    return (r["net_profit"] is None, -(r["net_profit"] or 0))


SORT_KEYS = ("net_profit", "roi", "profit_per_m3", "profit_per_jump", "profit_per_total_jump")


def _sort_by(key: str):
    """Highest first, rows without a value last."""
    return lambda r: (r.get(key) is None, -(r.get(key) or 0))


def trip_qty(item: dict, origin: str, dest: str, qty: float | None, mode: str, cargo_m3: float | None,
             budget: float) -> float:
    """Units to carry: `qty`, or the auto quantity (what `budget` buys at the origin, and for listed
    sales at most about a week of the destination's trade), capped by the cargo hold."""
    q = auto_qty(item, origin, budget) if not qty else qty
    vol = item.get("volume_m3")
    if cargo_m3 and vol:
        q = min(q, math.floor(cargo_m3 / vol))
    dv = (item.get("daily_volume") or {}).get(dest)
    if not qty and mode == "patient" and dv:
        q = min(q, max(math.ceil(dv * AUTO_SELL_DAYS), 1))
    return q


def rank_destinations(inputs: dict, item: dict, qty: float | None = None, fees: dict | None = None,
                      mode: str = "instant", buy_mode: str = "instant", cargo_m3: float | None = None,
                      budget: float = DEFAULT_BUDGET) -> list[dict]:
    """Every other hub for one item, best total net profit first. qty None = auto quantity."""
    fees = fees or fee_rates()
    origin = inputs["origin"]
    rows = [evaluate(item, origin, d, trip_qty(item, origin, d, qty, mode, cargo_m3, budget), fees, mode, buy_mode,
                     (inputs.get("routes") or {}).get(d))
            for d in inputs["hubs"] if d != origin]
    return sorted(rows, key=_sort_key)


LEG_KEYS = ("jumps", "jumps_secure", "low", "null", "low_secure", "null_secure")


def leg_jumps(leg: dict | None) -> int | None:
    """Jumps counted for a leg: the high-sec-only route where one exists, else the shortest."""
    if not leg:
        return None
    return leg["jumps_secure"] if leg.get("jumps_secure") is not None else leg.get("jumps")


def evaluate_trip(market: dict, item: dict, origin: str, dest: str, qty: float | None, fees: dict,
                  mode: str = "instant", buy_mode: str = "instant", cargo_m3: float | None = None,
                  budget: float = DEFAULT_BUDGET, start: dict | None = None) -> dict:
    """One buy hub -> sell hub trip. With `start` (see start_legs), the hauler first flies empty from
    their current system to the buy hub; that leg costs nothing but adds jumps."""
    route = (market.get("route_matrix") or {}).get(origin, {}).get(dest)
    row = evaluate(item, origin, dest, trip_qty(item, origin, dest, qty, mode, cargo_m3, budget), fees, mode,
                   buy_mode, route)
    row["origin"] = origin
    loaded = leg_jumps(route)
    row["loaded_jumps"] = loaded
    row["start"] = row["start_jumps"] = None
    total = loaded
    if start is not None:
        leg = (start.get("legs") or {}).get(origin)
        row["start"] = {**{k: (leg or {}).get(k) for k in LEG_KEYS}, "risk": None}
        row["start_jumps"] = leg_jumps(leg)
        total = loaded + row["start_jumps"] if loaded is not None and row["start_jumps"] is not None else None
        if leg is None:
            row["flags"].append(f"route from {start['system']['name']} to {origin} unknown")
    row["total_jumps"] = total
    net = row["net_profit"]
    row["profit_per_total_jump"] = net / total if net is not None and total else None
    return row


def rank_trips(market: dict, item: dict, qty: float | None = None, fees: dict | None = None,
               mode: str = "instant", buy_mode: str = "instant", cargo_m3: float | None = None,
               budget: float = DEFAULT_BUDGET, start: dict | None = None, sort: str | None = None) -> list[dict]:
    """Every buy hub -> sell hub pair for one item. Sorted by profit per total jump when a starting
    system is given, else by total net profit (or by `sort`, one of SORT_KEYS)."""
    fees = fees or fee_rates()
    rows = [evaluate_trip(market, item, a, b, qty, fees, mode, buy_mode, cargo_m3, budget, start)
            for a in market["hubs"] for b in market["hubs"] if a != b]
    key = sort or ("profit_per_total_jump" if start else "net_profit")
    return sorted(rows, key=_sort_by(key))


def arbitrage(market: dict, qty: float | None = None, fees: dict | None = None, mode: str = "instant",
              buy_mode: str = "instant", cargo_m3: float | None = None, budget: float = DEFAULT_BUDGET,
              start: dict | None = None, min_profit: float = 0.0, min_roi: float | None = None,
              buy_hub: str | None = None, sell_hub: str | None = None, sort: str = "net_profit",
              top: int | None = 100) -> list[dict]:
    """Profitable trades over every item and every ordered hub pair, after fees, best first.
    min_roi is a fraction (0.1 = 10%)."""
    fees = fees or fee_rates()
    out = []
    for item in market["items"]:
        for a in market["hubs"]:
            if buy_hub and a != buy_hub:
                continue
            for b in market["hubs"]:
                if a == b or (sell_hub and b != sell_hub):
                    continue
                r = evaluate_trip(market, item, a, b, qty, fees, mode, buy_mode, cargo_m3, budget, start)
                net = r["net_profit"]
                if net is None or net <= 0 or net < min_profit:
                    continue
                if min_roi is not None and (r["roi"] is None or r["roi"] < min_roi):
                    continue
                out.append({"type_id": item["type_id"], "name": item["name"], "group": item.get("group", ""), **r})
    out.sort(key=_sort_by(sort))
    return out[:top] if top else out


def basket_totals(per_item: list[list[dict]]) -> list[dict]:
    """Sum each destination's rows over a basket of items (each item's own quantity), best first.
    An item with no usable price at a destination adds nothing there and is counted as missing."""
    out: dict[str, dict] = {}
    for rows in per_item:
        for r in rows:
            t = out.setdefault(r["destination"], {
                "destination": r["destination"], "net_profit": 0.0, "cost": 0.0, "revenue": 0.0, "m3": 0.0,
                "items": 0, "missing": 0, "days_to_sell": 0.0, "risk": None,
                **{k: r.get(k) for k in ("jumps", "jumps_secure", "low", "null", "low_secure", "null_secure")}})
            if r["net_profit"] is None:
                t["missing"] += 1
                continue
            t["items"] += 1
            for k in ("net_profit", "cost", "revenue"):
                t[k] += r[k]
            t["m3"] += r["m3"] or 0.0
            t["days_to_sell"] = max(t["days_to_sell"], r["days_to_sell"] or 0.0)
    rows = []
    for t in out.values():
        if not t["items"]:
            t.update(net_profit=None, cost=None, revenue=None)
        t["roi"] = t["net_profit"] / t["cost"] if t["cost"] else None
        jumps = t["jumps_secure"] if t["jumps_secure"] is not None else t["jumps"]
        t["profit_per_jump"] = t["net_profit"] / jumps if jumps and t["net_profit"] is not None else None
        rows.append(t)
    return sorted(rows, key=_sort_key)


def best_runs(inputs: dict, fees: dict | None = None, mode: str = "instant", buy_mode: str = "instant",
              cargo_m3: float | None = None, budget: float = DEFAULT_BUDGET, top: int = 20,
              qty: float | None = None) -> list[dict]:
    """The most profitable item/destination pairs from the origin, each item at `qty` units
    (None: its auto quantity)."""
    out = []
    for item in inputs["items"]:
        for r in rank_destinations(inputs, item, qty, fees, mode, buy_mode, cargo_m3, budget):
            if r["net_profit"] is not None and r["net_profit"] > 0:
                out.append({"type_id": item["type_id"], "name": item["name"], "group": item.get("group", ""), **r})
    return sorted(out, key=_sort_key)[:top]


def hauled_items() -> dict[str, int]:
    """Items the Hauler offers: everything the hourly collect snapshots (it needs their order books)."""
    return ITEM_LISTS["all"]


def page_defaults() -> dict:
    """Fee and quantity constants, for the Hauler page's controls and the About page."""
    return {"sales_tax": SALES_TAX, "broker_fee": BROKER_FEE, "accounting_step": ACCOUNTING_STEP,
            "broker_step": BROKER_STEP, "budget": DEFAULT_BUDGET, "auto_sell_days": AUTO_SELL_DAYS,
            "listing_sanity": LISTING_SANITY, "route_days": ROUTE_MAX_AGE_DAYS, "stale_hours": STALE_HOURS,
            "best_fees": fee_rates(5, 5)}


# ---------------------------------------------------------------- ESI lookups, cached in SQLite

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _older_than(stamp: str, days: float) -> bool:
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > timedelta(days=days)


def item_volumes(conn, type_ids: list[int], client: ESIClient | None = None, offline: bool = False) -> dict[int, float]:
    """m3 per unit as hauled: the packaged volume (ships are much smaller packaged)."""
    rows = {r[0]: r[1:] for r in conn.execute(
        f"SELECT type_id, volume, packaged_volume, fetched_at FROM type_volumes "
        f"WHERE type_id IN ({','.join('?' * len(type_ids))})", type_ids)} if type_ids else {}
    missing = [t for t in type_ids if t not in rows or _older_than(rows[t][2], VOLUME_MAX_AGE_DAYS)]
    if missing and not offline:
        client = client or ESIClient()

        def fetch(t):
            try:
                return t, client.type_info(t)
            except Exception as exc:
                log.warning("type %s volume lookup failed: %s", t, exc)
                return t, None
        with ThreadPoolExecutor(8) as pool:
            got = [(t, info) for t, info in pool.map(fetch, missing) if info]
        stamp = _now()
        conn.executemany("INSERT OR REPLACE INTO type_volumes VALUES (?,?,?,?)",
                         [(t, i.get("volume"), i.get("packaged_volume"), stamp) for t, i in got])
        conn.commit()
        rows.update({t: (i.get("volume"), i.get("packaged_volume"), stamp) for t, i in got})
    return {t: (r[1] or r[0]) for t, r in rows.items() if (r[1] or r[0])}


def hub_systems(conn, client: ESIClient | None = None, offline: bool = False) -> dict[str, int]:
    """Solar system of each hub station."""
    known = dict(conn.execute("SELECT station_id, system_id FROM station_systems"))
    out = {}
    for name, m in MARKETS.items():
        sid = m["station_id"]
        if sid not in known and not offline:
            try:
                client = client or ESIClient()
                known[sid] = client.station(sid)["system_id"]
                conn.execute("INSERT OR REPLACE INTO station_systems VALUES (?,?)", (sid, known[sid]))
                conn.commit()
            except Exception as exc:
                log.warning("station %s lookup failed: %s", sid, exc)
        if sid in known:
            out[name] = known[sid]
    return out


def _systems_info(conn, system_ids: set[int], client, offline: bool) -> dict[int, tuple[str, float]]:
    if not system_ids:
        return {}
    ids = list(system_ids)
    have = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT system_id, name, security_status FROM systems WHERE system_id IN ({','.join('?' * len(ids))})", ids)}
    missing = [s for s in ids if s not in have]
    if missing and not offline:
        client = client or ESIClient()

        def fetch(s):
            try:
                return s, client.system(s)
            except Exception as exc:
                log.warning("system %s lookup failed: %s", s, exc)
                return s, None
        with ThreadPoolExecutor(8) as pool:
            got = [(s, i) for s, i in pool.map(fetch, missing) if i]
        stamp = _now()
        conn.executemany("INSERT OR REPLACE INTO systems VALUES (?,?,?,?)",
                         [(s, i["name"], i["security_status"], stamp) for s, i in got])
        conn.commit()
        have.update({s: (i["name"], i["security_status"]) for s, i in got})
    return have


def cached_route(conn, origin_system: int, dest_system: int, flag: str, client: ESIClient | None = None,
                 offline: bool = False, max_age_days: float = ROUTE_MAX_AGE_DAYS) -> list[int] | None:
    """System IDs on the route (origin first), from the cache if fresh enough, else from ESI. A stale
    cached route is still used if ESI can't be reached."""
    if origin_system == dest_system:
        return [origin_system]
    row = conn.execute("SELECT systems, fetched_at FROM routes WHERE origin_system=? AND destination_system=? "
                       "AND flag=?", (origin_system, dest_system, flag)).fetchone()
    if row and (offline or not _older_than(row[1], max_age_days)):
        return json.loads(row[0])
    if offline:
        return None
    try:
        systems = (client or ESIClient()).route(origin_system, dest_system, flag)
    except Exception as exc:
        log.warning("route %s -> %s (%s) failed: %s", origin_system, dest_system, flag, exc)
        return json.loads(row[0]) if row else None
    conn.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?)",
                 (origin_system, dest_system, flag, json.dumps(systems), _now()))
    conn.commit()
    return systems


def _prefetch_routes(conn, pairs: list[tuple[int, int]], client, offline: bool) -> None:
    """Fetch missing or old routes (both flags) in parallel, so cached_route then only reads the cache."""
    if offline:
        return
    fresh = {(r[0], r[1], r[2]) for r in conn.execute("SELECT origin_system, destination_system, flag, fetched_at "
                                                       "FROM routes") if not _older_than(r[3], ROUTE_MAX_AGE_DAYS)}
    jobs = [(o, d, f) for o, d in sorted(set(pairs)) if o != d for f in ("shortest", "secure")
            if (o, d, f) not in fresh]
    if not jobs:
        return
    client = client or ESIClient()

    def fetch(job):
        try:
            return job, client.route(*job)
        except Exception as exc:
            log.warning("route %s -> %s (%s) failed: %s", *job, exc)
            return job, None
    with ThreadPoolExecutor(8) as pool:
        got = [(j, r) for j, r in pool.map(fetch, jobs) if r]
    stamp = _now()
    conn.executemany("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?)",
                     [(o, d, f, json.dumps(r), stamp) for (o, d, f), r in got])
    conn.commit()


def route_legs(conn, pairs: dict, client: ESIClient | None = None, offline: bool = False) -> dict:
    """{key: (origin_system, destination_system)} -> {key: leg}. A leg has jumps, low and null counts
    for the shortest and the high-sec-only route, the shortest route's systems with their security,
    and a `risk` placeholder (None until a risk score exists). Pairs with no known route are left out."""
    _prefetch_routes(conn, list(pairs.values()), client, offline)
    # Everything fetchable was just fetched; read the cache (an old route is kept if ESI failed).
    raw = {(k, f): cached_route(conn, o, d, f, offline=True)
           for k, (o, d) in pairs.items() for f in ("shortest", "secure")}
    info = _systems_info(conn, {s for r in raw.values() if r for s in r}, client, offline)
    out = {}
    for k in pairs:
        if not raw[(k, "shortest")] and not raw[(k, "secure")]:
            continue
        leg = {"risk": None}
        for flag, suffix in (("shortest", ""), ("secure", "_secure")):
            route = raw[(k, flag)]
            if not route:
                leg.update({f"jumps{suffix}": None, f"low{suffix}": None, f"null{suffix}": None})
                continue
            classes = [security_class(info[s][1]) if s in info else "?" for s in route]
            leg[f"jumps{suffix}"] = len(route) - 1
            leg[f"low{suffix}"] = classes.count("low")
            leg[f"null{suffix}"] = classes.count("null")
            if flag == "shortest":
                leg["systems"] = [{"id": s, "name": info.get(s, ("?",))[0],
                                   "security": info[s][1] if s in info else None} for s in route]
        out[k] = leg
    return out


def hub_routes(conn, origin: str, client: ESIClient | None = None, offline: bool = False) -> dict[str, dict]:
    """Jumps and low/null-sec systems from `origin` to every other hub, shortest and high-sec-only."""
    systems = hub_systems(conn, client, offline)
    if origin not in systems:
        return {}
    return route_legs(conn, {d: (systems[origin], s) for d, s in systems.items() if d != origin}, client, offline)


def route_matrix(conn, client: ESIClient | None = None, offline: bool = False) -> dict[str, dict[str, dict]]:
    """Legs between every ordered pair of hubs: {buy hub: {sell hub: leg}}."""
    systems = hub_systems(conn, client, offline)
    legs = route_legs(conn, {(a, b): (systems[a], systems[b]) for a in systems for b in systems if a != b},
                      client, offline)
    out: dict[str, dict] = {a: {} for a in systems}
    for (a, b), leg in legs.items():
        out[a][b] = leg
    return out


def start_legs(conn, system_id: int, client: ESIClient | None = None, offline: bool = False) -> dict:
    """The empty first leg from the hauler's current system to every hub, and the nearest hub
    (fewest jumps, counted as in leg_jumps)."""
    systems = hub_systems(conn, client, offline)
    legs = route_legs(conn, {h: (system_id, s) for h, s in systems.items()}, client, offline)
    info = _systems_info(conn, {system_id}, client, offline).get(system_id)
    if info:
        name, sec = info
    else:
        row = conn.execute("SELECT name FROM system_names WHERE system_id=?", (system_id,)).fetchone()
        name, sec = (row[0] if row else str(system_id)), None
    reach = {h: leg_jumps(leg) for h, leg in legs.items() if leg_jumps(leg) is not None}
    return {"system": {"id": system_id, "name": name, "security": sec}, "legs": legs,
            "nearest": min(reach, key=lambda h: (reach[h], list(MARKETS).index(h))) if reach else None}


# ---------------------------------------------------------------- solar system names

def system_names(conn, client: ESIClient | None = None, offline: bool = False) -> dict[int, str]:
    """Every known-space solar system (no wormholes), cached: {system_id: name}."""
    rows = {k: v for k, v in conn.execute("SELECT system_id, name FROM system_names") if k < 31000000}
    oldest = conn.execute("SELECT MIN(fetched_at), COUNT(*) FROM system_names WHERE system_id < 31000000").fetchone()
    if offline or (oldest[1] > 1000 and not _older_than(oldest[0], VOLUME_MAX_AGE_DAYS)):
        return rows
    try:
        client = client or ESIClient()
        ids = [s for s in client.system_ids() if s < 31000000]
        got = []
        for i in range(0, len(ids), 1000):
            got += [(n["id"], n["name"]) for n in client.names(ids[i:i + 1000])]
    except Exception as exc:
        log.warning("system list lookup failed: %s", exc)
        return rows
    stamp = _now()
    conn.executemany("INSERT OR REPLACE INTO system_names VALUES (?,?,?)", [(i, n, stamp) for i, n in got])
    conn.commit()
    return dict(got)


def lookup_system(conn, query, client: ESIClient | None = None, offline: bool = False) -> dict | None:
    """A solar system by name (any case) or ID: {"id", "name"}, or None if there is no such system.
    Checks the cached names first, then asks ESI's /universe/ids."""
    q = str(query).strip()
    if not q:
        return None
    if q.isdigit():
        row = (conn.execute("SELECT system_id, name FROM system_names WHERE system_id=?", (int(q),)).fetchone()
               or conn.execute("SELECT system_id, name FROM systems WHERE system_id=?", (int(q),)).fetchone())
        return {"id": row[0], "name": row[1]} if row else None
    row = conn.execute("SELECT system_id, name FROM system_names WHERE name = ? COLLATE NOCASE", (q,)).fetchone()
    if row:
        return {"id": row[0], "name": row[1]}
    if offline:
        return None
    try:
        found = (client or ESIClient()).ids([q]).get("systems") or []
    except Exception as exc:
        log.warning("system lookup %r failed: %s", q, exc)
        return None
    if not found:
        return None
    s = found[0]
    conn.execute("INSERT OR REPLACE INTO system_names VALUES (?,?,?)", (s["id"], s["name"], _now()))
    conn.commit()
    return {"id": s["id"], "name": s["name"]}


def cached_start_systems(conn) -> list[int]:
    """Systems whose routes to every hub (both flags) are already cached; a static export includes these."""
    hubs = set(dict(conn.execute("SELECT station_id, system_id FROM station_systems")).values())
    if not hubs:
        return []
    have: dict[int, set] = {}
    for o, d, f in conn.execute("SELECT origin_system, destination_system, flag FROM routes"):
        if d in hubs:
            have.setdefault(o, set()).add((d, f))
    return sorted(o for o, got in have.items() if len(got) >= 2 * len(hubs - {o}))


def lane_systems(conn) -> list[int]:
    """Every system on a cached route between two hubs (shortest or high-sec): the trade lanes."""
    hubs = set(dict(conn.execute("SELECT station_id, system_id FROM station_systems")).values())
    return sorted({s for o, d, r in conn.execute("SELECT origin_system, destination_system, systems FROM routes")
                   if o in hubs and d in hubs for s in json.loads(r)})


def warm_start_routes(conn, system_ids: list[int], client: ESIClient | None = None) -> int:
    """Cache the routes from each system to every hub, so the static site can offer them as starts."""
    systems = hub_systems(conn, client)
    pairs = [(s, h) for s in system_ids for h in systems.values() if s != h]
    for i in range(0, len(pairs), 200):
        _prefetch_routes(conn, pairs[i:i + 200], client, offline=False)
    route_legs(conn, {i: p for i, p in enumerate(pairs)}, client)   # also caches system security
    return len(pairs)


# ---------------------------------------------------------------- inputs from the database

def latest_books(conn, type_ids: list[int]) -> dict[tuple[int, int], dict]:
    """The latest raw-order snapshot of each (station, type): asks lowest first, bids highest first."""
    if not type_ids:
        return {}
    marks = ",".join("?" * len(type_ids))
    df = pd.read_sql_query(
        f"SELECT r.location_id, r.type_id, r.taken_at, r.is_buy, r.price, r.volume_remain FROM raw_orders r "
        f"JOIN (SELECT location_id, type_id, MAX(taken_at) t FROM raw_orders WHERE type_id IN ({marks}) "
        f"GROUP BY location_id, type_id) m ON r.location_id = m.location_id AND r.type_id = m.type_id "
        f"AND r.taken_at = m.t", conn, params=type_ids)
    newest = df["taken_at"].max() if len(df) else None
    out = {}
    for (loc, tid), g in df.groupby(["location_id", "type_id"]):
        asks = g[g.is_buy == 0].groupby("price")["volume_remain"].sum().sort_index()
        bids = g[g.is_buy == 1].groupby("price")["volume_remain"].sum().sort_index(ascending=False)
        taken = g["taken_at"].iloc[0]
        stale = (pd.Timestamp(newest) - pd.Timestamp(taken)) > pd.Timedelta(hours=STALE_HOURS)
        out[(int(loc), int(tid))] = {"taken_at": taken, "stale": bool(stale),
                                     "asks": [[float(p), int(v)] for p, v in asks.items()],
                                     "bids": [[float(p), int(v)] for p, v in bids.items()]}
    return out


def daily_volumes(conn, type_ids: list[int], names: dict[int, str]) -> dict[tuple[str, int], tuple[float, float]]:
    """(hub, type) -> (median daily units traded, latest daily average price) in the hub's region,
    over the last 30 days of history."""
    if not type_ids:
        return {}
    marks = ",".join("?" * len(type_ids))
    df = pd.read_sql_query(
        f"SELECT region_id, type_id, date, average, volume FROM history WHERE type_id IN ({marks}) "
        f"AND date >= (SELECT date(MAX(date), '-{DAILY_VOLUME_DAYS} days') FROM history)", conn, params=type_ids)
    out = {}
    for (region, tid), g in df.groupby(["region_id", "type_id"]):
        g = g.sort_values("date").dropna(subset=["average", "volume"])
        if g.empty:
            continue
        stat = (float(g["volume"].median()), float(g["average"].iloc[-1]))
        override = REGION_OVERRIDES.get(names.get(int(tid), ""))
        for hub, m in MARKETS.items():
            if region == (override or m["region_id"]):
                out[(hub, int(tid))] = stat
    return out


def build_market(conn, items: dict[str, int], client: ESIClient | None = None, offline: bool = False) -> dict:
    """Everything the profit math needs for `items` ({name: type_id}) at every hub: order books,
    volumes, daily trade and the legs between every pair of hubs (`route_matrix`).
    `offline` uses only what's cached in the database (no ESI calls)."""
    type_ids = list(items.values())
    names = {t: n for n, t in items.items()}
    books = latest_books(conn, type_ids)
    offline = offline or not books  # nothing collected yet: don't go to ESI for volumes and routes
    vols = item_volumes(conn, sorted({t for (_, t) in books}), client, offline)
    dv = daily_volumes(conn, type_ids, names)
    out_items = []
    for name, tid in items.items():
        out_items.append({
            "type_id": tid, "name": name, "group": ITEM_GROUPS.get(name, ""),
            "volume_m3": vols.get(tid),
            "global_market": name in REGION_OVERRIDES,
            "books": {hub: books.get((m["station_id"], tid)) for hub, m in MARKETS.items()},
            "daily_volume": {hub: dv[(hub, tid)][0] for hub in MARKETS if (hub, tid) in dv},
            "ref_price": {hub: dv[(hub, tid)][1] for hub in MARKETS if (hub, tid) in dv},
        })
    taken = [b["taken_at"] for b in books.values()]
    return {"taken_at": max(taken) if taken else None,
            "hubs": {h: {"station": m["station"], "security": m["security"]} for h, m in MARKETS.items()},
            "route_matrix": route_matrix(conn, client, offline),
            "defaults": page_defaults(),
            "items": out_items}


def origin_view(market: dict, origin: str) -> dict:
    """The market seen from one buy hub: adds `origin` and its `routes` (to every other hub)."""
    if origin not in MARKETS:
        raise ValueError(f"unknown hub {origin}")
    return {**market, "origin": origin, "routes": (market.get("route_matrix") or {}).get(origin, {})}


def build_inputs(conn, origin: str, items: dict[str, int], client: ESIClient | None = None,
                 offline: bool = False) -> dict:
    """build_market seen from `origin` (see origin_view)."""
    if origin not in MARKETS:
        raise ValueError(f"unknown hub {origin}")
    return origin_view(build_market(conn, items, client, offline), origin)


def print_best_runs(runs: list[dict]) -> None:
    for r in runs:
        jumps = r.get("jumps_secure") if r.get("jumps_secure") is not None else r.get("jumps")
        print(f"{r['name'][:30]:30} {r.get('origin') or '':8} -> {r['destination']:8} {r['filled']:>10,.0f} units  "
              f"net {r['net_profit'] / 1e6:>9,.1f}M  ROI {r['roi'] * 100:5.1f}%  "
              f"{'?' if jumps is None else jumps:>3} jumps  {', '.join(r['flags'])}")
