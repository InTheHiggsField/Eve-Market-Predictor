import numpy as np
import pandas as pd

from eve_market import storage
from eve_market.collect import summarize_orders
from eve_market.forecast import FEATURES, backtest, build_features, predict_latest


def test_summarize_orders_filters_station():
    orders = [
        {"location_id": 1, "is_buy_order": True, "price": 10, "volume_remain": 5},
        {"location_id": 1, "is_buy_order": True, "price": 12, "volume_remain": 1},
        {"location_id": 1, "is_buy_order": False, "price": 15, "volume_remain": 3},
        {"location_id": 2, "is_buy_order": False, "price": 13, "volume_remain": 9},
    ]
    s = summarize_orders(orders, location_id=1)
    assert s["best_bid"] == 12 and s["best_ask"] == 15
    assert s["bid_volume"] == 6 and s["ask_orders"] == 1


def _synthetic_history(n_items=4, n_days=300):
    rng = np.random.default_rng(0)
    rows = []
    dates = pd.date_range("2025-01-01", periods=n_days, freq="D")
    for t in range(n_items):
        price = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n_days)))
        for d, p in zip(dates, price):
            rows.append({"type_id": t, "name": f"item{t}", "date": d, "average": p,
                         "highest": p * 1.01, "lowest": p * 0.99, "volume": 1000, "order_count": 10})
    return pd.DataFrame(rows)


def test_features_have_no_lookahead_and_pipeline_runs(tmp_path):
    hist = _synthetic_history()
    feats = build_features(hist, horizon=7)
    one = feats[feats.type_id == 0].reset_index(drop=True)
    # target at t is the forward return; the last 7 rows can't be labeled
    assert one["target"].tail(7).isna().all()
    assert np.isclose(one.loc[40, "target"], np.log(one.loc[47, "average"] / one.loc[40, "average"]))
    assert np.isclose(one.loc[40, "ret_7"], np.log(one.loc[40, "average"] / one.loc[33, "average"]))

    res = backtest(feats, horizon=7, n_folds=2)
    assert set(res.model) == {"no_change", "momentum", "ridge", "gbm"}
    preds = predict_latest(feats, "ridge")
    assert len(preds) == 4 and preds["pred_price"].notna().all()


def test_storage_roundtrip(tmp_path):
    conn = storage.connect(tmp_path / "t.db")
    storage.upsert_history(conn, 1, 34, [{"date": "2026-01-01", "average": 4.0, "highest": 4.1,
                                          "lowest": 3.9, "volume": 10, "order_count": 2}])
    storage.upsert_history(conn, 1, 34, [{"date": "2026-01-01", "average": 5.0, "highest": 5.1,
                                          "lowest": 4.9, "volume": 10, "order_count": 2}])
    df = storage.read_history(conn, [34], 1)
    assert len(df) == 1 and df.loc[0, "average"] == 5.0


