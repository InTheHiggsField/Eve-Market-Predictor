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


def load_type_names(conn: sqlite3.Connection, csv_path: Path) -> None:
    if conn.execute("SELECT COUNT(*) FROM types").fetchone()[0]:
        return
    df = pd.read_csv(csv_path)
    conn.executemany("INSERT OR REPLACE INTO types VALUES (?,?)", df[["typeID", "typeName"]].itertuples(index=False))
    conn.commit()


def read_history(conn: sqlite3.Connection, region_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT h.*, t.name FROM history h LEFT JOIN types t USING (type_id) WHERE region_id = ? ORDER BY type_id, date",
        conn, params=(region_id,),
    )
    df["date"] = pd.to_datetime(df["date"])
    return df
