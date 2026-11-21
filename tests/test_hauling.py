"""Hauler: profit math, ranking, missing data, ESI route/volume caching, API and static export."""
import json

import pytest

from eve_market import hauling, storage

FEES0 = hauling.fee_rates(0, 0)


def _item(books, name="Widget", type_id=1, volume=2.0, daily=None, ref=None, global_market=False):
    return {"type_id": type_id, "name": name, "group": "", "volume_m3": volume, "global_market": global_market,
            "books": books, "daily_volume": daily or {}, "ref_price": ref or {}}


def _book(asks=(), bids=(), stale=False):
    return {"taken_at": "2026-01-01T00:00:00Z", "stale": stale, "asks": [list(a) for a in asks],
            "bids": [list(b) for b in bids]}


def _inputs(items, hubs=("A", "B", "C"), routes=None):
    return {"origin": hubs[0], "taken_at": "t", "hubs": {h: {} for h in hubs}, "routes": routes or {},
            "defaults": hauling.page_defaults(), "items": items}


def test_walk_fills_through_levels():
    asks = [[10.0, 5], [11.0, 5], [20.0, 100]]
    assert hauling.walk(asks, 7) == (7, 5 * 10 + 2 * 11)
    assert hauling.walk(asks, 500) == (110, 50 + 55 + 2000)      # runs out of book
    assert hauling.depth(asks) == 110


def test_fees_with_skills():
    assert FEES0 == {"sales_tax": 0.075, "broker_fee": 0.03}
    f = hauling.fee_rates(5, 5)
    assert f["sales_tax"] == pytest.approx(0.075 * 0.45) and f["broker_fee"] == pytest.approx(0.015)
    f = hauling.fee_rates(3, 2)
    assert f["sales_tax"] == pytest.approx(0.075 * 0.67) and f["broker_fee"] == pytest.approx(0.024)
    assert hauling.fee_rates(9, -1) == hauling.fee_rates(5, 0)   # clamped to 0-5


def test_price_ticks_keep_four_significant_digits():
    assert hauling.undercut(1234.0) == 1233.0
    assert hauling.undercut(4_800_000.0) == 4_799_000.0
    assert hauling.undercut(1000.0) == 999.9
    assert hauling.undercut(5.67) == 5.66
    assert hauling.overbid(1234.0) == 1235.0 and hauling.overbid(5.67) == 5.68


def test_instant_profit_walks_both_books():
    item = _item({"A": _book(asks=[(100, 3), (110, 10)]), "B": _book(asks=[(200, 1)], bids=[(150, 4), (140, 10)])},
                 daily={"B": 50})
    r = hauling.evaluate(item, "A", "B", 5, FEES0, route={"jumps": 4, "jumps_secure": 5})
    cost = 3 * 100 + 2 * 110
    rev = 4 * 150 + 1 * 140
    assert r["filled"] == 5 and r["cost"] == cost and r["revenue"] == rev
    assert r["net_profit"] == pytest.approx(rev * (1 - 0.075) - cost)
    assert r["broker"] == 0                                       # no broker fee on instant trades
    assert r["roi"] == pytest.approx(r["net_profit"] / cost)
    assert r["m3"] == 10 and r["profit_per_m3"] == pytest.approx(r["net_profit"] / 10)
    assert r["profit_per_jump"] == pytest.approx(r["net_profit"] / 5)  # high-sec route
    assert r["days_to_sell"] == 0 and r["risk"] is None
    # 100->150, 100->150, 100->150, 110->150, then 110 vs 140*0.925=129.5 still pays: all 14 bid units pay
    assert r["max_profitable"] == 13                              # limited by the 13 ask units


def test_partial_fill_is_flagged():
    item = _item({"A": _book(asks=[(10, 2)]), "B": _book(bids=[(20, 100)])})
    r = hauling.evaluate(item, "A", "B", 10, FEES0)
    assert r["filled"] == 2 and any("only 2 of 10" in f for f in r["flags"])


