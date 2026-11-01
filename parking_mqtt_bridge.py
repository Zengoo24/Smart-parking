#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MQTT bridge cho mo hinh bai do xe Raspberry Pi 4B.

- Chi doc JSON/CSV, khong truy cap GPIO va khong dieu khien servo.
- Khong publish UID RFID hoac thong tin dinh danh nguoi dat cho.
- Doc parking_history.csv tu byte offset da luu, khong doc lai ca file moi chu ky.
- Khong publish trang thai rong/UNKNOWN gia khi JSON nguon tam thoi khong doc duoc.
- Cursor luon duoc luu duoi dang dictionary JSON voi cac truong offset, inode, header.
"""

import copy
import csv
import io
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

# -----------------------------------------------------------------------------
# Duong dan du lieu va cau hinh MQTT
# -----------------------------------------------------------------------------
HOME = Path.home()
LIVE_FILE = HOME / "parking_live.json"
ACTIVE_FILE = HOME / "parking_data.json"
HISTORY_FILE = HOME / "parking_history.csv"
RESERVATIONS_FILE = HOME / "parking_reservations.json"
CURSOR_FILE = HOME / "parking_mqtt_cursor.json"

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1").strip() or "127.0.0.1"
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
PUBLISH_INTERVAL = max(1.0, float(os.getenv("MQTT_PUBLISH_INTERVAL", "3")))
HEARTBEAT_SECONDS = 30.0
ROOT_TOPIC = os.getenv("MQTT_ROOT_TOPIC", "parking/pi4").strip("/") or "parking/pi4"
SLOTS = ["P1", "P2", "P3", "P4", "P5"]

# Ten vi tri tren bo dieu khien -> ten vi tri tren Digital Twin.
# Giu dung quy uoc da dung trong bridge truoc do.
DISPLAY_SOURCE = {"P1": "P1", "P2": "P2", "P3": "P3", "P4": "P4", "P5": "P5"}
CSV_SCAN_CHUNK = 4096


# -----------------------------------------------------------------------------
# Ham tien ich
# -----------------------------------------------------------------------------
def log(message):
    print("[parking-mqtt] {}".format(message), flush=True)


def read_json(path, default=None):
    """Doc JSON. Tra ve default neu tep dang duoc ghi, thieu, hoac JSON loi."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        log("Tam thoi khong doc duoc {}: {}".format(path.name, exc))
        return default


def atomic_json(path, data):
    """Ghi JSON qua tep tam, sau do doi ten de han che tep JSON nua chung."""
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def json_payload(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def safe_int(value, default=0):
    """Chuyen int an toan; None, chuoi rong, NaN/Infinity hoac chuoi sai -> default."""
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def count_active_sessions():
    data = read_json(ACTIVE_FILE, None)
    if not isinstance(data, dict):
        return None
    active = data.get("active")
    if isinstance(active, dict):
        data = active
    return sum(1 for value in data.values() if isinstance(value, dict))


# -----------------------------------------------------------------------------
# Du lieu dat cho va trang thai bai xe
# -----------------------------------------------------------------------------
def reservation_summary():
    data = read_json(RESERVATIONS_FILE, None)
    if not isinstance(data, dict):
        return None
    rows = data.get("reservations")
    if not isinstance(rows, list):
        return None

    counts = {
        "BOOKED": 0,
        "ARRIVED": 0,
        "IN_USE": 0,
        "COMPLETED": 0,
        "CANCELLED": 0,
        "EXPIRED": 0,
    }
    now = time.time()
    active_count = 0
    held_slots = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "BOOKED").strip().upper()
        if status in counts:
            counts[status] += 1

        try:
            start_ts = float(row.get("start_ts") or 0)
            end_ts = float(row.get("end_ts") or 0)
        except (TypeError, ValueError, OverflowError):
            start_ts, end_ts = 0.0, 0.0

        physical_slot = str(row.get("slot") or "").strip()
        if status in ("BOOKED", "ARRIVED", "IN_USE") and end_ts > now:
            active_count += 1
            if start_ts <= now < end_ts and physical_slot in SLOTS:
                held_slots[physical_slot] = status

    # Khong gui UID, ten hoac thong tin ca nhan qua MQTT.
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(rows),
        "active": active_count,
        "by_status": counts,
        "held_physical_slots": held_slots,
    }


