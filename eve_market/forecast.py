"""Baseline price forecasting from daily market history.

Target: log return of the daily average price over the next `horizon` days.
One pooled model is trained across all items on per-item lag features, and
evaluated with a walk-forward backtest against naive baselines. MODEL_REGISTRY lists every
model; DEFAULT_MODELS is the set the dashboard runs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import HuberRegressor, Ridge
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


# ---------------------------------------------------------------------------
# Models. Every model takes the whole feature frame and picks its own columns, so
# models that need more than FEATURES (ETS, per-item AR) fit the same interface.
# ---------------------------------------------------------------------------
class Cols(BaseEstimator, TransformerMixin):
    """Pipeline step that keeps only the given columns (as float)."""

    def __init__(self, columns):
        self.columns = columns

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        return X[list(self.columns)].to_numpy(dtype=float)


def _on(cols, *steps):
    return make_pipeline(Cols(cols), *steps)


class QuantileBand(BaseEstimator, RegressorMixin):
    """Three gradient-boosted quantile models. predict() is the median (the point forecast,
    which suits the MAE score); predict_interval() gives the low/high band."""

    def __init__(self, features=None, low: float = 0.1, high: float = 0.9):
        self.features, self.low, self.high = features, low, high

    def _q(self, q):
        return _on(self.features or FEATURES, HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=300,
                                                           learning_rate=0.05, max_leaf_nodes=15,
                                                           min_samples_leaf=50, random_state=0))

    def fit(self, X, y):
        self.models_ = {q: self._q(q).fit(X, y) for q in (self.low, 0.5, self.high)}
        return self

    def predict(self, X):
        return self.models_[0.5].predict(X)

    def predict_interval(self, X):
        lo, hi = self.models_[self.low].predict(X), self.models_[self.high].predict(X)
        return np.minimum(lo, hi), np.maximum(lo, hi)


class Ensemble(BaseEstimator, RegressorMixin):
    """Average of several member models' forecasts. Members whose library isn't installed
    are left out. The price band comes from the quantile member, centred on the average."""

    def __init__(self, members: tuple[str, ...], features=None):
        self.members, self.features = members, features

    def fit(self, X, y):
        self.models_ = make_models(self.members, self.features)
        for m in self.models_.values():
            m.fit(X, y)
        return self

    def predict(self, X):
        return np.mean([m.predict(X) for m in self.models_.values()], axis=0)

    def predict_interval(self, X):
        q = self.models_["quantile"]
        lo, hi = q.predict_interval(X)
        shift = self.predict(X) - q.predict(X)
        return lo + shift, hi + shift


def _lgbm(f, objective):
    import lightgbm as lgb
    return _on(f, lgb.LGBMRegressor(objective=objective, n_estimators=400, learning_rate=0.03,
                                           num_leaves=15, min_child_samples=100, subsample=0.8, subsample_freq=1,
                                           colsample_bytree=0.8, reg_lambda=1.0, random_state=0, verbose=-1))


def _xgb(f, objective):
    import xgboost as xgb
    return _on(f, xgb.XGBRegressor(objective=objective, n_estimators=400, learning_rate=0.03, max_depth=4,
                                          min_child_weight=50, subsample=0.8, colsample_bytree=0.8,
                                          reg_lambda=1.0, random_state=0, n_jobs=-1))


def _catboost(f, loss):
    import catboost as cb
    return _on(f, cb.CatBoostRegressor(loss_function=loss, iterations=500, learning_rate=0.05, depth=5,
                                              l2_leaf_reg=3.0, random_seed=0, verbose=False, thread_count=-1))


# name -> (factory, what it is). Kept: models that beat ridge and gbm on average in the
# 12-fold backtest over every hub and list (Nov 2026). Tried and dropped for losing to them:
# squared-error LightGBM / XGBoost / CatBoost, per-series exponential smoothing (ETS) and a
# per-item autoregression (ARIMA-style). Optional libraries (lightgbm, xgboost, catboost) are imported
# only when their model is built; if the library isn't installed, the model is skipped.
MODEL_REGISTRY = {
    "ridge": (lambda f: _on(f, StandardScaler(), Ridge(alpha=10.0)), "linear, squared error"),
    "gbm": (lambda f: _on(f, HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=50, l2_regularization=1.0,
        random_state=0)), "sklearn gradient boosting, squared error"),
    "huber": (lambda f: _on(f, StandardScaler(), HuberRegressor(epsilon=1.2, alpha=1e-3, max_iter=500)),
              "linear, robust (Huber) loss"),
    "gbm_l1": (lambda f: _on(f, HistGradientBoostingRegressor(
        loss="absolute_error", max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=50,
        l2_regularization=1.0, random_state=0)), "sklearn gradient boosting, absolute error"),
    "random_forest": (lambda f: _on(f, RandomForestRegressor(
        n_estimators=200, min_samples_leaf=100, max_features=0.5, n_jobs=-1, random_state=0)), "random forest"),
    "extra_trees": (lambda f: _on(f, ExtraTreesRegressor(
        n_estimators=200, min_samples_leaf=100, max_features=0.7, n_jobs=-1, random_state=0)), "extra trees"),
    "lightgbm_l1": (lambda f: _lgbm(f, "l1"), "LightGBM, absolute error"),
    "xgboost_l1": (lambda f: _xgb(f, "reg:absoluteerror"), "XGBoost, absolute error"),
    "catboost_l1": (lambda f: _catboost(f, "MAE"), "CatBoost, absolute error"),
    "quantile": (lambda f: QuantileBand(f), "gradient-boosted median with a 10-90% price band"),
}
ENSEMBLE_MEMBERS = ("ridge", "quantile", "lightgbm_l1", "catboost_l1")
MODEL_REGISTRY["ensemble"] = (lambda f: Ensemble(ENSEMBLE_MEMBERS, f), "average of " + ", ".join(ENSEMBLE_MEMBERS))

# Models the dashboard and the default backtest run. `--models all` runs the whole registry.
# ensemble had the best average skill over every hub and list; quantile is close behind and
# needs only scikit-learn.
DEFAULT_MODELS = ("ridge", "gbm", "quantile", "ensemble")
DEFAULT_MODEL = "ensemble"


def available_models(names=None) -> list[str]:
    """Registry names whose libraries are installed (in registry order)."""
    out = []
    for name in (names or MODEL_REGISTRY):
        try:
            MODEL_REGISTRY[name][0](FEATURES)
        except ImportError:
            continue
        out.append(name)
    return out


def make_models(names=None, features=None) -> dict:
    """Fresh, unfitted models on `features` (default FEATURES). Default names: DEFAULT_MODELS.
    Models whose optional library isn't installed are skipped."""
    names = DEFAULT_MODELS if names is None else names
    features = list(features or FEATURES)
    if names == "all":
        names = list(MODEL_REGISTRY)
    out = {}
    for name in names:
        if name not in MODEL_REGISTRY:
            raise KeyError(f"unknown model {name!r}; choose from {', '.join(MODEL_REGISTRY)}")
        try:
            out[name] = MODEL_REGISTRY[name][0](features)
        except ImportError:
            continue
    return out


