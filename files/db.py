"""Modul database bersama untuk logger.py dan api.py (SQLite)."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("OR_DATA_DIR", BASE_DIR / "data"))

DB_PATH = DATA_DIR / "operating_room.db"
CSV_TELEMETRY_PATH = DATA_DIR / "telemetry_log.csv"
CSV_EVENTS_PATH = DATA_DIR / "actuator_events_log.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at         TEXT    NOT NULL,   -- waktu server menerima (UTC)
    timestamp           TEXT    NOT NULL,   -- waktu pembacaan (WIB, ISO 8601)
    ts_source           TEXT    NOT NULL,   -- 'device' (NTP ESP32) / 'server'
    elapsed_ms          INTEGER,
    temp_in             REAL,
    temp_out            REAL,
    delta_temp          REAL,
    pres_in             REAL,
    pres_out            REAL,
    delta_pres          REAL,
    mq135_raw           INTEGER,
    mq135_mv            REAL,
    blower              INTEGER NOT NULL,
    heater              INTEGER NOT NULL,
    pressure_status     TEXT,
    temperature_status  TEXT,
    air_status          TEXT,
    data_origin         TEXT    NOT NULL DEFAULT 'measured',
    scenario            TEXT    NOT NULL DEFAULT 'live_logging'
);
CREATE INDEX IF NOT EXISTS idx_telemetry_received ON telemetry(received_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_scenario ON telemetry(scenario);

CREATE TABLE IF NOT EXISTS actuator_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    ts_source   TEXT NOT NULL,
    elapsed_ms  INTEGER,
    actuator    TEXT NOT NULL,              -- 'blower' / 'heater'
    state       TEXT NOT NULL,              -- 'ON' / 'OFF'
    reason      TEXT,
    value       REAL,
    threshold   REAL
);
CREATE INDEX IF NOT EXISTS idx_events_received ON actuator_events(received_at);

CREATE TABLE IF NOT EXISTS device_status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    status      TEXT NOT NULL,              -- 'online' / 'offline'
    retained    INTEGER NOT NULL DEFAULT 0
);
"""

# (nama kolom CSV, nama kolom database)
# Nama kolom CSV mengikuti dataset lama (operating_room_dataset_augmented.csv),
# ditambah tiga kolom status di bagian akhir sebelum data_origin/scenario.
TELEMETRY_CSV_COLUMNS = [
    ("timestamp_local", "timestamp"),
    ("elapsed_ms", "elapsed_ms"),
    ("tempIn_C", "temp_in"),
    ("tempOut_C", "temp_out"),
    ("presIn_hPa", "pres_in"),
    ("presOut_hPa", "pres_out"),
    ("deltaPres_hPa", "delta_pres"),
    ("deltaTemp_C", "delta_temp"),
    ("mq135Raw", "mq135_raw"),
    ("mq135Voltage_mV", "mq135_mv"),
    ("blower", "blower"),
    ("heater", "heater"),
    ("airStatus", "air_status"),
    ("pressureStatus", "pressure_status"),
    ("temperatureStatus", "temperature_status"),
    ("data_origin", "data_origin"),
    ("scenario", "scenario"),
]

EVENTS_CSV_COLUMNS = [
    ("timestamp_local", "timestamp"),
    ("elapsed_ms", "elapsed_ms"),
    ("actuator", "actuator"),
    ("state", "state"),
    ("reason", "reason"),
    ("value", "value"),
    ("threshold", "threshold"),
]


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: API membaca lewat thread pool; tiap koneksi
    # tetap hanya dipakai satu request pada satu waktu.
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # logger menulis, API membaca
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def csv_row(row, columns) -> list:
    """Ubah satu baris (dict / sqlite3.Row) menjadi list nilai untuk CSV."""
    out = []
    for _, col in columns:
        v = row[col]
        out.append("" if v is None else v)
    return out
