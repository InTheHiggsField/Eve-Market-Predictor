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
    df = storage.read_history(conn, [34])
    assert len(df) == 1 and df.loc[0, "average"] == 5.0


def test_dashboard_api(tmp_path):
    from eve_market.dashboard import create_app
    hist = _synthetic_history(n_items=3)
    conn = storage.connect(tmp_path / "d.db")
    from data_sets.tracked_items import TRACKED_ITEMS
    ids = list(TRACKED_ITEMS.items())[:3]
    storage.save_type_names(conn, dict(ids))
    for (name, tid), (_, g) in zip(ids, hist.groupby("type_id")):
        rows = g.assign(date=g["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
        storage.upsert_history(conn, 10000002, tid, rows)
    conn.close()
    client = create_app(tmp_path / "d.db").test_client()
    for page in ("/", "/predictions", "/about"):
        assert client.get(page).status_code == 200
    items = client.get("/api/items?list=core").get_json()
    assert len(items) == 3 and all(i["price"] > 0 for i in items)
    assert client.get(f"/api/history/{ids[0][1]}").status_code == 200
    pred = client.get("/api/predictions?list=core&model=ridge&horizon=7").get_json()
    assert len(pred["items"]) == 3 and {r["model"] for r in pred["summary"]} == {"no_change", "momentum", "ridge", "gbm"}
