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
DEFAULT_MIN_ISK = 1e9  # health floor: items trading less ISK than this per day are left out of the models


def clean_log_price(logp: pd.Series, window: int = 7, k: float = 6.0, min_band: float = 0.3) -> pd.Series:
    """Clip outlier prints (fat-finger or manipulated trades) out of a daily log-price series.

    Each day is limited to a band around the median of the previous `window` days. The band is
    `k` robust standard deviations wide and never narrower than `min_band` (30%). Only past days
    are used, so features built on the result have no lookahead. A real jump is delayed by a few
    days, until the trailing median catches up, rather than removed."""
    med = logp.shift(1).rolling(window, min_periods=3).median()
    dev = (logp - med).abs()
    scale = dev.shift(1).rolling(30, min_periods=5).median() * 1.4826
    band = np.maximum(k * scale.fillna(0), min_band)
    return logp.clip(lower=med - band, upper=med + band).where(med.notna(), logp)
"""This is where all of the forecasting models rest"""

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


def build_features(history: pd.DataFrame, horizon: int = 7) -> pd.DataFrame: #Horizon = Future, attempts to forecast a week in the future by default
    frames = []
    for _, g in history.groupby("type_id"):
        if len(g) < 60:
            continue
        g = _daily(g)
        logp = clean_log_price(np.log(g["average"]))
        r1 = logp.diff()
        f = g[["date", "type_id", "name", "average", "volume"]].copy()
        for lag in LAGS:
            f[f"ret_{lag}"] = logp - logp.shift(lag)
        f["vol_7"] = r1.rolling(7).std()
        f["vol_30"] = r1.rolling(30).std()
        f["ma_ratio_7"] = logp - logp.rolling(7).mean()
        f["ma_ratio_30"] = logp - logp.rolling(30).mean()
        f["range_pct"] = ((g["highest"] - g["lowest"]) / g["average"]).clip(upper=1.0)
        # Market-health features (see health.py), all trailing 30 days.
        isk = g["average"] * g["volume"]
        f["isk_30"] = isk.rolling(30).median()
        cov = r1.rolling(30).cov(r1.shift(1))
        f["roll_30"] = 2 * np.sqrt((-cov).clip(lower=0))          # Roll implied spread (bid-ask bounce)
        f["amihud_30"] = np.log1p(1e9 * r1.abs().rolling(30).sum() / isk.rolling(30).sum().replace(0, np.nan))
        f["zero_30"] = (g["volume"] == 0).rolling(30).mean()
        lv = np.log1p(g["volume"])
        f["volume_z"] = (lv - lv.rolling(30).mean()) / (lv.rolling(30).std() + 1e-9)
        f["dow"] = g["date"].dt.day_of_week
        f["target"] = logp.shift(-horizon) - logp
        frames.append(f)
    if not frames:
        raise ValueError("Not enough history: each item needs at least 60 days. Run `collect` first.")
    return pd.concat(frames, ignore_index=True)


FEATURES = [f"ret_{l}" for l in LAGS] + ["vol_7", "vol_30", "ma_ratio_7", "ma_ratio_30",
                                         "range_pct", "volume_z", "dow", "roll_30", "amihud_30", "zero_30"]


def apply_health_floor(feats: pd.DataFrame, min_isk: float = DEFAULT_MIN_ISK) -> tuple[pd.DataFrame, list[str]]:
    """Keep only liquid markets. Drops every day on which an item's median ISK traded over the
    previous 30 days was under `min_isk` (so the backtest never trains or scores on thin days),
    and drops items that are under the floor today. Returns the kept rows and the names of
    the items left out."""
    if not min_isk:
        return feats, []
    latest = feats.sort_values("date").groupby("type_id").tail(1)
    out = latest[~(latest["isk_30"] >= min_isk) | (latest["date"] < feats["date"].max() - pd.Timedelta(days=7))]
    kept = feats[(feats["isk_30"] >= min_isk) & ~feats["type_id"].isin(out["type_id"])]
    return kept, sorted(out["name"].astype(str))


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