def make_status_payload():
    """Tao status; tra None neu du lieu nguon tam thoi khong hop le."""
    live = read_json(LIVE_FILE, None)
    if not isinstance(live, dict):
        return None

    raw_slots = live.get("slots")
    if not isinstance(raw_slots, dict) or any(slot not in raw_slots for slot in SLOTS):
        log("parking_live.json chua co day du slots; bo qua status lan nay.")
        return None

    raw_gates = live.get("gates", {})
    if not isinstance(raw_gates, dict):
        log("Truong gates khong hop le; bo qua status lan nay.")
        return None

    active_sessions = count_active_sessions()
    reservations = reservation_summary()
    if active_sessions is None or reservations is None:
        log("Thieu JSON hop le cho sessions/reservations; bo qua status lan nay.")
        return None

    physical_slots = {}
    for slot in SLOTS:
        state = str(raw_slots.get(slot) or "UNKNOWN").strip().upper()
        physical_slots[slot] = state if state in ("FREE", "OCCUPIED") else "UNKNOWN"

    held_physical_slots = reservations.get("held_physical_slots", {})
    display_slots = {}
    for display_name, physical_name in DISPLAY_SOURCE.items():
        state = physical_slots[physical_name]
        if state == "FREE" and physical_name in held_physical_slots:
            state = "RESERVED"
        display_slots[display_name] = state

    gates = {}
    for gate_name in ("entry", "exit"):
        state = str(raw_gates.get(gate_name) or "UNKNOWN").strip().upper()
        gates[gate_name] = state if state in ("OPEN", "CLOSED") else "UNKNOWN"

    sensor_ready = all(state in ("FREE", "OCCUPIED") for state in physical_slots.values())
    return {
        "device": "raspberrypi4b",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slots": display_slots,
        "free_count": sum(state == "FREE" for state in display_slots.values()) if sensor_ready else None,
        "occupied_count": sum(state == "OCCUPIED" for state in display_slots.values()) if sensor_ready else None,
        "reserved_count": sum(state == "RESERVED" for state in display_slots.values()) if sensor_ready else None,
        "sensor_ready": sensor_ready,
        "gates": gates,
        "active_sessions": active_sessions,
    }


# -----------------------------------------------------------------------------
# CSV cursor: schema moi luon la {"offset": ..., "inode": ..., "header": ...}
# Van doc duoc cursor cu: so nguyen, hoac dict co truong byte_offset.
# -----------------------------------------------------------------------------
def make_cursor(offset, inode, header):
    return {"offset": int(offset), "inode": int(inode), "header": str(header)}


def last_complete_line_offset(file_handle, size):
    """Tim byte ngay sau dong ket thuc bang newline cuoi cung trong file."""
    pos = size
    while pos > 0:
        start = max(0, pos - CSV_SCAN_CHUNK)
        file_handle.seek(start)
        block = file_handle.read(pos - start)
        newline_index = block.rfind(b"\n")
        if newline_index >= 0:
            return start + newline_index + 1
        pos = start
    return 0


def _is_line_boundary(file_handle, offset, header_end):
    if offset == header_end:
        return True
    if offset < header_end:
        return False
    file_handle.seek(offset - 1)
    return file_handle.read(1) == b"\n"


def normalise_cursor(cursor_value):
    """Chuẩn hóa kiểu cursor sơ bộ trước khi kiểm tra metadata CSV.

    - Dict có offset/byte_offset: giữ lại các metadata nếu có.
    - Số nguyên: chuyển thành dict có khóa offset.
    - Kiểu dữ liệu khác: trả None.
    """
    if isinstance(cursor_value, dict):
        if "offset" in cursor_value or "byte_offset" in cursor_value:
            return dict(cursor_value)
        return None
    if isinstance(cursor_value, int) and not isinstance(cursor_value, bool):
        return {"offset": cursor_value}
    return None


def validate_cursor(cursor_value, file_handle, stat_result, header, header_end):
    """Xác minh cursor theo file CSV và trả về dict schema chuẩn.

    Schema chuẩn: {"offset": int, "inode": int, "header": str}.
    Giá trị int hoặc dict cũ thiếu metadata được nâng cấp an toàn.
    """
    normalized = normalise_cursor(cursor_value)
    if normalized is None:
        return None, False

    raw_offset = normalized.get("offset", normalized.get("byte_offset"))
    try:
        offset = int(raw_offset)
        inode = int(normalized.get("inode", stat_result.st_ino))
        old_header = str(normalized.get("header", header))
    except (TypeError, ValueError, OverflowError):
        return None, False

    if offset < header_end or offset > stat_result.st_size:
        return None, False
    if inode != stat_result.st_ino or old_header != header:
        return None, False
    try:
        if not _is_line_boundary(file_handle, offset, header_end):
            return None, False
    except OSError:
        return None, False

    canonical = make_cursor(offset, stat_result.st_ino, header)
    needs_migration = (
        not isinstance(cursor_value, dict)
        or "offset" not in cursor_value
        or "inode" not in cursor_value
        or "header" not in cursor_value
    )
    return canonical, needs_migration