def test_dashboard_api(tmp_path):
    from eve_market.dashboard import create_app
    hist = _synthetic_history(n_items=3)
    hist["volume"] = 10 ** 8  # liquid enough to clear the health floor
    conn = storage.connect(tmp_path / "d.db")
    from data_sets.tracked_items import TRACKED_ITEMS
    ids = list(TRACKED_ITEMS.items())[:3]
    storage.save_type_names(conn, dict(ids))
    for (name, tid), (_, g) in zip(ids, hist.groupby("type_id")):
        rows = g.assign(date=g["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
        storage.upsert_history(conn, 10000002, tid, rows)
    conn.close()
    client = create_app(tmp_path / "d.db").test_client()
    for page in ("/", "/predictions", "/health", "/about"):
        assert client.get(page).status_code == 200
    items = client.get("/api/items?list=core").get_json()
    assert len(items) == 3 and all(i["price"] > 0 for i in items)
    assert client.get(f"/api/history/{ids[0][1]}").status_code == 200
    pred = client.get("/api/predictions?list=core&model=ridge&horizon=7").get_json()
    assert len(pred["items"]) == 3 and {r["model"] for r in pred["summary"]} == {"no_change", "momentum", "ridge", "gbm"}
    assert pred["excluded"] == []
    health = client.get("/api/health").get_json()
    assert len(health["items"]) == 3 and all(i["label"] in ("Healthy", "Thin", "Illiquid") for i in health["items"])


def test_backfill_trims_to_tracked_items():
    import bz2
    from eve_market.backfill import _wanted_key, fetch_and_trim

    csv_text = ("date,region_id,type_id,average,highest,lowest,volume,order_count\n"
                "2017-03-01,10000002,34,5.0,5.1,4.9,1000,10\n"       # kept: Jita mineral
                "2017-03-01,10000043,34,5.5,5.6,5.4,500,4\n"         # dropped: other region
                "2017-03-01,10000002,99999,1.0,1.0,1.0,1,1\n"        # dropped: untracked item
                "2017-03-01,19000001,44992,3e6,3e6,3e6,10,2\n")      # kept: PLEX global market

    class FakeResp:
        content = bz2.compress(csv_text.encode())
        def raise_for_status(self): pass

    class FakeSession:
        def get(self, url, timeout): return FakeResp()

    keep = _wanted_key({"Tritanium": 34, "PLEX": 44992}, {"PLEX": 19000001}, [10000002])
    rows = fetch_and_trim(FakeSession(), "x", keep)
    assert sorted((r[0], r[1]) for r in rows) == [(10000002, 34), (19000001, 44992)]


def test_read_history_prefers_higher_volume_region(tmp_path):
    conn = storage.connect(tmp_path / "p.db")
    base = {"date": "2025-01-01", "average": 1.0, "highest": 1.0, "lowest": 1.0, "order_count": 1}
    storage.upsert_history(conn, 10000002, 44992, [{**base, "volume": 5}])
    storage.upsert_history(conn, 19000001, 44992, [{**base, "average": 2.0, "volume": 50}])
    df = storage.read_history(conn, [44992], 10000002, (19000001,))
    assert len(df) == 1 and df.loc[0, "region_id"] == 19000001


def test_history_is_split_by_market(tmp_path):
    conn = storage.connect(tmp_path / "m.db")
    base = {"date": "2025-01-01", "highest": 1.0, "lowest": 1.0, "volume": 5, "order_count": 1}
    storage.upsert_history(conn, 10000002, 34, [{**base, "average": 4.0}])   # Jita
    storage.upsert_history(conn, 10000043, 34, [{**base, "average": 4.5}])   # Amarr
    storage.upsert_history(conn, 19000001, 44992, [{**base, "average": 5e6}])  # global PLEX
    amarr = storage.read_history(conn, [34, 44992], 10000043, (19000001,))
    assert sorted(amarr["average"]) == [4.5, 5e6]


def test_snapshot_migration_keeps_rows(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE order_snapshots (taken_at TEXT NOT NULL, region_id INTEGER NOT NULL, location_id INTEGER, "
              "type_id INTEGER NOT NULL, best_bid REAL, best_ask REAL, bid_volume INTEGER, ask_volume INTEGER, "
              "bid_orders INTEGER, ask_orders INTEGER, PRIMARY KEY (taken_at, region_id, type_id))")
    c.execute("INSERT INTO order_snapshots VALUES ('t', 1, 60003760, 34, 1, 2, 3, 4, 5, 6)")
    c.commit(); c.close()
    conn = storage.connect(db)
    storage.insert_snapshot(conn, {"taken_at": "t", "region_id": 1, "location_id": 60008494, "type_id": 34,
                                   "best_bid": 1, "best_ask": 2, "bid_volume": 0, "ask_volume": 0,
                                   "bid_orders": 0, "ask_orders": 0})
    assert conn.execute("SELECT COUNT(*) FROM order_snapshots").fetchone()[0] == 2


def test_auth_env_and_jwt_claims(tmp_path):
    import base64, json
    from eve_market import auth
    env = tmp_path / ".env"
    env.write_text("EVE_CLIENT_ID=abc\nEVE_CLIENT_SECRET=\n", encoding="utf-8")
    try:
        auth.load_env(env)
        assert False, "missing secret should raise"
    except auth.NotLoggedIn as exc:
        assert "EVE_CLIENT_SECRET" in str(exc)
    env.write_text("EVE_CLIENT_ID=abc\nEVE_CLIENT_SECRET=xyz\n", encoding="utf-8")
    assert auth.load_env(env)["EVE_CALLBACK_URL"].endswith("/callback")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "CHARACTER:EVE:123", "name": "Pilot"}).encode()).decode().rstrip("=")
    assert auth._claims(f"h.{payload}.s") == {"sub": "CHARACTER:EVE:123", "name": "Pilot"}


def test_security_bands():
    from eve_market.hubs import security_class
    assert security_class(0.46) == "high" and security_class(0.44) == "low"
    assert security_class(0.04) == "low" and security_class(-0.2) == "null"


def test_station_model_trains_on_enough_snapshots():
    from eve_market.forecast import build_station_features, predict_station, station_days, MIN_STATION_DAYS
    hist = _synthetic_history(n_items=4, n_days=300)
    feats = build_features(hist, horizon=7)
    rng = np.random.default_rng(1)
    rows = []
    for t in range(4):
        g = hist[hist.type_id == t].tail(80)
        for d, p in zip(g["date"], g["average"]):
            for hour in (0, 12):
                mid = p * 1.02 * np.exp(rng.normal(0, 0.005))
                rows.append({"taken_at": d + pd.Timedelta(hours=hour), "type_id": t, "best_bid": mid * 0.99,
                             "best_ask": mid * 1.01, "bid_top5": mid * 0.99, "ask_top5": mid * 1.01, "mid": mid})
    station = pd.DataFrame(rows)
    assert station_days(station) == 80 >= MIN_STATION_DAYS
    sf = build_station_features(feats, station, horizon=7)
    assert np.isclose(sf["premium"].dropna().median(), np.log(1.02), atol=0.01)
    metrics, latest = predict_station(sf, "ridge", horizon=7)
    assert len(latest) == 4 and {"ridge", "no_change"} <= set(metrics.model)
    assert predict_station(sf[sf.date > sf.date.max() - pd.Timedelta(days=20)], "ridge", 7) is None


