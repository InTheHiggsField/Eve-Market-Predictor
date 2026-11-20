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


class FakeESI:
    def __init__(self):
        self.calls = []
        self.fail = False

    def route(self, o, d, flag):
        self.calls.append(("route", o, d, flag))
        if self.fail:
            raise ConnectionError("down")
        return [o, 99, d] if flag == "shortest" else [o, 98, 97, d]

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
    inputs = json.loads((out / "data/hauling/inputs/Jita.json").read_text())
    assert inputs["origin"] == "Jita" and any(i["name"] == "Tritanium" and i["books"]["Amarr"] for i in inputs["items"])
