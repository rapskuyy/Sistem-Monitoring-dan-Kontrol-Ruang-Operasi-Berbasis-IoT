"""
MQTT logger: subscribe ke topic ESP32, simpan ke SQLite + CSV.

Jalankan (biarkan terus berjalan selama perekaman data):
    python logger.py
    python logger.py --scenario box_sealed     # beri label skenario

Konfigurasi bisa diubah lewat environment variable:
    MQTT_BROKER, MQTT_PORT, DEVICE_ID, SCENARIO, OR_DATA_DIR
"""

import argparse
import csv
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

import db

log = logging.getLogger("logger")

WIB = timezone(timedelta(hours=7))

BROKER_HOST = os.getenv("MQTT_BROKER", "broker.hivemq.com")
BROKER_PORT = int(os.getenv("MQTT_PORT", "1883"))
DEVICE_ID = os.getenv("DEVICE_ID", "operation_room_simulation")

BASE_TOPIC = f"ruangop/{DEVICE_ID}"
TOPIC_TELEMETRY = f"{BASE_TOPIC}/telemetry"
TOPIC_EVENTS = f"{BASE_TOPIC}/events"
TOPIC_STATUS = f"{BASE_TOPIC}/status"

TELEMETRY_REQUIRED = (
    "tempIn", "tempOut", "presIn", "presOut",
    "deltaPres", "deltaTemp", "blower", "heater",
)

counters = {"telemetry": 0, "events": 0, "status": 0, "rejected": 0}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def pick_timestamp(payload: dict):
    """Pakai timestamp ESP32 jika valid; jika tidak, pakai jam server (WIB)."""
    ts = payload.get("timestamp")
    if isinstance(ts, str) and ts and ts != "TIME_NOT_SYNCED":
        return ts, "device"
    return datetime.now(WIB).isoformat(timespec="seconds"), "server"


def num(payload: dict, key: str, cast=float, required: bool = False):
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"field '{key}' tidak ada")
        return None
    return cast(value)


def append_csv(path, header, row) -> None:
    """Append satu baris ke CSV. File dibuka-tutup tiap tulis supaya tidak
    terkunci; jika gagal (mis. dibuka Excel di Windows) data tetap aman di
    SQLite."""
    try:
        is_new = (not path.exists()) or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(header)
            writer.writerow(row)
    except OSError as e:
        log.warning("Gagal menulis CSV %s (%s). Data tetap tersimpan di database.",
                    path.name, e)


# ---------------------------------------------------------------------------
# Handler per topic
# ---------------------------------------------------------------------------

def handle_telemetry(conn, payload: dict, scenario: str) -> None:
    for key in TELEMETRY_REQUIRED:
        if key not in payload:
            raise ValueError(f"field '{key}' tidak ada")

    ts, src = pick_timestamp(payload)

    rec = {
        "received_at": db.utc_now_str(),
        "timestamp": ts,
        "ts_source": src,
        "elapsed_ms": num(payload, "elapsedMs", int),
        "temp_in": num(payload, "tempIn", float, True),
        "temp_out": num(payload, "tempOut", float, True),
        "delta_temp": num(payload, "deltaTemp", float, True),
        "pres_in": num(payload, "presIn", float, True),
        "pres_out": num(payload, "presOut", float, True),
        "delta_pres": num(payload, "deltaPres", float, True),
        "mq135_raw": num(payload, "mq135Raw", int),
        "mq135_mv": num(payload, "mq135mV", float),
        "blower": int(bool(payload["blower"])),
        "heater": int(bool(payload["heater"])),
        "pressure_status": payload.get("pressureStatus"),
        "temperature_status": payload.get("temperatureStatus"),
        "air_status": payload.get("airStatus"),
        "data_origin": "measured",
        "scenario": scenario,
    }

    conn.execute(
        """INSERT INTO telemetry (
               received_at, timestamp, ts_source, elapsed_ms,
               temp_in, temp_out, delta_temp, pres_in, pres_out, delta_pres,
               mq135_raw, mq135_mv, blower, heater,
               pressure_status, temperature_status, air_status,
               data_origin, scenario)
           VALUES (
               :received_at, :timestamp, :ts_source, :elapsed_ms,
               :temp_in, :temp_out, :delta_temp, :pres_in, :pres_out, :delta_pres,
               :mq135_raw, :mq135_mv, :blower, :heater,
               :pressure_status, :temperature_status, :air_status,
               :data_origin, :scenario)""",
        rec,
    )
    conn.commit()

    append_csv(
        db.CSV_TELEMETRY_PATH,
        [c for c, _ in db.TELEMETRY_CSV_COLUMNS],
        db.csv_row(rec, db.TELEMETRY_CSV_COLUMNS),
    )

    counters["telemetry"] += 1
    if counters["telemetry"] % 60 == 1:
        log.info(
            "Telemetry #%d | dP=%.2f hPa dT=%.2f C MQ135=%s blower=%d heater=%d",
            counters["telemetry"], rec["delta_pres"], rec["delta_temp"],
            rec["mq135_raw"], rec["blower"], rec["heater"],
        )