def backtest(feats: pd.DataFrame, horizon: int = 7, n_folds: int = 5, test_days: int = 21,
             return_predictions: bool = False, features: list[str] | None = None, min_train: int = 500):
    """Walk-forward evaluation. Training rows end `horizon` days before each test window so
    no training target overlaps the test period.

    With `return_predictions=True`, also returns the out-of-sample predictions for every
    test row (one column per model), for plotting predicted vs. actual."""
    features = features or FEATURES
    data = feats.dropna(subset=features + ["target"])
    dates = np.sort(data["date"].unique())
    results, oos = [], []
    for k in range(n_folds, 0, -1):
        test_start = dates[-k * test_days]
        test_end = test_start + np.timedelta64(test_days, "D")
        train = data[data["date"] < test_start - np.timedelta64(horizon, "D")]
        test = data[(data["date"] >= test_start) & (data["date"] < test_end)]
        if len(train) < min_train or test.empty:
            continue
        y = test["target"].to_numpy()
        preds = {
            "no_change": np.zeros_like(y),
            # momentum: assume the last `horizon` days' move continues
            "momentum": test[f"ret_{min(LAGS, key=lambda l: abs(l - horizon))}"].to_numpy(),
        }
        for name, model in make_models().items():
            model.fit(train[features], train["target"])
            preds[name] = model.predict(test[features])
        for name, p in preds.items():
            results.append({"fold_start": pd.Timestamp(test_start).date(), "model": name, **_metrics(y, p)})
        if return_predictions:
            oos.append(test[["date", "type_id", "average", "target"]].assign(**preds))
    results = pd.DataFrame(results)
    if return_predictions:
        return results, pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    return results


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


# ---------------------------------------------------------------------------
# Station-level model: predicts the hub station's own mid price (from hourly order-book
# snapshots), using the regional features above plus the station's premium and momentum.
# ---------------------------------------------------------------------------
STATION_FEATURES = FEATURES + ["premium", "sret_1", "sret_3", "sret_7", "spread", "imbalance"]
MIN_STATION_DAYS = 45  # days of snapshots needed before the station model is trained


def station_days(station: pd.DataFrame) -> int:
    return 0 if station.empty else int(station["taken_at"].dt.normalize().nunique())


def build_station_features(regional: pd.DataFrame, station: pd.DataFrame, horizon: int = 7) -> pd.DataFrame:
    """One row per item per day of snapshots. `regional` comes from build_features; `station` from
    storage.read_station_prices. Regional history lags a day behind live orders, so each station
    day is joined to the latest regional row on or before it."""
    if station.empty:
        return pd.DataFrame(columns=["date", "type_id", "mid", "target", *STATION_FEATURES])
    st = station.copy()
    st["date"] = st["taken_at"].dt.tz_localize(None).dt.normalize() if st["taken_at"].dt.tz else st["taken_at"].dt.normalize()
    st["spread_raw"] = (st["ask_top5"].fillna(st["best_ask"]) - st["bid_top5"].fillna(st["best_bid"])) / st["mid"]
    if {"bid_depth", "ask_depth"} <= set(st.columns):  # book imbalance: ISK within 5% of best, bid vs ask
        st["imb_raw"] = ((st["bid_depth"] - st["ask_depth"]) / (st["bid_depth"] + st["ask_depth"]).replace(0, np.nan))
    else:
        st["imb_raw"] = np.nan
    daily = st.sort_values("taken_at").groupby(["type_id", "date"]).agg(
        mid=("mid", "last"), spread=("spread_raw", "median"), imbalance=("imb_raw", "median")).reset_index()
    frames = []
    for type_id, g in daily.groupby("type_id"):
        g = g.set_index("date").asfreq("D")
        g["type_id"] = type_id
        g["mid"] = g["mid"].ffill()
        g["spread"] = g["spread"].ffill()
        g["imbalance"] = g["imbalance"].ffill().fillna(0.0)  # snapshots from before depth was recorded
        logm = np.log(g["mid"])
        for lag in (1, 3, 7):
            g[f"sret_{lag}"] = logm - logm.shift(lag)
        g["target"] = logm.shift(-horizon) - logm
        frames.append(g.rename_axis("date").reset_index())
    daily = pd.concat(frames, ignore_index=True).sort_values("date")
    reg = regional.drop(columns=["target"]).sort_values("date")
    out = pd.merge_asof(daily, reg, on="date", by="type_id", direction="backward")
    out["premium"] = np.log(out["mid"] / out["average"])
    return out


def station_model_status(station_feats: pd.DataFrame, days: int) -> dict:
    return {"ready": days >= MIN_STATION_DAYS, "days": days, "needed": MIN_STATION_DAYS}


def predict_station(station_feats: pd.DataFrame, model_name: str = "ridge", horizon: int = 7):
    """Backtest and latest forecasts of the station mid price, or None if there isn't enough data."""
    labeled = station_feats.dropna(subset=STATION_FEATURES + ["target"])
    if labeled["date"].nunique() < MIN_STATION_DAYS - horizon:
        return None
    test_days = 7
    metrics = backtest(station_feats, horizon, n_folds=3, test_days=test_days, features=STATION_FEATURES, min_train=100)
    model = make_models()[model_name].fit(labeled[STATION_FEATURES], labeled["target"])
    latest = station_feats.dropna(subset=STATION_FEATURES).sort_values("date").groupby("type_id").tail(1).copy()
    latest["pred"] = model.predict(latest[STATION_FEATURES])
    return metrics, latest
