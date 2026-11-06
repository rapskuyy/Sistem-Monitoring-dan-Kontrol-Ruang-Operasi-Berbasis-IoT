"""
API baca-saja untuk dashboard: riwayat, event aktuator, statistik, export CSV.

Jalankan:
    uvicorn api:app --host 0.0.0.0 --port 8000

Dokumentasi interaktif: http://localhost:8000/docs
Jika ada folder `static/` berisi index.html, dashboard otomatis dilayani di
http://localhost:8000/ (tidak perlu urus CORS / mixed content).
"""

import csv
import io
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import db

app = FastAPI(title="Operating Room IoT API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # prototipe lokal; batasi jika dipublikasikan
    allow_methods=["GET"],
    allow_headers=["*"],
)

db.init_db()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def telemetry_to_json(r) -> dict:
    """Bentuk sama dengan payload MQTT, supaya dashboard bisa memakai ulang
    fungsi yang sama untuk data live maupun riwayat."""
    return {
        "id": r["id"],
        "timestamp": r["timestamp"],
        "elapsedMs": r["elapsed_ms"],
        "tempIn": r["temp_in"],
        "tempOut": r["temp_out"],
        "deltaTemp": r["delta_temp"],
        "presIn": r["pres_in"],
        "presOut": r["pres_out"],
        "deltaPres": r["delta_pres"],
        "mq135Raw": r["mq135_raw"],
        "mq135mV": r["mq135_mv"],
        "blower": bool(r["blower"]),
        "heater": bool(r["heater"]),
        "pressureStatus": r["pressure_status"],
        "temperatureStatus": r["temperature_status"],
        "airStatus": r["air_status"],
        "scenario": r["scenario"],
    }


def event_to_json(r) -> dict:
    return {
        "id": r["id"],
        "timestamp": r["timestamp"],
        "elapsedMs": r["elapsed_ms"],
        "actuator": r["actuator"],
        "state": r["state"],
        "reason": r["reason"],
        "value": r["value"],
        "threshold": r["threshold"],
    }


def build_where(minutes: Optional[int], scenario: Optional[str] = None):
    clauses, params = [], []
    if minutes is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)
                  ).strftime("%Y-%m-%dT%H:%M:%S")
        clauses.append("received_at >= ?")
        params.append(cutoff)
    if scenario:
        clauses.append("scenario = ?")
        params.append(scenario)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def csv_stream(header, cursor_rows, conn):
    """Generator CSV; koneksi ditutup setelah selesai."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    try:
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in cursor_rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/latest")
def latest():
    conn = db.connect()
    try:
        r = conn.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if r is None:
        raise HTTPException(404, "Belum ada data telemetry")
    return telemetry_to_json(r)


@app.get("/api/history")
def history(
    minutes: int = Query(60, ge=1, le=60 * 24 * 30),
    limit: int = Query(600, ge=10, le=5000),
    scenario: Optional[str] = None,
):
    """Riwayat telemetry (urut waktu naik). Jika data lebih banyak dari
    `limit`, diambil tiap n-baris (downsampling) agar grafik tetap ringan."""
    where, params = build_where(minutes, scenario)
    conn = db.connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM telemetry {where}", params).fetchone()[0]
        step = max(1, math.ceil(total / limit))
        sql = f"SELECT * FROM telemetry {where}"
        sql += (" AND" if where else " WHERE") + " id % ? = 0 ORDER BY id"
        rows = conn.execute(sql, params + [step]).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "step": step,
        "count": len(rows),
        "data": [telemetry_to_json(r) for r in rows],
    }


@app.get("/api/events")
def events(
    limit: int = Query(50, ge=1, le=1000),
    actuator: Optional[str] = None,
):
    """Event ON/OFF aktuator terbaru (terbaru di atas)."""
    sql = "SELECT * FROM actuator_events"
    params = []
    if actuator:
        sql += " WHERE actuator = ?"
        params.append(actuator)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    conn = db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {"count": len(rows), "data": [event_to_json(r) for r in rows]}


@app.get("/api/stats")
def stats():
    """Ringkasan penyimpanan: bukti data tersimpan terus-menerus."""
    conn = db.connect()
    try:
        total, first, last = conn.execute(
            "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM telemetry"
        ).fetchone()
        by_scenario = {
            r[0]: r[1] for r in conn.execute(
                "SELECT scenario, COUNT(*) FROM telemetry GROUP BY scenario")
        }
        ev = [
            {"actuator": r[0], "state": r[1], "count": r[2]}
            for r in conn.execute(
                "SELECT actuator, state, COUNT(*) FROM actuator_events "
                "GROUP BY actuator, state ORDER BY actuator, state")
        ]
        st = conn.execute(
            "SELECT status, received_at FROM device_status ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return {
        "telemetryRows": total,
        "firstTimestamp": first,
        "lastTimestamp": last,
        "rowsByScenario": by_scenario,
        "actuatorEvents": ev,
        "deviceStatus": ({"status": st[0], "at": st[1]} if st else None),
    }


@app.get("/api/export.csv")
def export_telemetry_csv(
    minutes: Optional[int] = Query(None, ge=1),
    scenario: Optional[str] = None,
):
    """Unduh seluruh telemetry (atau N menit terakhir / satu skenario) sebagai CSV."""
    where, params = build_where(minutes, scenario)
    conn = db.connect()
    cur = conn.execute(f"SELECT * FROM telemetry {where} ORDER BY id", params)
    rows = (db.csv_row(r, db.TELEMETRY_CSV_COLUMNS) for r in cur)
    return StreamingResponse(
        csv_stream([c for c, _ in db.TELEMETRY_CSV_COLUMNS], rows, conn),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="telemetry_export.csv"'},
    )


@app.get("/api/events.csv")
def export_events_csv(minutes: Optional[int] = Query(None, ge=1)):
    """Unduh log aktivitas aktuator sebagai CSV."""
    where, params = build_where(minutes)
    conn = db.connect()
    cur = conn.execute(f"SELECT * FROM actuator_events {where} ORDER BY id", params)
    rows = (db.csv_row(r, db.EVENTS_CSV_COLUMNS) for r in cur)
    return StreamingResponse(
        csv_stream([c for c, _ in db.EVENTS_CSV_COLUMNS], rows, conn),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="actuator_events_export.csv"'},
    )


# Harus paling akhir agar tidak menimpa route /api/*
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
