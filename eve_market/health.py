"""Market health: how tradeable is each item at each hub?

A healthy market lets you trade size quickly, close to fair value, without moving the price,
and reflects many independent traders. The metrics here come from market microstructure:

  from daily history      turnover, Amihud price impact, volatility and its clustering,
                          bid-ask bounce (return autocorrelation), Roll implied spread,
                          daily range, trade activity, outlier prints, price gap to Jita
  from hourly snapshots   quoted and effective spread, depth near the top of the book,
                          book imbalance, bait orders
  from raw orders         concentration of the top of the book, order churn

The 0-100 score is the average percentile of the liquidity metrics, ranked across every item
at every hub so that a thin hub is not graded on a curve. Volatility, imbalance and the other
warning signs are flags, not part of the score: a volatile market is not unhealthy by itself.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from eve_market import storage
from eve_market.config import DEFAULT_MARKET, ITEM_GROUPS, ITEM_LISTS, MARKETS, load_history
from eve_market.forecast import DEFAULT_MIN_ISK, clean_log_price

WINDOW = 30       # days for liquidity metrics
LONG_WINDOW = 90  # days for volatility and autocorrelation, which need more data to be stable

# (column, True if higher is healthier)
SCORE_PARTS = [("isk_turnover", True), ("amihud", False), ("roll_spread", False), ("trades", True),
               ("zero_days", False), ("quoted_spread", False), ("depth", True)]

FLAG_RULES = [  # (flag, column, test)
    ("volatile", "volatility", lambda v: v > 1.0),
    ("outlier prints", "outlier_days", lambda v: v >= 2),
    ("wide spread", "quoted_spread", lambda v: v > 0.15),
    ("one-sided book", "imbalance", lambda v: v.abs() > 0.95),
    ("bait order", "bait", lambda v: v > 0.05),
    ("price gap vs Jita", "hub_gap", lambda v: v > 0.15),
    ("concentrated book", "top_share", lambda v: v > 0.8),
    ("order churn", "churn", lambda v: v > 0.5),
]


def _autocorr(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.autocorr(1)) if len(x) > 10 and x.std() > 0 else np.nan


def history_metrics(hist: pd.DataFrame, asof: pd.Timestamp | None = None) -> pd.DataFrame:
    """One row per item from daily history, measured over the days up to `asof`
    (default: the latest day in `hist`). Days without a trade count as zero volume."""
    if hist.empty:
        return pd.DataFrame()
    asof = asof or hist["date"].max()
    days = pd.date_range(asof - pd.Timedelta(days=LONG_WINDOW + 30), asof, freq="D")
    rows = []
    for type_id, g in hist.groupby("type_id"):
        name = g["name"].iloc[0]
        g = g.set_index("date").sort_index().reindex(days)
        g[["average", "highest", "lowest"]] = g[["average", "highest", "lowest"]].ffill()
        g[["volume", "order_count"]] = g[["volume", "order_count"]].fillna(0)
        row = {"type_id": int(type_id), "name": name, "price": g["average"].iloc[-1]}
        if g["average"].notna().sum() < WINDOW:
            rows.append({**row, "isk_turnover": 0.0, "trades": 0.0, "zero_days": 1.0})
            continue
        logp = np.log(g["average"])
        clean = clean_log_price(logp)
        r = clean.diff()                       # returns with outlier prints clipped
        isk = g["average"] * g["volume"]
        w, lw = slice(-WINDOW, None), slice(-LONG_WINDOW, None)
        traded = isk.iloc[w] > 0
        cov = r.iloc[lw].cov(r.iloc[lw].shift(1))
        active = (g["volume"].iloc[lw] > 0).sum() >= 20  # too few trading days: return stats are meaningless
        rows.append({
            **row,
            "isk_turnover": float(isk.iloc[w].median()),
            "amihud": float((r.iloc[w].abs()[traded] / isk.iloc[w][traded]).median() * 1e9) if traded.any() else np.nan,
            "volatility": float(r.iloc[lw].std() * np.sqrt(365)) if active else np.nan,
            "vol_cluster": _autocorr(r.iloc[lw].abs()) if active else np.nan,
            "bounce": _autocorr(r.iloc[lw]) if active else np.nan,
            "roll_spread": (float(2 * np.sqrt(-cov)) if cov < 0 else 0.0) if active else np.nan,
            "daily_range": float(((g["highest"] - g["lowest"]) / g["average"]).iloc[w].median()),
            "trades": float(g["order_count"].iloc[w].median()),
            "zero_days": float((g["volume"].iloc[w] == 0).mean()),
            "outlier_days": int((clean != logp).iloc[w].sum()),
        })
    return pd.DataFrame(rows).set_index("type_id")


def snapshot_metrics(snaps: pd.DataFrame) -> pd.DataFrame:
    """Spread, depth and imbalance per item: medians over the given order-book snapshots."""
    if snaps.empty:
        return pd.DataFrame()
    s = snaps.copy()
    mid = (s["best_bid"] + s["best_ask"]) / 2
    s["quoted_spread"] = (s["best_ask"] - s["best_bid"]) / mid
    s["effective_spread"] = (s["ask_top5"] - s["bid_top5"]) / ((s["ask_top5"] + s["bid_top5"]) / 2)
    s["depth"] = s[["bid_depth", "ask_depth"]].min(axis=1, skipna=False)
    s["imbalance"] = (s["bid_depth"] - s["ask_depth"]) / (s["bid_depth"] + s["ask_depth"]).replace(0, np.nan)
    # A best order far from the bulk of the book is usually one unit listed as bait.
    s["bait"] = pd.concat([(s["best_ask"] / s["ask_top5"] - 1).abs(), (s["best_bid"] / s["bid_top5"] - 1).abs()],
                          axis=1).max(axis=1)
    out = s.groupby("type_id")[["quoted_spread", "effective_spread", "depth", "imbalance", "bait"]].median()
    out["snapshots"] = s.groupby("type_id").size()
    return out


def book_metrics(orders: pd.DataFrame) -> pd.DataFrame:
    """Concentration and churn of the top of the book, from raw orders.

    ESI does not say who owns a public order, so concentration is measured per order: the ISK
    share of the largest single order among those within 5% of the best price (worse side),
    and the Herfindahl index of those shares. Churn is the share of those orders created or
    repriced within the hour before the snapshot, the mark of an undercutting war."""
    if orders.empty:
        return pd.DataFrame()
    o = orders.copy()
    o["isk"] = o["price"] * o["volume_remain"]
    prices = o.groupby(["taken_at", "type_id", "is_buy"])["price"]
    best = pd.Series(np.where(o["is_buy"] == 1, prices.transform("max"), prices.transform("min")), index=o.index)
    o = o[(o["price"] >= best * 0.95) & (o["price"] <= best * 1.05)]
    age = (pd.to_datetime(o["taken_at"], utc=True) - pd.to_datetime(o["issued"], utc=True)).dt.total_seconds() / 3600
    o["fresh"] = age < 1
    side = o.groupby(["taken_at", "type_id", "is_buy"])
    share = o["isk"] / side["isk"].transform("sum").replace(0, np.nan)
    per_side = pd.DataFrame({"top_share": share.groupby([o["taken_at"], o["type_id"], o["is_buy"]]).max(),
                             "hhi": (share ** 2).groupby([o["taken_at"], o["type_id"], o["is_buy"]]).sum()})
    per_snap = per_side.groupby(level=[0, 1]).max()
    per_snap["churn"] = o.groupby(["taken_at", "type_id"])["fresh"].mean()
    per_snap["near_orders"] = o.groupby(["taken_at", "type_id"]).size()
    return per_snap.groupby(level=1).median()


def hub_gap(hist: pd.DataFrame, ref: pd.DataFrame) -> pd.Series:
    """Median absolute log price difference from the reference hub over the last WINDOW days."""
    m = hist.merge(ref[["type_id", "date", "average"]], on=["type_id", "date"], suffixes=("", "_ref"))
    m = m[m["date"] > m["date"].max() - pd.Timedelta(days=WINDOW)] if len(m) else m
    return np.log(m["average"] / m["average_ref"]).abs().groupby(m["type_id"]).median()


def _market_metrics(conn, market: str, ref_hist: pd.DataFrame | None, since: str) -> pd.DataFrame:
    hist = load_history(conn, "all", market, since)
    df = history_metrics(hist)
    if df.empty:
        return df
    station = MARKETS[market]["station_id"]
    for extra in (snapshot_metrics(storage.read_snapshots(conn, station)),
                  book_metrics(storage.read_raw_orders(conn, station))):
        if not extra.empty:
            df = df.join(extra)
    if ref_hist is not None and market != DEFAULT_MARKET:
        df["hub_gap"] = hub_gap(hist, ref_hist)
    df["market"] = market
    return df.reset_index()


def health_table(conn, market: str = DEFAULT_MARKET, list_name: str = "all",
                 min_isk: float = DEFAULT_MIN_ISK) -> pd.DataFrame:
    """Health metrics, score, label and flags for every item of `list_name` at `market`."""
    last = conn.execute("SELECT MAX(date) FROM history").fetchone()[0]
    if last is None:
        return pd.DataFrame()
    since = (pd.Timestamp(last) - pd.Timedelta(days=LONG_WINDOW + 40)).strftime("%Y-%m-%d")
    ref = load_history(conn, "all", DEFAULT_MARKET, since)
    frames = [f for f in (_market_metrics(conn, m, ref, since) for m in MARKETS) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = score(df, min_isk)
    df = df[(df["market"] == market) & df["type_id"].isin(ITEM_LISTS[list_name].values())]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def score(df: pd.DataFrame, min_isk: float = DEFAULT_MIN_ISK) -> pd.DataFrame:
    """Add score (0-100), label and flags. Percentiles are taken over every row passed in."""
    df = df.copy()
    parts = []
    for col, higher_better in SCORE_PARTS:
        if col not in df or df[col].notna().sum() < 5:
            continue
        pct = df[col].rank(pct=True, ascending=higher_better)
        # An item that never trades has no Amihud or Roll estimate: that is the worst case, not a gap.
        parts.append(pct.fillna(0) if col in ("amihud", "roll_spread") else pct)
    df["score"] = (pd.concat(parts, axis=1).mean(axis=1) * 100).round(1) if parts else np.nan
    df["below_floor"] = df["isk_turnover"] < min_isk
    df["label"] = np.where(df["below_floor"] | (df["score"] < 35), "Illiquid",
                           np.where(df["score"] >= 60, "Healthy", "Thin"))
    flags = pd.Series([[] for _ in range(len(df))], index=df.index)
    for flag, col, test in FLAG_RULES:
        if col in df:
            hit = test(df[col]).fillna(False)
            flags[hit] = flags[hit].apply(lambda f: f + [flag])
    df["flags"] = flags
    df["group"] = df["name"].map(ITEM_GROUPS).fillna("")
    return df


def print_health(df: pd.DataFrame) -> None:
    if df.empty:
        print("No history yet. Run `collect` first.")
        return
    out = pd.DataFrame({
        "item": df["name"], "score": df["score"], "label": df["label"],
        "ISK/day": df["isk_turnover"].map(lambda v: f"{v / 1e9:,.2f}B"),
        "spread": df.get("quoted_spread", pd.Series(np.nan, df.index)).map(lambda v: "" if pd.isna(v) else f"{v:.1%}"),
        "roll": df["roll_spread"].map(lambda v: "" if pd.isna(v) else f"{v:.1%}"),
        "vol": df["volatility"].map(lambda v: "" if pd.isna(v) else f"{v:.0%}"),
        "bounce": df["bounce"].round(2), "amihud": df["amihud"].round(4),
        "zero days": df["zero_days"].map(lambda v: f"{v:.0%}"),
        "flags": df["flags"].map(", ".join),
    })
    print(out.to_string(index=False))
    print(f"\n{(df.label == 'Healthy').sum()} healthy, {(df.label == 'Thin').sum()} thin, "
          f"{(df.label == 'Illiquid').sum()} illiquid. Score = average percentile across all hubs.")