def handle_event(conn, payload: dict) -> None:
    actuator = payload.get("actuator")
    state = payload.get("state")
    if not actuator or not state:
        raise ValueError("event tanpa 'actuator' atau 'state'")

    ts, src = pick_timestamp(payload)

    rec = {
        "received_at": db.utc_now_str(),
        "timestamp": ts,
        "ts_source": src,
        "elapsed_ms": num(payload, "elapsedMs", int),
        "actuator": str(actuator),
        "state": str(state).upper(),
        "reason": payload.get("reason"),
        "value": num(payload, "value", float),
        "threshold": num(payload, "threshold", float),
    }

    conn.execute(
        """INSERT INTO actuator_events (
               received_at, timestamp, ts_source, elapsed_ms,
               actuator, state, reason, value, threshold)
           VALUES (
               :received_at, :timestamp, :ts_source, :elapsed_ms,
               :actuator, :state, :reason, :value, :threshold)""",
        rec,
    )
    conn.commit()

    append_csv(
        db.CSV_EVENTS_PATH,
        [c for c, _ in db.EVENTS_CSV_COLUMNS],
        db.csv_row(rec, db.EVENTS_CSV_COLUMNS),
    )

    counters["events"] += 1
    log.info("EVENT %s -> %s (%s)", rec["actuator"], rec["state"], rec["reason"])


def handle_status(conn, text: str, retained: bool) -> None:
    status = text.strip().lower()
    if status not in ("online", "offline"):
        raise ValueError(f"status tidak dikenal: {status!r}")

    conn.execute(
        "INSERT INTO device_status (received_at, status, retained) VALUES (?, ?, ?)",
        (db.utc_now_str(), status, int(retained)),
    )
    conn.commit()
    counters["status"] += 1
    log.info("Device status: %s%s", status, " (retained)" if retained else "")


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------

def make_on_message(conn, scenario: str):
    def on_message(client, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8")
            if msg.topic == TOPIC_TELEMETRY:
                handle_telemetry(conn, json.loads(raw), scenario)
            elif msg.topic == TOPIC_EVENTS:
                handle_event(conn, json.loads(raw))
            elif msg.topic == TOPIC_STATUS:
                handle_status(conn, raw, bool(msg.retain))
        except Exception as e:  # satu pesan buruk tidak boleh menghentikan logger
            counters["rejected"] += 1
            log.warning("Pesan di %s ditolak: %s", msg.topic, e)
    return on_message


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code.is_failure:
        log.error("Gagal terhubung ke broker: %s", reason_code)
        return
    log.info("Terhubung ke %s:%s", BROKER_HOST, BROKER_PORT)
    client.subscribe([(TOPIC_TELEMETRY, 0), (TOPIC_EVENTS, 1), (TOPIC_STATUS, 1)])
    log.info("Subscribe: %s | %s | %s", TOPIC_TELEMETRY, TOPIC_EVENTS, TOPIC_STATUS)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    log.warning("Terputus dari broker (%s). Mencoba menyambung ulang...", reason_code)


def main() -> None:
    parser = argparse.ArgumentParser(description="MQTT -> SQLite/CSV logger")
    parser.add_argument(
        "--scenario",
        default=os.getenv("SCENARIO", "live_logging"),
        help="label skenario pada tiap baris data (mis. normal, box_sealed, sensor_luar_ditutup)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db.init_db()
    conn = db.connect()

    log.info("Database : %s", db.DB_PATH)
    log.info("CSV      : %s", db.CSV_TELEMETRY_PATH)
    log.info("Skenario : %s", args.scenario)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"or-logger-{uuid.uuid4().hex[:8]}",
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = make_on_message(conn, args.scenario)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    # connect_async + loop_forever: tetap mencoba sampai berhasil walau
    # internet belum tersedia saat logger dijalankan.
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=30)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log.info("Dihentikan. Ringkasan: %s", counters)
    finally:
        client.disconnect()
        conn.close()


if __name__ == "__main__":
    main()