def get_history_events():
    """Tra (events, next_cursor).

    next_cursor luon la dictionary hoac None. Neu cursor khong ton tai/sai, chi danh
    dau cuoi dong hoan chinh hien tai de khong gui lai toan bo lich su cu.
    Dong CSV dang ghi do duoc giu lai cho chu ky sau.
    """
    try:
        with HISTORY_FILE.open("rb") as file_handle:
            stat_result = os.fstat(file_handle.fileno())
            size = stat_result.st_size
            header_bytes = file_handle.readline()
            if not header_bytes.endswith(b"\n"):
                log("Header CSV chua hoan chinh; thu lai o chu ky sau.")
                return [], None
            try:
                header = header_bytes.decode("utf-8-sig").rstrip("\r\n")
            except UnicodeDecodeError:
                log("Header CSV khong phai UTF-8; thu lai o chu ky sau.")
                return [], None
            if not header.strip():
                log("Header CSV rong; thu lai o chu ky sau.")
                return [], None
            header_end = file_handle.tell()

            raw_cursor = read_json(CURSOR_FILE, None)
            cursor, needs_migration = validate_cursor(
                raw_cursor, file_handle, stat_result, header, header_end
            )

            if cursor is None:
                # Lan dau chay, file CSV bi thay the, hoac cursor sai: khong phat lai lich su.
                end_offset = max(header_end, last_complete_line_offset(file_handle, size))
                return [], make_cursor(end_offset, stat_result.st_ino, header)

            old_offset = cursor["offset"]
            file_handle.seek(old_offset)
            new_bytes = file_handle.read()
            if not new_bytes:
                # Neu cursor cu can migrate schema, luu dict moi ca khi chua co event.
                return ([], cursor) if needs_migration else ([], None)

            last_newline = new_bytes.rfind(b"\n")
            if last_newline < 0:
                # Chua co dong day du; KHONG tang cursor.
                return ([], cursor) if needs_migration else ([], None)

            complete_bytes = new_bytes[:last_newline + 1]
            next_offset = old_offset + len(complete_bytes)
            if not complete_bytes:
                return ([], cursor) if needs_migration else ([], None)

            try:
                new_text = complete_bytes.decode("utf-8")
            except UnicodeDecodeError:
                log("Du lieu CSV chua doc duoc UTF-8; thu lai o chu ky sau.")
                return [], None

            events = []
            reader_stream = io.StringIO(header + "\n" + new_text, newline="")
            try:
                reader = csv.DictReader(reader_stream)
                for row in reader:
                    if not row or all(value is None for value in row.values()):
                        continue
                    event_name = str(row.get("event", row.get("su_kien", "EVENT")) or "EVENT").strip()
                    physical_slot = str(row.get("slot", row.get("vi_tri", "")) or "").strip()
                    display_slot = next(
                        (display for display, physical in DISPLAY_SOURCE.items() if physical == physical_slot),
                        physical_slot,
                    )
                    timestamp = str(row.get("timestamp", row.get("thoi_gian", "")) or "").strip()
                    seconds = row.get("parking_seconds", row.get("thoi_gian_giay", 0))
                    fee = row.get("fee_vnd", row.get("phi_vnd", 0))
                    events.append({
                        "event": event_name,
                        "slot": display_slot,
                        "timestamp": timestamp,
                        "parking_seconds": safe_int(seconds, 0),
                        "fee_vnd": safe_int(fee, 0),
                    })
            except (csv.Error, TypeError, ValueError) as exc:
                log("Loi doc CSV {}: se thu lai o chu ky sau.".format(exc))
                return [], None

            return events, make_cursor(next_offset, stat_result.st_ino, header)

    except (OSError, UnicodeError) as exc:
        log("Khong doc duoc CSV lich su: {}; se thu lai.".format(exc))
        return [], None


def cursor_for_save(next_cursor):
    """Chuẩn hóa và xác minh cursor trước khi ghi; file luôn chứa dictionary chuẩn."""
    normalized = normalise_cursor(next_cursor)
    if normalized is None:
        return None

    try:
        offset = int(normalized.get("offset", normalized.get("byte_offset")))
    except (TypeError, ValueError, OverflowError):
        return None

    try:
        with HISTORY_FILE.open("rb") as file_handle:
            stat_result = os.fstat(file_handle.fileno())
            header_bytes = file_handle.readline()
            if not header_bytes.endswith(b"\n"):
                return None
            header = header_bytes.decode("utf-8-sig").rstrip("\r\n")
            header_end = file_handle.tell()

            if offset < header_end or offset > stat_result.st_size:
                return None
            if not _is_line_boundary(file_handle, offset, header_end):
                return None

            # Neu metadata cu ton tai, phai trung voi file CSV hien tai.
            try:
                old_inode = normalized.get("inode")
                if old_inode is not None and int(old_inode) != stat_result.st_ino:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
            old_header = normalized.get("header")
            if old_header is not None and str(old_header) != header:
                return None

            return make_cursor(offset, stat_result.st_ino, header)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return None