def test_patient_lists_under_lowest_ask_and_pays_broker():
    item = _item({"A": _book(asks=[(100, 50)]), "B": _book(asks=[(150, 5)], bids=[(90, 5)])}, daily={"B": 4},
                 ref={"B": 140})
    fees = hauling.fee_rates(5, 5)
    r = hauling.evaluate(item, "A", "B", 10, fees, mode="patient")
    price = hauling.undercut(150)
    assert r["filled"] == 10 and r["sell_price"] == price           # not limited by the destination's book
    assert r["net_profit"] == pytest.approx(10 * price * (1 - fees["sales_tax"] - fees["broker_fee"]) - 1000)
    assert r["days_to_sell"] == pytest.approx(2.5)
    # A silly listing far above what trades is priced at the daily average instead.
    item["books"]["B"]["asks"] = [[1000, 1]]
    r = hauling.evaluate(item, "A", "B", 10, fees, mode="patient")
    assert r["sell_price"] == 140 and any("daily average" in f for f in r["flags"])


def test_buy_order_mode_pays_broker_on_purchase():
    item = _item({"A": _book(asks=[(100, 5)], bids=[(90, 5)]), "B": _book(bids=[(120, 10)])})
    r = hauling.evaluate(item, "A", "B", 10, FEES0, buy_mode="order")
    assert r["buy_price"] == hauling.overbid(90) == 90.01
    assert r["cost"] == pytest.approx(10 * 90.01 * 1.03) and r["broker"] == pytest.approx(10 * 90.01 * 0.03)
    assert r["filled"] == 10                                       # buy orders aren't limited by the ask book


def test_missing_data_is_flagged_not_raised():
    good = _book(asks=[(10, 5)], bids=[(9, 5)])
    cases = {
        "no order snapshot at B": _item({"A": good}),
        "no buy orders at B": _item({"A": good, "B": _book(asks=[(12, 1)])}),
        "no sell orders at A": _item({"A": _book(bids=[(9, 1)]), "B": good}),
        "nothing to haul": _item({"A": good, "B": good}, global_market=True),
    }
    for flag, item in cases.items():
        r = hauling.evaluate(item, "A", "B", 3, FEES0)
        assert r["net_profit"] is None and any(flag in f for f in r["flags"]), flag
    r = hauling.evaluate(_item({"A": good, "B": _book(bids=[(20, 9)], stale=True)}, volume=None), "A", "B", 3, FEES0)
    assert r["net_profit"] is not None and r["m3"] is None
    assert {"item volume unknown", "route unknown"} <= set(r["flags"]) and any("old" in f for f in r["flags"])


def test_ranking_auto_qty_and_cargo_cap():
    item = _item({"A": _book(asks=[(100, 1000)]), "B": _book(bids=[(130, 1000)]), "C": _book(bids=[(120, 1000)])},
                 volume=10)
    inp = _inputs([item])
    rows = hauling.rank_destinations(inp, item, qty=5)
    assert [r["destination"] for r in rows] == ["B", "C"] and rows[0]["net_profit"] > rows[1]["net_profit"] > 0
    assert hauling.auto_qty(item, "A", budget=25_050) == 250
    assert hauling.rank_destinations(inp, item, None, budget=25_050)[0]["filled"] == 250
    assert hauling.rank_destinations(inp, item, qty=500, cargo_m3=95)[0]["filled"] == 9
    assert any("cargo" in f for f in hauling.rank_destinations(inp, item, qty=5, cargo_m3=5)[0]["flags"])
    # A destination with no data sorts after the priced ones.
    del item["books"]["C"]
    assert hauling.rank_destinations(inp, item, qty=5)[-1]["net_profit"] is None


def test_patient_auto_qty_is_about_a_week_of_volume():
    item = _item({"A": _book(asks=[(100, 10_000)]), "B": _book(asks=[(150, 1)])}, daily={"B": 10})
    rows = hauling.rank_destinations(_inputs([item], hubs=("A", "B")), item, None, mode="patient", budget=1e9)
    assert rows[0]["filled"] == 10 * hauling.AUTO_SELL_DAYS