def _metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    moved = np.abs(y) > 1e-6
    return {
        "mae": float(np.mean(np.abs(y - pred))),
        "direction_acc": float(np.mean(np.sign(pred[moved]) == np.sign(y[moved]))) if moved.any() else np.nan,
    }


def backtest(feats: pd.DataFrame, horizon: int = 7, n_folds: int = 5, test_days: int = 21,
             return_predictions: bool = False, features: list[str] | None = None, min_train: int = 500,
             models=None):
    """Walk-forward evaluation. Training rows end `horizon` days before each test window so
    no training target overlaps the test period. `models`: registry names (default
    DEFAULT_MODELS, "all" for every installed model). no_change and momentum always run.

    Models with a price band (quantile) also report `coverage`: the share of outcomes that
    landed inside the band (0.8 is right for a 10-90% band).

    With `return_predictions=True`, also returns the out-of-sample predictions for every
    test row (one column per model), for plotting predicted vs. actual."""
    features = list(features or FEATURES)
    data = feats.dropna(subset=features + ["target"])
    dates = np.sort(data["date"].unique())
    names = list(make_models(models, features))
    results, oos = [], []
    for k in range(n_folds, 0, -1):
        if k * test_days > len(dates):
            continue
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
        extra, intervals = {}, {}
        fit_names = [n for n in names if n != "ensemble"]
        if "ensemble" in names:  # its members are fitted once and averaged below
            fit_names += [m for m in ENSEMBLE_MEMBERS if m not in fit_names]
        for name, model in make_models(fit_names, features).items():
            model.fit(train, train["target"])
            preds[name] = model.predict(test)
            if hasattr(model, "predict_interval"):
                lo, hi = intervals[name] = model.predict_interval(test)
                extra[name] = {"coverage": float(np.mean((y >= lo) & (y <= hi)))}
        if "ensemble" in names:  # same result as Ensemble.fit/predict, without refitting the members
            members = [m for m in ENSEMBLE_MEMBERS if m in preds]
            preds["ensemble"] = np.mean([preds[m] for m in members], axis=0)
            if "quantile" in extra:
                lo, hi = intervals["quantile"]
                shift = preds["ensemble"] - preds["quantile"]
                extra["ensemble"] = {"coverage": float(np.mean((y >= lo + shift) & (y <= hi + shift)))}
        for name, p in preds.items():
            results.append({"fold_start": pd.Timestamp(test_start).date(), "model": name, **_metrics(y, p),
                            **extra.get(name, {})})
        if return_predictions:
            oos.append(test[["date", "type_id", "average", "target"]].assign(**preds))
    results = pd.DataFrame(results)
    if return_predictions:
        return results, pd.concat(oos, ignore_index=True) if oos else pd.DataFrame()
    return results