def test_top_pct_price_ignores_bait_order():
    from eve_market.collect import _top_pct_price
    asks = [{"price": 9.0, "volume_remain": 1}, {"price": 10.0, "volume_remain": 1000}]
    assert 9.5 < _top_pct_price(asks, highest_first=False) <= 10.0  # best order alone would say 9.0


def test_top_pct_price_ignores_lowball_bids():
    from eve_market.collect import _top_pct_price
    bids = [{"price": 4.8e6, "volume_remain": 100}, {"price": 4.0e4, "volume_remain": 1_000_000}]
    assert _top_pct_price(bids, highest_first=True) == 4.8e6


def test_clean_log_price_clips_a_print_without_lookahead():
    from eve_market.forecast import clean_log_price
    logp = pd.Series(np.log(100.0), index=range(60)) + np.random.default_rng(0).normal(0, 0.01, 60)
    spiked = logp.copy()
    spiked[40] += np.log(5)  # one fat-finger day at 5x
    clean = clean_log_price(spiked)
    assert clean[40] < logp[40] + 0.35 and np.allclose(clean.drop(40), logp.drop(40))
    # causal: changing a later day never changes an earlier cleaned value
    assert np.allclose(clean_log_price(spiked.iloc[:41]), clean.iloc[:41])


def test_health_floor_drops_thin_items():
    from eve_market.forecast import apply_health_floor
    hist = _synthetic_history(n_items=3)
    hist.loc[hist.type_id == 2, "volume"] = 1  # item2 trades ~100 ISK a day
    feats = build_features(hist, horizon=7)
    kept, excluded = apply_health_floor(feats, min_isk=50_000)
    assert excluded == ["item2"] and set(kept.type_id) == {0, 1}
    assert apply_health_floor(feats, 0)[1] == []


def test_health_metrics_and_score():
    from eve_market import health
    hist = _synthetic_history(n_items=6, n_days=200)
    hist.loc[hist.type_id == 5, "volume"] = 1          # thin item
    m = health.history_metrics(hist)
    assert {"isk_turnover", "amihud", "roll_spread", "volatility", "bounce", "zero_days"} <= set(m.columns)
    assert 0.2 < m.loc[0, "volatility"] < 0.6          # 2% daily vol is about 38% a year
    snaps = pd.DataFrame({"type_id": [0, 0], "best_bid": [98, 99], "best_ask": [102, 101], "bid_top5": [98, 99],
                          "ask_top5": [102, 101], "bid_depth": [300.0, 100.0], "ask_depth": [100.0, 100.0]})
    s = health.snapshot_metrics(snaps)
    assert np.isclose(s.loc[0, "quoted_spread"], 0.03) and s.loc[0, "depth"] == 100 and s.loc[0, "imbalance"] == 0.25
    orders = pd.DataFrame({"taken_at": "2026-01-01T12:00:00Z", "type_id": 0, "order_id": [1, 2, 3, 4],
                           "is_buy": [0, 0, 0, 1], "price": [100, 101, 200, 90], "volume_remain": [9, 1, 50, 5],
                           "issued": ["2026-01-01T11:30:00Z", "2025-12-01T00:00:00Z", "2025-12-01T00:00:00Z",
                                      "2025-12-01T00:00:00Z"]})
    b = health.book_metrics(orders)   # the 200 ISK sell order is outside the 5% band
    assert b.loc[0, "top_share"] == 1.0 and np.isclose(b.loc[0, "churn"], 1 / 3)
    scored = health.score(m.reset_index().assign(market="x"), min_isk=50_000)
    thin = scored[scored.type_id == 5].iloc[0]
    assert thin["label"] == "Illiquid" and thin["score"] < scored["score"].max()


def test_collect_keeps_depth_and_raw_orders(tmp_path):
    from eve_market.collect import near_touch
    orders = [{"order_id": 1, "location_id": 1, "is_buy_order": True, "price": 100, "volume_remain": 2, "issued": "t"},
              {"order_id": 2, "location_id": 1, "is_buy_order": True, "price": 50, "volume_remain": 99, "issued": "t"},
              {"order_id": 3, "location_id": 1, "is_buy_order": False, "price": 110, "volume_remain": 3, "issued": "t"}]
    s = summarize_orders(orders, 1)
    assert s["bid_depth"] == 200 and s["ask_depth"] == 330   # the lowball bid is not depth
    assert [o["order_id"] for o in near_touch(orders, 0.2)] == [1, 3]
    conn = storage.connect(tmp_path / "r.db")
    storage.insert_raw_orders(conn, "2026-01-01T00:00:00Z", 1, 34, near_touch(orders, 0.2))
    assert len(storage.read_raw_orders(conn, 1)) == 2
    assert storage.prune_raw_orders(conn, 90) == 2           # long past the retention window