def test_best_runs_and_basket():
    a = _item({"A": _book(asks=[(100, 10)]), "B": _book(bids=[(200, 10)]), "C": _book(bids=[(120, 10)])}, "a", 1)
    b = _item({"A": _book(asks=[(100, 10)]), "B": _book(bids=[(50, 10)]), "C": _book(bids=[(300, 10)])}, "b", 2)
    inp = _inputs([a, b])
    runs = hauling.best_runs(inp, budget=1e9)
    assert [(r["name"], r["destination"]) for r in runs] == [("b", "C"), ("a", "B"), ("a", "C")]  # losers left out
    totals = hauling.basket_totals([hauling.rank_destinations(inp, i, qty=10) for i in (a, b)])
    assert totals[0]["destination"] == "C" and totals[0]["items"] == 2
    assert totals[0]["net_profit"] == pytest.approx(10 * 120 * 0.925 - 1000 + 10 * 300 * 0.925 - 1000)


START = 30000144


class FakeESI:
    def __init__(self):
        self.calls = []
        self.fail = False

    def route(self, o, d, flag):
        self.calls.append(("route", o, d, flag))
        if self.fail:
            raise ConnectionError("down")
        if o == START:  # the test starting system: one jump to Jita, more to the rest
            hops = {60003: [], 60008: [11, 12]}.get(d, [11])
            return [o, *hops, d] if flag == "shortest" else [o, *hops, 98, d]
        return [o, 99, d] if flag == "shortest" else [o, 98, 97, d]

    def system_ids(self):
        self.calls.append(("system_ids",))
        return [START, 30000142, 31000005]           # the wormhole is left out

    def names(self, ids):
        self.calls.append(("names", len(ids)))
        return [{"id": i, "name": {START: "Perimeter", 30000142: "Jita"}[i], "category": "solar_system"} for i in ids]

    def ids(self, names):
        self.calls.append(("ids", tuple(names)))
        return {"systems": [{"id": 30002187, "name": "Amarr"}]} if names[0].lower() == "amarr" else {}

    def system(self, s):
        self.calls.append(("system", s))
        return {"name": f"S{s}", "security_status": {99: 0.2, 98: 0.9, 97: -0.3}.get(s, 1.0)}

    def station(self, sid):
        self.calls.append(("station", sid))
        return {"system_id": sid // 1000}

    def type_info(self, t):
        self.calls.append(("type", t))
        return {"volume": 50.0, "packaged_volume": 5.0}


def test_route_cache_and_security(tmp_path):
    conn = storage.connect(tmp_path / "r.db")
    esi = FakeESI()
    routes = hauling.hub_routes(conn, "Jita", esi)
    assert set(routes) == {"Amarr", "Dodixie", "Hek", "Rens", "Zarzakh"}
    r = routes["Amarr"]
    assert r["jumps"] == 2 and r["low"] == 1 and r["null"] == 0
    assert r["jumps_secure"] == 3 and r["low_secure"] == 0 and r["null_secure"] == 1
    assert [s["name"] for s in r["systems"]][1] == "S99" and r["systems"][1]["security"] == 0.2
    n = len(esi.calls)
    assert len([c for c in esi.calls if c[0] == "route"]) == 10
    hauling.hub_routes(conn, "Jita", esi)                       # all cached now
    assert len(esi.calls) == n
    # An old cached route is refreshed, and kept if ESI is down.
    conn.execute("UPDATE routes SET fetched_at = '2020-01-01T00:00:00Z'")
    esi.fail = True
    again = hauling.hub_routes(conn, "Jita", esi)
    assert again["Amarr"]["jumps"] == 2 and len([c for c in esi.calls if c[0] == "route"]) == 20
    assert hauling.hub_routes(storage.connect(tmp_path / "empty.db"), "Jita", esi, offline=True) == {}


def test_item_volume_is_cached_packaged(tmp_path):
    conn = storage.connect(tmp_path / "v.db")
    esi = FakeESI()
    assert hauling.item_volumes(conn, [1, 2], esi) == {1: 5.0, 2: 5.0}
    assert hauling.item_volumes(conn, [1, 2], esi) == {1: 5.0, 2: 5.0}
    assert len([c for c in esi.calls if c[0] == "type"]) == 2


def _hauling_db(path):
    """Raw-order snapshots at Jita and Amarr for two tracked items, plus cached volumes and routes."""
    conn = storage.connect(path)
    from eve_market.config import MARKETS
    t = "2026-01-01T00:00:00Z"
    jita, amarr = MARKETS["Jita"]["station_id"], MARKETS["Amarr"]["station_id"]
    orders = {(jita, 34): [(1, 0, 4.0, 1000), (2, 1, 3.5, 1000)], (amarr, 34): [(3, 0, 6.0, 500), (4, 1, 5.0, 800)],
              (jita, 44992): [(5, 0, 5e6, 10)], (amarr, 44992): [(6, 1, 6e6, 10)]}
    for (loc, tid), rows in orders.items():
        storage.insert_raw_orders(conn, t, loc, tid, [{"order_id": o, "is_buy_order": b, "price": p, "volume_remain": v}
                                                      for o, b, p, v in rows])
    storage.upsert_history(conn, 10000043, 34, [{"date": "2026-01-01", "average": 5.5, "highest": 6, "lowest": 5,
                                                 "volume": 2000, "order_count": 9}])
    conn.close()


def test_hauling_api_and_static_export(tmp_path):
    from eve_market.dashboard import create_app
    from eve_market.site import build_site
    _hauling_db(tmp_path / "h.db")
    client = create_app(tmp_path / "h.db", offline=True).test_client()
    assert client.get("/hauler").status_code == 200
    assert "Hauler" in client.get("/about").get_data(as_text=True)
    res = client.get("/api/hauling?origin=Jita&items=Tritanium,PLEX&qty=100&accounting=5").get_json()
    trit = res["items"][0]["destinations"]
    assert trit[0]["destination"] == "Amarr" and trit[0]["filled"] == 100
    assert trit[0]["net_profit"] == pytest.approx(100 * 5.0 * (1 - 0.075 * 0.45) - 400)
    assert trit[0]["daily_volume"] == 2000
    assert any("no order snapshot" in f for f in trit[-1]["flags"])
    assert any("global" in f for f in res["items"][1]["destinations"][0]["flags"])
    assert res["basket"][0]["destination"] == "Amarr"
    assert client.get("/api/hauling?origin=Jita&items=Nope").status_code == 400
    assert client.get("/api/hauling?origin=Nowhere&items=34").status_code == 400
    best = client.get("/api/hauling/best?origin=Jita&mode=patient").get_json()
    assert best["runs"] and best["runs"][0]["name"] == "Tritanium"

    out = build_site(tmp_path / "site", tmp_path / "h.db", markets=["Jita"], offline=True)
    assert (out / "hauler.html").exists() and (out / "static/hauler.js").exists()
    assert 'href="hauler.html"' in (out / "index.html").read_text(encoding="utf-8")
    market = json.loads((out / "data/hauling/market/all.json").read_text())
    assert any(i["name"] == "Tritanium" and i["books"]["Amarr"] for i in market["items"])
    assert json.loads((out / "data/hauling/systems/all.json").read_text())["complete"] is False


def _matrix_inputs():
    """Two items over three hubs with a hand-made route matrix and a starting system."""
    a = _item({"A": _book(asks=[(100, 10)]), "B": _book(bids=[(200, 10)]), "C": _book(asks=[(90, 10)], bids=[(150, 10)])}, "a", 1)
    b = _item({"A": _book(asks=[(100, 10)]), "B": _book(asks=[(100, 10)], bids=[(50, 10)]), "C": _book(bids=[(300, 10)])}, "b", 2)

    def leg(j, js=None, low=0):
        return {"jumps": j, "jumps_secure": js, "low": low, "null": 0, "low_secure": 0 if js else None,
                "null_secure": 0 if js else None, "risk": None}
    matrix = {"A": {"B": leg(10, 12), "C": leg(4, None, 2)}, "B": {"A": leg(10, 12), "C": leg(6, 6)},
              "C": {"A": leg(4), "B": leg(6, 6)}}
    market = {"taken_at": "t", "hubs": {h: {} for h in "ABC"}, "route_matrix": matrix,
              "defaults": hauling.page_defaults(), "items": [a, b]}
    start = {"system": {"id": 1, "name": "Here", "security": 0.9}, "nearest": "C",
             "legs": {"A": leg(20, 20), "B": leg(30, 30), "C": leg(2, 2)}}
    return market, start, a, b


def test_two_leg_trips_rank_by_profit_per_total_jump():
    market, start, a, b = _matrix_inputs()
    rows = hauling.rank_trips(market, a, qty=10, start=start)
    best = rows[0]
    # C -> B: buy at 90, sell at 200: 10 * 200 * 0.925 - 900 = 950 net, over 2 empty + 6 loaded jumps
    assert (best["origin"], best["destination"]) == ("C", "B")
    assert best["start_jumps"] == 2 and best["loaded_jumps"] == 6 and best["total_jumps"] == 8
    assert best["net_profit"] == pytest.approx(950) and best["profit_per_total_jump"] == pytest.approx(950 / 8)
    assert best["profit_per_jump"] == pytest.approx(950 / 6) and best["start"]["risk"] is None and best["risk"] is None
    # A -> B: 10 * 200 * 0.925 - 1000 = 850, but A is 20 jumps away and the haul is 12 high-sec jumps
    ab = next(r for r in rows if (r["origin"], r["destination"]) == ("A", "B"))
    assert ab["total_jumps"] == 32 and ab["profit_per_total_jump"] == pytest.approx(850 / 32)
    # A -> C has no high-sec route, so the shortest (4 jumps, 2 low-sec) counts
    ac = next(r for r in rows if (r["origin"], r["destination"]) == ("A", "C"))
    assert ac["loaded_jumps"] == 4 and ac["low"] == 2
    by_profit = hauling.rank_trips(market, b, qty=10, start=start, sort="net_profit")
    assert [(r["origin"], r["destination"]) for r in by_profit[:2]] == [("A", "C"), ("B", "C")]
    # Without a start, trips rank by total profit and the total jumps are the loaded ones.
    plain = hauling.rank_trips(market, a, qty=10)
    assert plain[0]["start_jumps"] is None and plain[0]["total_jumps"] == plain[0]["loaded_jumps"]
    assert plain[0]["net_profit"] == max(r["net_profit"] or 0 for r in plain)
    # A buy hub the start has no route to is flagged and gets no total.
    del start["legs"]["B"]
    ba = next(r for r in hauling.rank_trips(market, b, qty=10, start=start) if r["origin"] == "B")
    assert ba["total_jumps"] is None and any("Here to B unknown" in f for f in ba["flags"])


def test_arbitrage_filters_and_sorts():
    market, start, a, b = _matrix_inputs()
    rows = hauling.arbitrage(market, qty=10)
    pairs = [(r["name"], r["origin"], r["destination"]) for r in rows]
    assert pairs[0] == ("b", "A", "C") and all(r["net_profit"] > 0 for r in rows)
    assert ("b", "A", "B") not in pairs                              # loses money, left out
    assert {r["destination"] for r in hauling.arbitrage(market, qty=10, sell_hub="B")} == {"B"}
    assert {r["origin"] for r in hauling.arbitrage(market, qty=10, buy_hub="C")} == {"C"}
    assert all(r["net_profit"] >= 1500 for r in hauling.arbitrage(market, qty=10, min_profit=1500))
    assert all(r["roi"] >= 1.0 for r in hauling.arbitrage(market, qty=10, min_roi=1.0))
    by_roi = hauling.arbitrage(market, qty=10, sort="roi")
    assert [r["roi"] for r in by_roi] == sorted((r["roi"] for r in by_roi), reverse=True)
    near = hauling.arbitrage(market, qty=10, start=start, sort="profit_per_total_jump")
    assert near[0]["origin"] == "C" and near[0]["start_jumps"] == 2
    assert len(hauling.arbitrage(market, qty=10, top=2)) == 2


def test_system_lookup_and_start_legs(tmp_path):
    conn = storage.connect(tmp_path / "s.db")
    esi = FakeESI()
    assert hauling.system_names(conn, esi) == {START: "Perimeter", 30000142: "Jita"}
    assert hauling.system_names(storage.connect(tmp_path / "s.db"), offline=True) == {START: "Perimeter", 30000142: "Jita"}
    assert hauling.lookup_system(conn, "perimeter", esi) == {"id": START, "name": "Perimeter"}
    assert hauling.lookup_system(conn, str(START)) == {"id": START, "name": "Perimeter"}
    n = len(esi.calls)
    assert hauling.lookup_system(conn, "AMARR", esi) == {"id": 30002187, "name": "Amarr"}   # not cached: ESI ids
    assert hauling.lookup_system(conn, "amarr", esi)["id"] == 30002187 and len(esi.calls) == n + 1  # now cached
    assert hauling.lookup_system(conn, "Nowhere", esi) is None
    assert hauling.lookup_system(conn, "Nowhere", offline=True) is None
    st = hauling.start_legs(conn, START, esi)
    assert st["nearest"] == "Jita" and st["system"]["id"] == START
    assert st["legs"]["Jita"]["jumps"] == 1 and st["legs"]["Jita"]["jumps_secure"] == 2
    assert st["legs"]["Amarr"]["jumps"] == 3 and st["legs"]["Amarr"]["risk"] is None
    assert START in hauling.cached_start_systems(conn)
    calls = len(esi.calls)
    hauling.start_legs(conn, START, esi)                              # all cached now
    assert len(esi.calls) == calls


def test_trip_and_arbitrage_api(tmp_path):
    from eve_market.dashboard import create_app
    from eve_market.site import build_site
    _hauling_db(tmp_path / "h.db")
    conn = storage.connect(tmp_path / "h.db")
    esi = FakeESI()
    hauling.route_matrix(conn, esi)
    hauling.system_names(conn, esi)
    hauling.start_legs(conn, START, esi)
    conn.close()
    client = create_app(tmp_path / "h.db", offline=True).test_client()
    st = client.get("/api/hauling/start?system=Perimeter").get_json()
    assert st["nearest"] == "Jita" and st["legs"]["Amarr"]["jumps"] == 3
    assert client.get("/api/hauling/start?system=Nowhere").status_code == 404
    assert [s[1] for s in client.get("/api/hauling/systems").get_json()["systems"]] == ["Jita", "Perimeter"]
    trips = client.get("/api/hauling/trips?items=Tritanium&system=Perimeter&mode=instant").get_json()
    best = trips["items"][0]["trips"][0]
    assert (best["origin"], best["destination"]) == ("Jita", "Amarr") and best["start_jumps"] == 2
    assert best["total_jumps"] == best["start_jumps"] + best["loaded_jumps"]
    arb = client.get("/api/hauling/arbitrage?mode=patient&min_roi=1&sort=roi").get_json()
    assert arb["trades"] and all(t["roi"] >= 0.01 for t in arb["trades"])
    assert client.get("/api/hauling/arbitrage?sort=nonsense").status_code == 400
    assert client.get("/api/hauling/arbitrage?buy=Nowhere").status_code == 400
    assert client.get("/api/hauling/market").get_json()["route_matrix"]["Jita"]["Amarr"]["jumps"] == 2
    # The static export carries the cached starting systems.
    out = build_site(tmp_path / "site", tmp_path / "h.db", markets=["Jita"], offline=True)
    systems = json.loads((out / "data/hauling/systems/all.json").read_text())
    assert [START, "Perimeter"] in systems["systems"] and systems["complete"] is False
    assert json.loads((out / f"data/hauling/start/{START}.json").read_text())["nearest"] == "Jita"
