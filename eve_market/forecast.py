"""Baseline price forecasting from daily market history.

Target: log return of the daily average price over the next `horizon` days.
One pooled model is trained across all items on per-item lag features, and
evaluated with a walk-forward backtest against naive baselines.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

LAGS = (1, 3, 7, 14, 30)


def _daily(g: pd.DataFrame) -> pd.DataFrame:
    """Reindex one item to a continuous daily calendar (no-trade days: carry price, zero volume)."""
    g = g.set_index("date").sort_index()
    idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
    g = g.reindex(idx)
    for col in ("average", "highest", "lowest"):
        g[col] = g[col].ffill()
    g[["volume", "order_count"]] = g[["volume", "order_count"]].fillna(0)
    g["type_id"] = g["type_id"].ffill().astype(int)
    g["name"] = g["name"].ffill() if "name" in g else None
    return g.rename_axis("date").reset_index()


def build_features(history: pd.DataFrame, horizon: int = 7) -> pd.DataFrame:
    frames = []
    for _, g in history.groupby("type_id"):
        if len(g) < 60:
            continue
        g = _daily(g)
        logp = np.log(g["average"])
        r1 = logp.diff()
        f = g[["date", "type_id", "name", "average", "volume"]].copy()
        for lag in LAGS:
            f[f"ret_{lag}"] = logp - logp.shift(lag)
        f["vol_7"] = r1.rolling(7).std()
        f["vol_30"] = r1.rolling(30).std()
        f["ma_ratio_7"] = logp - np.log(g["average"].rolling(7).mean())
        f["ma_ratio_30"] = logp - np.log(g["average"].rolling(30).mean())
        f["range_pct"] = (g["highest"] - g["lowest"]) / g["average"]
        lv = np.log1p(g["volume"])
        f["volume_z"] = (lv - lv.rolling(30).mean()) / (lv.rolling(30).std() + 1e-9)
        f["dow"] = g["date"].dt.day_of_week
        f["target"] = logp.shift(-horizon) - logp
        frames.append(f)
    if not frames:
        raise ValueError("Not enough history: each item needs at least 60 days. Run `collect` first.")
    return pd.concat(frames, ignore_index=True)


FEATURES = [f"ret_{l}" for l in LAGS] + ["vol_7", "vol_30", "ma_ratio_7", "ma_ratio_30",
                                         "range_pct", "volume_z", "dow"]


def make_models() -> dict:
    return {
        "ridge": make_pipeline(StandardScaler(), Ridge(alpha=10.0)),
        "gbm": HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                             min_samples_leaf=50, l2_regularization=1.0, random_state=0),
    }


def _metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    moved = np.abs(y) > 1e-6
    return {
        "mae": float(np.mean(np.abs(y - pred))),
        "direction_acc": float(np.mean(np.sign(pred[moved]) == np.sign(y[moved]))) if moved.any() else np.nan,
    }


def backtest(feats: pd.DataFrame, horizon: int = 7, n_folds: int = 5, test_days: int = 21) -> pd.DataFrame:
    """Walk-forward evaluation. Training rows end `horizon` days before each test window so
    no training target overlaps the test period."""
    data = feats.dropna(subset=FEATURES + ["target"])
    dates = np.sort(data["date"].unique())
    results = []
    for k in range(n_folds, 0, -1):
        test_start = dates[-k * test_days]
        test_end = test_start + np.timedelta64(test_days, "D")
        train = data[data["date"] < test_start - np.timedelta64(horizon, "D")]
        test = data[(data["date"] >= test_start) & (data["date"] < test_end)]
        if len(train) < 500 or test.empty:
            continue
        y = test["target"].to_numpy()
        preds = {
            "no_change": np.zeros_like(y),
            # momentum: assume the last `horizon` days' move continues
            "momentum": test[f"ret_{min(LAGS, key=lambda l: abs(l - horizon))}"].to_numpy(),
        }
        for name, model in make_models().items():
            model.fit(train[FEATURES], train["target"])
            preds[name] = model.predict(test[FEATURES])
        for name, p in preds.items():
            results.append({"fold_start": pd.Timestamp(test_start).date(), "model": name, **_metrics(y, p)})
    return pd.DataFrame(results)


def predict_latest(feats: pd.DataFrame, model_name: str = "gbm") -> pd.DataFrame:
    """Fit on all labeled rows, then forecast from each item's most recent day."""
    train = feats.dropna(subset=FEATURES + ["target"])
    model = make_models()[model_name]
    model.fit(train[FEATURES], train["target"])
    latest = feats.dropna(subset=FEATURES).sort_values("date").groupby("type_id").tail(1).copy()
    latest["pred_log_return"] = model.predict(latest[FEATURES])
    latest["pred_pct_change"] = np.expm1(latest["pred_log_return"]) * 100
    latest["pred_price"] = latest["average"] * np.exp(latest["pred_log_return"])
    cols = ["type_id", "name", "date", "average", "pred_price", "pred_pct_change", "vol_30"]
    return latest[cols].sort_values("pred_pct_change", ascending=False).reset_index(drop=True)
