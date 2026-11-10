"""SQLite storage so collected data accumulates across runs."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    region_id INTEGER NOT NULL,
    type_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    average REAL, highest REAL, lowest REAL,
    volume INTEGER, order_count INTEGER,
    PRIMARY KEY (region_id, type_id, date)
);
CREATE TABLE IF NOT EXISTS order_snapshots (
    taken_at TEXT NOT NULL,
    region_id INTEGER NOT NULL,
    location_id INTEGER,
    type_id INTEGER NOT NULL,
    best_bid REAL, best_ask REAL,
    bid_volume INTEGER, ask_volume INTEGER,
    bid_orders INTEGER, ask_orders INTEGER,
    bid_top5 REAL, ask_top5 REAL,
    bid_depth REAL, ask_depth REAL,
    PRIMARY KEY (taken_at, region_id, location_id, type_id)
);
CREATE TABLE IF NOT EXISTS raw_orders (
    taken_at TEXT NOT NULL,
    location_id INTEGER NOT NULL,
    type_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    is_buy INTEGER NOT NULL,
    price REAL, volume_remain INTEGER, volume_total INTEGER,
    issued TEXT,
    PRIMARY KEY (taken_at, order_id)
);
CREATE INDEX IF NOT EXISTS raw_orders_book ON raw_orders (location_id, type_id, taken_at);
CREATE TABLE IF NOT EXISTS collect_runs (
    taken_at TEXT PRIMARY KEY,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS types (
    type_id INTEGER PRIMARY KEY,
    name TEXT
);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Older databases keyed order snapshots without location_id (one hub only). Rebuild that
    table so snapshots from several hub stations in one region can coexist."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'order_snapshots'").fetchone()
    if row and "location_id, type_id)" not in row[0]:
        conn.executescript("""
            ALTER TABLE order_snapshots RENAME TO order_snapshots_old;
        """ + SCHEMA + """
            INSERT INTO order_snapshots (taken_at, region_id, location_id, type_id, best_bid, best_ask,
                bid_volume, ask_volume, bid_orders, ask_orders)
            SELECT taken_at, region_id, location_id, type_id, best_bid, best_ask,
                bid_volume, ask_volume, bid_orders, ask_orders FROM order_snapshots_old;
            DROP TABLE order_snapshots_old;
        """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(order_snapshots)")}
    if cols and "bid_top5" not in cols:
        conn.executescript("ALTER TABLE order_snapshots ADD COLUMN bid_top5 REAL;"
                           "ALTER TABLE order_snapshots ADD COLUMN ask_top5 REAL;")
        cols |= {"bid_top5"}
    if cols and "bid_depth" not in cols:
        conn.executescript("ALTER TABLE order_snapshots ADD COLUMN bid_depth REAL;"
                           "ALTER TABLE order_snapshots ADD COLUMN ask_depth REAL;")


def upsert_history(conn: sqlite3.Connection, region_id: int, type_id: int, rows: list[dict]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?)",
        [
            (region_id, type_id, r["date"], r.get("average"), r.get("highest"), r.get("lowest"),
             r.get("volume"), r.get("order_count"))
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def insert_snapshot(conn: sqlite3.Connection, row: dict) -> None:
    cols = ",".join(row)
    conn.execute(f"INSERT OR REPLACE INTO order_snapshots ({cols}) VALUES ({','.join('?' * len(row))})",
                 tuple(row.values()))
    conn.commit()


def insert_raw_orders(conn: sqlite3.Connection, taken_at: str, location_id: int, type_id: int,
                      orders: list[dict]) -> None:
    """Individual orders near the top of one station's book (see collect.near_touch)."""
    conn.executemany(
        "INSERT OR REPLACE INTO raw_orders VALUES (?,?,?,?,?,?,?,?,?)",
        [(taken_at, location_id, type_id, o["order_id"], int(o["is_buy_order"]), o["price"],
          o["volume_remain"], o.get("volume_total"), o.get("issued")) for o in orders])
    conn.commit()


