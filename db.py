#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "999.db")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS ads (
    id               TEXT PRIMARY KEY,
    profile          TEXT NOT NULL,
    subcategory_id   INTEGER,
    subcategory      TEXT,
    title            TEXT,
    price            REAL,
    currency         TEXT,
    old_price        REAL,
    old_currency     TEXT,
    street           TEXT,
    lat              REAL,
    lon              REAL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    gone_at          TEXT,
    first_price      REAL,
    prev_price       REAL,
    price_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS ads_profile      ON ads(profile);
CREATE INDEX IF NOT EXISTS ads_first_seen   ON ads(first_seen);
CREATE INDEX IF NOT EXISTS ads_price_change ON ads(price_changed_at);

CREATE TABLE IF NOT EXISTS price_history (
    ad_id    TEXT NOT NULL,
    ts       TEXT NOT NULL,
    price    REAL,
    currency TEXT,
    PRIMARY KEY (ad_id, ts)
);

CREATE TABLE IF NOT EXISTS ad_details (
    id               TEXT PRIMARY KEY,
    fetched_at       TEXT,
    state            TEXT,
    posted           TEXT,
    reseted          TEXT,
    expire           TEXT,
    views_total      INTEGER,
    views_today      INTEGER,
    offer_type       TEXT,
    author_type      TEXT,
    rooms            TEXT,
    rooms_n          INTEGER,
    area             REAL,
    area_unit        TEXT,
    floor            INTEGER,
    floors_total     INTEGER,
    housing_stock    TEXT,
    living_room      TEXT,
    developer        TEXT,
    price_per_m2     REAL,
    region           TEXT,
    city             TEXT,
    district         TEXT,
    street           TEXT,
    house            TEXT,
    owner_login      TEXT,
    owner_id         TEXT,
    owner_registered TEXT,
    owner_is_business INTEGER,
    owner_verified   INTEGER,
    phones           TEXT,
    photos           TEXT,
    photos_count     INTEGER,
    description      TEXT,
    features_json    TEXT,
    terms_flag       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS det_district ON ad_details(district);
CREATE INDEX IF NOT EXISTS det_rooms    ON ad_details(rooms_n);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile    TEXT,
    started    TEXT,
    finished   TEXT,
    total      INTEGER,
    fetched    INTEGER,
    added      INTEGER,
    gone       INTEGER,
    price_down INTEGER,
    price_up   INTEGER
);

CREATE TABLE IF NOT EXISTS flags (
    ad_id    TEXT PRIMARY KEY,
    favorite INTEGER DEFAULT 0,
    hidden   INTEGER DEFAULT 0,
    note     TEXT DEFAULT '',
    updated  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    ip    TEXT,
    name  TEXT,
    email TEXT,
    body  TEXT,
    sent  INTEGER DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS cad_buildings (
    code        TEXT PRIMARY KEY,
    address     TEXT,
    street      TEXT,
    house       TEXT,
    classifier  TEXT,
    year_built  INTEGER,
    floors      INTEGER,
    condition   TEXT,
    walls       TEXT,
    gas         TEXT,
    water       TEXT,
    sewer       TEXT,
    electrified TEXT,
    value_lei   INTEGER,
    updated     TEXT,
    props_json  TEXT,
    geometry    TEXT,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS cad_b_house ON cad_buildings(house);

CREATE TABLE IF NOT EXISTS cad_units (
    code       TEXT PRIMARY KEY,
    building   TEXT NOT NULL,
    address    TEXT,
    apt        TEXT,
    area       REAL,
    kind       TEXT,
    floor      INTEGER,
    wc         TEXT,
    bath       TEXT,
    last_floor INTEGER,
    value_lei  INTEGER
);
CREATE INDEX IF NOT EXISTS cad_u_building ON cad_units(building, floor);

CREATE TABLE IF NOT EXISTS cad_points (
    point      TEXT PRIMARY KEY,
    buildings  TEXT,
    status     TEXT,
    probes     INTEGER,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS ad_cadastre (
    ad_id      TEXT PRIMARY KEY,
    building   TEXT,
    match      TEXT,
    tolerance  REAL,
    n          INTEGER,
    value_min  INTEGER,
    value_med  INTEGER,
    value_max  INTEGER,
    units      TEXT,
    matched_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


MIGRATIONS = [("ad_details", "terms_flag", "INTEGER DEFAULT 0")]


SECTOR_RENAMES = {"Окраина": "Пригород"}


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.commit()
    for old, new in SECTOR_RENAMES.items():
        if conn.execute("SELECT 1 FROM ad_details WHERE district = ? LIMIT 1", (old,)).fetchone():
            conn.execute(
                "UPDATE ad_details SET district = ?, features_json = replace(features_json, ?, ?) "
                "WHERE district = ?", (new, f'"Сектор": "{old}"', f'"Сектор": "{new}"', old))
            conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def stats(conn: sqlite3.Connection) -> dict:
    out = {}
    for profile, count in conn.execute(
        "SELECT profile, COUNT(*) c FROM ads WHERE gone_at IS NULL GROUP BY profile"
    ):
        out[profile] = count
    out["_details"] = conn.execute("SELECT COUNT(*) c FROM ad_details").fetchone()["c"]
    out["_history"] = conn.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]
    out["_gone"] = conn.execute("SELECT COUNT(*) c FROM ads WHERE gone_at IS NOT NULL").fetchone()["c"]
    return out


if __name__ == "__main__":
    with connect() as conn:
        print(f"база: {DB_PATH}")
        for key, value in stats(conn).items():
            print(f"  {key}: {value}")
