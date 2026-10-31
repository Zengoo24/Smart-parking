"""Doc/ghi du lieu phien gui xe, lich su, dat cho va yeu cau UID."""

import csv
import json
import os
import time
from datetime import datetime

from .config import DATA_FILE, HISTORY_FILE, RESERVATIONS_FILE, UID_SCAN_RESULT_FILE

def save_active(active):
    temp = DATA_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as file:
        json.dump(active, file, ensure_ascii=False, indent=2)
        file.write("\n")

    os.replace(temp, DATA_FILE)

def load_active():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {}

        changed = False

        for record in data.values():
            if not isinstance(record, dict):
                continue

            defaults = {
                "entry_ts": None,
                "parked_ts": None,
                "start_ts": None,
                "slot": None,
                "last_slot": None,
                "awaiting_slot": True,
                "reservation_id": None,
                "reserved_slot": None,
                "reservation_display_slot": None,
            }

            for key, value in defaults.items():
                if key not in record:
                    record[key] = value
                    changed = True

        if changed:
            save_active(data)

        return data

    except (OSError, json.JSONDecodeError) as exc:
        print("Canh bao doc parking_data.json:", exc, flush=True)
        return {}

def log_history(event, uid, slot, start_ts, end_ts, fee):
    exists = os.path.exists(HISTORY_FILE)
    duration = 0

    if start_ts is not None and end_ts is not None:
        duration = max(0, int(float(end_ts) - float(start_ts)))

    with open(
        HISTORY_FILE,
        "a",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.writer(file)

        if not exists:
            writer.writerow([
                "event",
                "uid",
                "slot",
                "timestamp",
                "parking_seconds",
                "fee_vnd",
            ])

        writer.writerow([
            event,
            uid,
            slot or "",
            datetime.now().isoformat(timespec="seconds"),
            duration,
            fee,
        ])

def calculate_fee(parked_ts, end_ts):
    if parked_ts is None:
        return 0

    seconds = max(0.0, float(end_ts) - float(parked_ts))
    return max(1, int(math.ceil(seconds / 3600.0))) * FEE_PER_HOUR

def elapsed_text(start_ts):
    if start_ts is None:
        return "Chua do bai"

    total = max(0, int(time.time() - float(start_ts)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    return "{:02d}:{:02d}:{:02d}".format(
        hours, minutes, seconds
    )

def save_reservation_records(rows):
    temp_path = RESERVATIONS_FILE + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(
            {"reservations": rows},
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")

    os.replace(temp_path, RESERVATIONS_FILE)

def load_reservation_records():
    try:
        with open(RESERVATIONS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, dict):
        return []

    rows = data.get("reservations", [])

    if not isinstance(rows, list):
        return []

    now = time.time()
    changed = False

    # Chi BOOKED moi co the het han vi khong check-in.
    # ARRIVED/IN_USE khong bi huy boi bo dem no-show.
    for row in rows:
        if not isinstance(row, dict):
            continue

        if row.get("status") != "BOOKED":
            continue

        try:
            start_ts = float(row["start_ts"])
            end_ts = float(row["end_ts"])
        except (KeyError, TypeError, ValueError):
            continue

        reason = None

        if now >= end_ts:
            reason = "RESERVATION_ENDED"
        elif now >= start_ts + NO_SHOW_GRACE_SECONDS:
            reason = "NO_SHOW"

        if reason:
            row["status"] = "EXPIRED"
            row["expire_reason"] = reason
            row["expired_at"] = (
                datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
            )
            changed = True

    if changed:
        try:
            save_reservation_records(rows)
        except OSError as exc:
            print("Loi cap nhat het han dat cho:", exc, flush=True)

    return rows

def read_uid_scan_json(path):
    """Read a small JSON request/result file written by Digital Twin."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def write_uid_scan_result(request_id, status, uid="", message=""):
    """Return a scanned UID to Digital Twin without starting an entry/exit flow."""
    temp = UID_SCAN_RESULT_FILE + ".tmp"
    result = {
        "request_id": request_id,
        "status": status,
        "uid": uid,
        "message": message,
        "updated_ts": time.time(),
    }
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(temp, UID_SCAN_RESULT_FILE)
    except OSError as exc:
        print("Loi ghi ket qua doc UID tu Digital Twin:", exc, flush=True)