def prune_raw_orders(conn: sqlite3.Connection, keep_days: int = 90) -> int:
    """Raw orders are bulky and only needed for recent book analysis; drop the old ones."""
    cur = conn.execute("DELETE FROM raw_orders WHERE taken_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                       (f"-{keep_days} days",))
    conn.commit()
    return cur.rowcount


def read_raw_orders(conn: sqlite3.Connection, station_id: int, hours: int = 24) -> pd.DataFrame:
    """Raw orders from the snapshots of the last `hours` before the station's latest snapshot."""
    return pd.read_sql_query(
        "SELECT taken_at, type_id, order_id, is_buy, price, volume_remain, issued FROM raw_orders "
        "WHERE location_id = ? AND taken_at >= strftime('%Y-%m-%dT%H:%M:%SZ', "
        "(SELECT MAX(taken_at) FROM raw_orders WHERE location_id = ?), ?)",
        conn, params=(station_id, station_id, f"-{hours} hours"))


def read_snapshots(conn: sqlite3.Connection, station_id: int, days: int = 14) -> pd.DataFrame:
    """Order-book summaries from the last `days` before the station's latest snapshot."""
    return pd.read_sql_query(
        "SELECT * FROM order_snapshots WHERE location_id = ? AND taken_at >= strftime('%Y-%m-%dT%H:%M:%SZ', "
        "(SELECT MAX(taken_at) FROM order_snapshots WHERE location_id = ?), ?)",
        conn, params=(station_id, station_id, f"-{days} days"))


def log_run(conn: sqlite3.Connection, taken_at: str, kind: str) -> None:
    conn.execute("INSERT OR REPLACE INTO collect_runs VALUES (?, ?)", (taken_at, kind))
    conn.commit()


def last_run(conn: sqlite3.Connection, needs: str) -> str | None:
    """Timestamp of the latest collect run that included `needs` ("history" or "snapshots")."""
    return conn.execute("SELECT MAX(taken_at) FROM collect_runs WHERE kind LIKE ?", (f"%{needs}%",)).fetchone()[0]


def read_station_prices(conn: sqlite3.Connection, station_id: int, type_ids: list[int]) -> pd.DataFrame:
    """Every snapshot taken at one station: that station's own price series."""
    marks = ",".join("?" * len(type_ids))
    df = pd.read_sql_query(
        f"SELECT taken_at, type_id, best_bid, best_ask, bid_top5, ask_top5, bid_volume, ask_volume, "
        f"bid_depth, ask_depth "
        f"FROM order_snapshots WHERE location_id = ? AND type_id IN ({marks}) ORDER BY type_id, taken_at",
        conn, params=(station_id, *type_ids))
    df["taken_at"] = pd.to_datetime(df["taken_at"])
    # Mid price: halfway between the best 5% of bids and of asks, falling back to the single best orders.
    bid = df["bid_top5"].fillna(df["best_bid"])
    ask = df["ask_top5"].fillna(df["best_ask"])
    df["mid"] = (bid + ask) / 2
    return df


def save_type_names(conn: sqlite3.Connection, items: dict[str, int]) -> None:
    conn.executemany("INSERT OR REPLACE INTO types VALUES (?,?)", [(t, n) for n, t in items.items()])
    conn.commit()


def read_history(conn: sqlite3.Connection, type_ids: list[int], region_id: int,
                 extra_regions: tuple[int, ...] = (), since: str | None = None) -> pd.DataFrame:
    """History for the given types in one hub's region.

    `extra_regions` adds markets that span regions (the global PLEX market). If an item has rows
    from two regions on the same day (PLEX around its move to the global market), the row with
    the larger traded volume wins."""
    marks = ",".join("?" * len(type_ids))
    regions = (region_id, *extra_regions)
    df = pd.read_sql_query(
        f"SELECT h.*, t.name FROM history h LEFT JOIN types t USING (type_id) "
        f"WHERE h.type_id IN ({marks}) AND h.region_id IN ({','.join('?' * len(regions))}) "
        f"AND h.date >= ? ORDER BY type_id, date, volume DESC",
        conn, params=(*type_ids, *regions, since or ""),
    )
    df = df.drop_duplicates(["type_id", "date"], keep="first").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df