# -----------------------------------------------------------------------------
# MQTT client va publish
# -----------------------------------------------------------------------------
def make_client():
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="parking-pi4-bridge",
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id="parking-pi4-bridge")

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.will_set("{}/availability".format(ROOT_TOPIC), "offline", qos=1, retain=True)

    def on_connect(client_obj, userdata, flags, reason_code, properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code == 0:
            log("Da ket noi MQTT {}:{}".format(MQTT_HOST, MQTT_PORT))
            client_obj.publish("{}/availability".format(ROOT_TOPIC), "online", qos=1, retain=True)
        else:
            log("Ket noi MQTT that bai: {}".format(reason_code))

    def on_disconnect(client_obj, userdata, disconnect_flags, reason_code=None, properties=None):
        log("Mat ket noi MQTT; Paho se thu ket noi lai.")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def confirmed_publish(client, topic, payload, qos=1, retain=False, timeout=2.0):
    """Doi xac nhan publish. Tra False neu broker chua xac nhan trong timeout."""
    try:
        info = client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        info.wait_for_publish(timeout=timeout)
        if hasattr(info, "is_published"):
            return bool(info.is_published())
        return True
    except (RuntimeError, ValueError, TypeError, OSError) as exc:
        log("Publish {} that bai: {}".format(topic, exc))
        return False


def main():
    client = make_client()
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=45)
    client.loop_start()

    last_status = None
    last_reservations = None
    last_heartbeat = 0.0

    log("MQTT Bridge dang chay; khong truy cap GPIO.")
    log("Status: {}/status".format(ROOT_TOPIC))
    log("Events: {}/events".format(ROOT_TOPIC))
    log("Reservations: {}/reservations".format(ROOT_TOPIC))

    try:
        while True:
            cycle_started = time.monotonic()
            try:
                status = make_status_payload()
                reservation_data = reservation_summary()

                # JSON loi/ghi do: giu lai ban tin retained cu, khong publish du lieu rong.
                if status is not None:
                    status_compare = copy.deepcopy(status)
                    status_compare.pop("updated_at", None)
                    heartbeat_due = cycle_started - last_heartbeat >= HEARTBEAT_SECONDS
                    if status_compare != last_status or heartbeat_due:
                        if confirmed_publish(
                            client,
                            "{}/status".format(ROOT_TOPIC),
                            json_payload(status),
                            qos=1,
                            retain=True,
                        ):
                            last_status = copy.deepcopy(status_compare)
                            last_heartbeat = time.monotonic()

                if reservation_data is not None:
                    reservation_publish = copy.deepcopy(reservation_data)
                    reservation_publish.pop("held_physical_slots", None)
                    reservation_compare = copy.deepcopy(reservation_publish)
                    reservation_compare.pop("updated_at", None)
                    if reservation_compare != last_reservations:
                        if confirmed_publish(
                            client,
                            "{}/reservations".format(ROOT_TOPIC),
                            json_payload(reservation_publish),
                            qos=1,
                            retain=True,
                        ):
                            last_reservations = copy.deepcopy(reservation_compare)

                # Doc CSV khi da ket noi. Cursor chi duoc luu sau khi publish event thanh cong.
                if client.is_connected():
                    events, next_cursor = get_history_events()
                    if next_cursor is not None:
                        all_events_sent = True
                        for event in events:
                            if not confirmed_publish(
                                client,
                                "{}/events".format(ROOT_TOPIC),
                                json_payload(event),
                                qos=1,
                                retain=False,
                            ):
                                all_events_sent = False
                                log("Event chua duoc broker xac nhan; se thu lai o chu ky sau.")
                                break

                        if all_events_sent:
                            # Khong bao gio atomic_json so nguyen; cursor luon la dict schema chuan.
                            canonical_cursor = cursor_for_save(next_cursor)
                            if canonical_cursor is None:
                                log("Cursor khong hop le; khong cap nhat cursor file.")
                            else:
                                try:
                                    atomic_json(CURSOR_FILE, canonical_cursor)
                                except OSError as exc:
                                    log("Khong luu duoc cursor: {}; event co the duoc gui lap lai.".format(exc))

            except Exception as exc:
                log("Loi trong chu ky bridge: {}: {}".format(type(exc).__name__, exc))

            elapsed = time.monotonic() - cycle_started
            time.sleep(max(0.0, PUBLISH_INTERVAL - elapsed))

    except KeyboardInterrupt:
        log("Nhan Ctrl+C; dang dung MQTT Bridge.")
    finally:
        try:
            if client.is_connected():
                confirmed_publish(
                    client,
                    "{}/availability".format(ROOT_TOPIC),
                    "offline",
                    qos=1,
                    retain=True,
                    timeout=1.0,
                )
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
