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
    PRIMARY KEY (taken_at, region_id, type_id)
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
    conn.executescript(SCHEMA)
    return conn


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


def save_type_names(conn: sqlite3.Connection, items: dict[str, int]) -> None:
    conn.executemany("INSERT OR REPLACE INTO types VALUES (?,?)", [(t, n) for n, t in items.items()])
    conn.commit()


def read_history(conn: sqlite3.Connection, type_ids: list[int]) -> pd.DataFrame:
    """History for the given types, whichever region each was collected from.

    If an item has rows from two regions on the same day (PLEX around its move from Jita to
    the global market), the row with the larger traded volume wins."""
    marks = ",".join("?" * len(type_ids))
    df = pd.read_sql_query(
        f"SELECT h.*, t.name FROM history h LEFT JOIN types t USING (type_id) "
        f"WHERE h.type_id IN ({marks}) ORDER BY type_id, date, volume DESC",
        conn, params=tuple(type_ids),
    )
    df = df.drop_duplicates(["type_id", "date"], keep="first").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df
