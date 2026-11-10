"""Local dashboard: Flask app serving three pages plus a JSON API over market.db.

    python -m eve_market dashboard          # then open http://127.0.0.1:5000
"""
from __future__ import annotations

import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, abort, jsonify, render_template, request

from .. import storage
from ..config import DEFAULT_MARKET, ITEM_GROUPS, ITEM_LISTS, MARKETS, REGION_OVERRIDES, load_history
from ..forecast import FEATURES, backtest, build_features, make_models

HERE = Path(__file__).resolve().parent
LIST_OF = {name: lst for lst in ("core", "commodities") for name in ITEM_LISTS[lst]}
_lock = threading.Lock()  # sklearn fits are CPU heavy; don't run two at once


def create_app(db_path: str | Path = storage.DEFAULT_DB) -> Flask:
    app = Flask(__name__, template_folder=str(HERE / "templates"), static_folder=str(HERE / "static"))

    def conn():
        return closing(storage.connect(db_path))

    def market_arg() -> str:
        m = request.args.get("market", DEFAULT_MARKET)
        if m not in MARKETS:
            abort(400, f"unknown market {m}")
        return m

    def history(list_name: str, market: str) -> pd.DataFrame:
        if list_name not in ITEM_LISTS:
            abort(400, f"unknown list {list_name}")
        with conn() as c:
            return load_history(c, list_name, market)

    def latest_snapshots(market: str) -> pd.DataFrame:
        """Most recent order snapshot of each item at this hub's station."""
        with conn() as c:
            return pd.read_sql_query(
                "SELECT s.* FROM order_snapshots s JOIN (SELECT type_id, MAX(taken_at) t FROM order_snapshots "
                "WHERE location_id = ? GROUP BY type_id) m ON s.type_id = m.type_id AND s.taken_at = m.t "
                "WHERE s.location_id = ?", c, params=(MARKETS[market]["station_id"],) * 2)

    # ---------- pages ----------
    @app.get("/")
    def market_page():
        return render_template("market.html", page="market", markets=MARKETS)

    @app.get("/predictions")
    def predictions_page():
        return render_template("predictions.html", page="predictions", markets=MARKETS)

    @app.get("/about")
    def about_page():
        return render_template("about.html", page="about", region_overrides=REGION_OVERRIDES, markets=MARKETS,
                               features=FEATURES, n_core=len(ITEM_LISTS["core"]),
                               n_commod=len(ITEM_LISTS["commodities"]))

    # ---------- API ----------
    @app.get("/api/items")
    def api_items():
        market = market_arg()
        h = history(request.args.get("list", "all"), market)
        snaps = latest_snapshots(market).set_index("type_id")
        out = []
        for type_id, g in h.groupby("type_id"):
            g = g.sort_values("date")
            p = g.set_index("date")["average"]
            last = p.iloc[-1]

            def chg(days):
                past = p[p.index <= p.index[-1] - pd.Timedelta(days=days)]
                return None if past.empty else float((last / past.iloc[-1] - 1) * 100)

            g30 = g[g["date"] > g["date"].iloc[-1] - pd.Timedelta(days=30)]
            name = g["name"].iloc[0]
            row = {
                "type_id": int(type_id), "name": name, "group": ITEM_GROUPS.get(name, ""),
                "list": LIST_OF.get(name, ""), "date": g["date"].iloc[-1].strftime("%Y-%m-%d"),
                "price": float(last), "chg_1d": chg(1), "chg_7d": chg(7), "chg_30d": chg(30),
                "volume_30d": float(g30["volume"].mean()),
                "isk_30d": float((g30["average"] * g30["volume"]).mean()),
                "orders_30d": float(g30["order_count"].mean()),
                "spark": [float(x) for x in p.tail(30)],
                "best_bid": None, "best_ask": None, "spread_pct": None, "snapshot_at": None,
            }
            if type_id in snaps.index:
                s = snaps.loc[type_id]
                bid, ask = s["best_bid"], s["best_ask"]
                row.update(best_bid=_num(bid), best_ask=_num(ask), snapshot_at=s["taken_at"],
                           spread_pct=_num((ask - bid) / ask * 100) if bid and ask else None)
            out.append(row)
        return jsonify(out)

    @app.get("/api/history/<int:type_id>")
    def api_history(type_id: int):
        h = history("all", market_arg())
        g = h[h["type_id"] == type_id].sort_values("date")
        if g.empty:
            abort(404)
        return jsonify({
            "type_id": type_id, "name": g["name"].iloc[0],
            "dates": g["date"].dt.strftime("%Y-%m-%d").tolist(),
            **{c: g[c].astype(float).tolist() for c in ("average", "highest", "lowest", "volume", "order_count")},
        })

    @app.get("/api/predictions")
    def api_predictions():
        market = market_arg()
        list_name = request.args.get("list", "core")
        model = request.args.get("model", "ridge")
        horizon = int(request.args.get("horizon", 7))
        if model not in make_models() or not 1 <= horizon <= 30 or list_name not in ITEM_LISTS:
            abort(400)
        with conn() as c:  # cache is keyed on the latest data date so a new collect invalidates it
            stamp = c.execute("SELECT MAX(date) || COUNT(*) FROM history").fetchone()[0]
        with _lock:
            return jsonify(_predictions(str(db_path), market, list_name, model, horizon, stamp))

    return app