def summarize(results: pd.DataFrame) -> pd.DataFrame:
    """Mean over folds per model, with skill vs. no_change (1 - MAE / no-change MAE, as a %)
    and the share of folds in which the model beat no_change."""
    if results.empty:
        return pd.DataFrame(columns=["mae", "direction_acc", "skill_pct", "fold_wins"])
    agg = {"mae": "mean", "direction_acc": "mean"}
    if "coverage" in results:
        agg["coverage"] = "mean"
    out = results.groupby("model").agg(agg)
    base = results[results.model == "no_change"].set_index("fold_start")["mae"]
    r = results.join(base.rename("base"), on="fold_start")
    out["skill_pct"] = (1 - out["mae"] / base.mean()) * 100
    out["fold_wins"] = r.assign(win=r["mae"] < r["base"]).groupby("model")["win"].mean()
    return out.sort_values("mae")


def fit_model(feats: pd.DataFrame, model_name: str = DEFAULT_MODEL, features: list[str] | None = None):
    """Fit one model on every labeled row."""
    features = list(features or FEATURES)
    train = feats.dropna(subset=features + ["target"])
    return make_models([model_name], features)[model_name].fit(train, train["target"])


def predict_latest(feats: pd.DataFrame, model_name: str = DEFAULT_MODEL) -> pd.DataFrame:
    """Fit on all labeled rows, then forecast from each item's most recent day. Models with
    a price band also return pred_low / pred_high."""
    model = fit_model(feats, model_name)
    latest = feats.dropna(subset=FEATURES).sort_values("date").groupby("type_id").tail(1).copy()
    latest["pred_log_return"] = model.predict(latest)
    latest["pred_pct_change"] = np.expm1(latest["pred_log_return"]) * 100
    latest["pred_price"] = latest["average"] * np.exp(latest["pred_log_return"])
    cols = ["type_id", "name", "date", "average", "pred_price", "pred_pct_change", "vol_30"]
    if hasattr(model, "predict_interval"):
        lo, hi = model.predict_interval(latest)
        latest["pred_low"], latest["pred_high"] = latest["average"] * np.exp(lo), latest["average"] * np.exp(hi)
        cols[5:5] = ["pred_low", "pred_high"]
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
    metrics = backtest(station_feats, horizon, n_folds=3, test_days=test_days, features=STATION_FEATURES, min_train=100,
                       models=sorted({*DEFAULT_MODELS, model_name}, key=list(MODEL_REGISTRY).index))
    model = fit_model(station_feats, model_name, STATION_FEATURES)
    latest = station_feats.dropna(subset=STATION_FEATURES).sort_values("date").groupby("type_id").tail(1).copy()
    latest["pred"] = model.predict(latest)
    return metrics, latest
