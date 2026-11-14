"""Build a static copy of the dashboard for GitHub Pages (or any static host).

    python -m eve_market site                # writes site/ ; preview with: python -m http.server -d site

GitHub Pages can't run Flask, so this asks the Flask app for every page and every API answer the
dashboard's JavaScript can request, and saves them as files: the four pages as .html, and the API
answers under data/ (one JSON file per market, item, list, model and horizon). The JavaScript reads
those files when the page was built with `static_export` (see apiUrl in static/common.js).
"""
from __future__ import annotations

import json
import math
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from eve_market import storage
from eve_market.config import ITEM_LISTS, MARKETS
from eve_market.dashboard.server import HERE, PAGES, create_app

log = logging.getLogger(__name__)

# What the Predictions page's buttons can ask for (templates/predictions.html).
PRED_LISTS = ("core", "commodities", "all")
PRED_MODELS = ("ridge", "gbm")
PRED_HORIZONS = (3, 7, 14)
DEFAULT_REPO = "https://github.com/InTheHiggsField/Eve-Market-Predictor"


def _round(x):
    """6 significant digits keeps the files about a third smaller with no visible change on the charts."""
    if isinstance(x, float):
        return float(f"{x:.6g}") if math.isfinite(x) else None
    if isinstance(x, list):
        return [_round(v) for v in x]
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    return x


def build_site(out: str | Path = "site", db_path=storage.DEFAULT_DB, refresh_hours: float = 6,
               repo_url: str = DEFAULT_REPO, markets: list[str] | None = None) -> Path:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    shutil.copytree(HERE / "static", out / "static")
    (out / ".nojekyll").touch()  # serve files as they are; no Jekyll processing on Pages

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    app = create_app(db_path, static_export={"built_at": built_at, "refresh_hours": f"{refresh_hours:g}",
                                             "repo_url": repo_url})
    client = app.test_client()

    def get(url: str):
        r = client.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"{url}: HTTP {r.status_code}")
        return r

    def save_json(rel: str, url: str):
        path = out / "data" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_round(get(url).get_json()), separators=(",", ":"), allow_nan=False))

    for page, filename in PAGES.items():
        url = "/" if page == "market" else f"/{page}"
        (out / filename).write_bytes(get(url).data)

    started = time.monotonic()
    for market in markets or list(MARKETS):
        save_json(f"items/{market}.json", f"/api/items?market={market}")
        save_json(f"health/{market}.json", f"/api/health?market={market}")
        type_ids = sorted({i["type_id"] for i in get(f"/api/items?market={market}").get_json()})
        for tid in type_ids:
            save_json(f"history/{tid}/{market}.json", f"/api/history/{tid}?market={market}")
            save_json(f"station/{tid}/{market}.json", f"/api/station/{tid}?market={market}")
        # Loop order matters: the backtest is cached per (market, list, horizon) and shared by both models.
        for lst in PRED_LISTS:
            if lst not in ITEM_LISTS:
                continue
            for h in PRED_HORIZONS:
                for model in PRED_MODELS:
                    save_json(f"predictions/{market}/{lst}-{model}-{h}.json",
                              f"/api/predictions?market={market}&list={lst}&model={model}&horizon={h}")
        log.info("%s: %d items, predictions done (%.0fs so far)", market, len(type_ids), time.monotonic() - started)

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    log.info("Static site written to %s (%.1f MB)", out, size)
    return out