def _num(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


@lru_cache(maxsize=32)
def _predictions(db_path: str, market: str, list_name: str, model_name: str, horizon: int, _stamp: str) -> dict:
    with closing(storage.connect(db_path)) as c:
        hist = load_history(c, list_name, market)
    feats = build_features(hist, horizon)
    metrics, oos = backtest(feats, horizon, return_predictions=True)
    summary = (metrics.groupby("model")[["mae", "direction_acc"]].mean()
               .reindex(["no_change", "momentum", "ridge", "gbm"]).reset_index())

    train = feats.dropna(subset=FEATURES + ["target"])
    model = make_models()[model_name].fit(train[FEATURES], train["target"])
    latest = feats.dropna(subset=FEATURES).sort_values("date").groupby("type_id").tail(1).copy()
    latest["pred"] = model.predict(latest[FEATURES])

    items = []
    for _, r in latest.sort_values("pred", ascending=False).iterrows():
        tid = int(r["type_id"])
        f = feats[feats["type_id"] == tid].sort_values("date").tail(120)
        o = oos[oos["type_id"] == tid].sort_values("date")
        target_dates = (o["date"] + pd.Timedelta(days=horizon)).dt.strftime("%Y-%m-%d")
        band = float(r["vol_30"] * np.sqrt(horizon))  # ~1 sigma of the horizon return
        items.append({
            "type_id": tid, "name": r["name"], "group": ITEM_GROUPS.get(r["name"], ""),
            "last_date": r["date"].strftime("%Y-%m-%d"), "price": float(r["average"]),
            "pred_pct": float(np.expm1(r["pred"]) * 100),
            "pred_price": float(r["average"] * np.exp(r["pred"])),
            "pred_low": float(r["average"] * np.exp(r["pred"] - band)),
            "pred_high": float(r["average"] * np.exp(r["pred"] + band)),
            "target_date": (r["date"] + pd.Timedelta(days=horizon)).strftime("%Y-%m-%d"),
            "history": {"dates": f["date"].dt.strftime("%Y-%m-%d").tolist(), "price": f["average"].tolist()},
            # Out-of-sample backtest: what the model predicted for each date, placed at the date it targeted.
            "backtest": {"dates": target_dates.tolist(),
                         "pred_price": (o["average"] * np.exp(o[model_name])).tolist(),
                         "hit": (np.sign(o[model_name]) == np.sign(o["target"])).astype(int).tolist()},
            "direction_acc": float(np.mean(np.sign(o[model_name]) == np.sign(o["target"]))) if len(o) else None,
        })
    return {
        "market": market, "list": list_name, "model": model_name, "horizon": horizon,
        "summary": summary.replace({np.nan: None}).to_dict("records"),
        "folds": [{**rec, "fold_start": str(rec["fold_start"])} for rec in metrics.to_dict("records")],
        "items": items,
    }


def run(host: str = "127.0.0.1", port: int = 5000, db_path=storage.DEFAULT_DB) -> None:
    create_app(db_path).run(host=host, port=port, debug=False)
