"""Backfill daily market history from the EVE Ref archive (https://data.everef.net/market-history/).

EVE Ref stores one bz2-compressed CSV per day, for every region and item, back to 2003-10-01,
in the same shape as ESI's history endpoint. Each file is streamed into memory, trimmed to the
tracked items, and upserted; nothing is kept on disk. Finished days are logged so a re-run
only fetches what's missing (or what changed upstream, or days collected for a different item set).
"""
from __future__ import annotations

import bz2
import csv
import hashlib
import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

from . import storage
from .esi import USER_AGENT

log = logging.getLogger(__name__)
ROOT_INDEX = "https://data.everef.net/market-history/index.json"

#This set of function looks into the historical data from evefet.net to gather long term historical market data inorder to enable XGBoost prediction modeling

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_regions (
    file_date TEXT NOT NULL,
    region_id INTEGER NOT NULL,
    etag TEXT,
    item_set TEXT,
    rows INTEGER,
    PRIMARY KEY (file_date, region_id)
);
"""


def _region_sets(keep: set[tuple[int, int]]) -> dict[int, str]:
    """A short fingerprint of which items are kept per region, so adding a hub or item only
    re-fetches days for the regions that changed."""
    by_region: dict[int, list[int]] = {}
    for r, t in keep:
        by_region.setdefault(r, []).append(t)
    return {r: hashlib.sha1(repr(sorted(ts)).encode()).hexdigest()[:12] for r, ts in by_region.items()}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def list_files(session: requests.Session, start: str, end: str) -> list[dict]:
    """All archive files (url, date, etag, size) between start and end inclusive."""
    years = session.get(ROOT_INDEX, timeout=30).json()["directories"]
    files = []
    for y in years:
        if not (start[:4] <= y["name"] <= end[:4]):
            continue
        for f in session.get(y["index_url"], timeout=30).json()["files"]:
            day = f["file_time"][:10]
            if f["name"].endswith(".csv.bz2") and start <= day <= end:
                files.append({"url": f["url"], "date": day, "etag": f.get("etag"), "size": f.get("size", 0)})
    return sorted(files, key=lambda f: f["date"])


def _wanted_key(items: dict[str, int], region_overrides: dict[str, int], regions: list[int]) -> set[tuple[int, int]]:
    """(region_id, type_id) pairs to keep. Override items are kept from both their own region and the
    hub regions, because PLEX traded at regional hubs before it moved to the global market."""
    keep = {(r, t) for r in regions for t in items.values()}
    keep |= {(r, items[n]) for n, r in region_overrides.items() if n in items}
    return keep


def fetch_and_trim(session: requests.Session, url: str, keep: set[tuple[int, int]]) -> list[tuple]:
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=120)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
    text = bz2.decompress(resp.content).decode("utf-8")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        key = (int(r["region_id"]), int(r["type_id"]))
        if key in keep:
            out.append((key[0], key[1], r["date"], float(r["average"]), float(r["highest"]),
                        float(r["lowest"]), int(float(r["volume"])), int(float(r["order_count"]))))
    return out


def backfill(items: dict[str, int], region_overrides: dict[str, int], regions: list[int], start: str = "2003-10-01",
             end: str | None = None, db_path=storage.DEFAULT_DB, workers: int = 4, force: bool = False) -> None:
    end = end or date.today().isoformat()
    keep = _wanted_key(items, region_overrides, regions)
    region_sets = _region_sets(keep)
    conn = storage.connect(db_path)
    conn.executescript(LOG_SCHEMA)
    storage.save_type_names(conn, items)
    done = {(d, r): (e, s) for d, r, e, s in conn.execute(
        "SELECT file_date, region_id, etag, item_set FROM backfill_regions")}

    session = _session()
    files = list_files(session, start, end)
    for f in files:  # regions this day still needs
        f["regions"] = [r for r, h in region_sets.items() if force or done.get((f["date"], r)) != (f["etag"], h)]
    todo = [f for f in files if f["regions"]]
    mb = sum(f["size"] for f in todo) / 1e6
    log.info("%d archive days in range, %d to fetch (~%.0f MB download)", len(files), len(todo), mb)

    total_rows = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(fetch_and_trim, session, f["url"], {k for k in keep if k[0] in f["regions"]}): f
                   for f in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            f = futures[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                log.warning("%s failed: %s (re-run backfill to retry)", f["date"], exc)
                continue
            conn.executemany("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.executemany("INSERT OR REPLACE INTO backfill_regions VALUES (?,?,?,?,?)",
                             [(f["date"], r, f["etag"], region_sets[r], sum(1 for x in rows if x[0] == r))
                              for r in f["regions"]])
            conn.commit()
            total_rows += len(rows)
            if i % 100 == 0 or i == len(todo):
                log.info("[%d/%d] through %s, %d rows stored so far", i, len(todo), f["date"], total_rows)
    conn.close()
    log.info("Backfill finished: %d rows from %d days", total_rows, len(todo))
