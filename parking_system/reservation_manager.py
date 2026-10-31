"""Quan ly trang thai dat cho, check-in va het han."""

import time

from .config import DISPLAY_TO_PHYSICAL, NO_SHOW_GRACE_SECONDS
from .rfid import normalize_uid
from .data_store import load_reservation_records, save_reservation_records

def set_reservation_status(reservation_id, new_status, extra=None):
    """Chuyen trang thai theo luong BOOKED -> ARRIVED -> IN_USE -> COMPLETED."""
    rows = load_reservation_records()

    allowed_from = {
        "ARRIVED": {"BOOKED"},
        "IN_USE": {"ARRIVED"},
        "COMPLETED": {"ARRIVED", "IN_USE"},
        "CANCELLED": {"BOOKED"},
    }

    for row in rows:
        if not isinstance(row, dict):
            continue

        if row.get("id") != reservation_id:
            continue

        old_status = row.get("status")

        if new_status not in allowed_from:
            return False

        if old_status not in allowed_from[new_status]:
            print(
                "Khong doi dat cho {} tu {} sang {}.".format(
                    reservation_id, old_status, new_status
                ),
                flush=True,
            )
            return False

        row["status"] = new_status

        if extra:
            row.update(extra)

        try:
            save_reservation_records(rows)
        except OSError as exc:
            print("Loi ghi dat cho:", exc, flush=True)
            return False

        print(
            "Dat cho {}: {} -> {}".format(
                reservation_id, old_status, new_status
            ),
            flush=True,
        )

        return True

    return False

def find_reservation_for_uid(uid):
    uid = normalize_uid(uid)

    if uid is None:
        return "NONE", None

    rows = load_reservation_records()
    now = time.time()
    upcoming = []
    valid = []
    no_show = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        if normalize_uid(row.get("uid")) != uid:
            continue

        status = row.get("status")

        if status in ("ARRIVED", "IN_USE"):
            return "ALREADY_ARRIVED", row

        try:
            start_ts = float(row.get("start_ts", 0))
            end_ts = float(row.get("end_ts", 0))
        except (TypeError, ValueError):
            continue

        if (
            status == "EXPIRED"
            and row.get("expire_reason") == "NO_SHOW"
            and now < end_ts
        ):
            no_show.append(row)

        elif status == "BOOKED":
            if start_ts <= now < end_ts:
                if now < start_ts + NO_SHOW_GRACE_SECONDS:
                    valid.append(row)
            elif now < start_ts:
                upcoming.append(row)

    if valid:
        valid.sort(key=lambda row: float(row["start_ts"]))
        return "VALID", valid[0]

    if no_show:
        no_show.sort(key=lambda row: float(row["start_ts"]), reverse=True)
        return "NO_SHOW", no_show[0]

    if upcoming:
        upcoming.sort(key=lambda row: float(row["start_ts"]))
        return "EARLY", upcoming[0]

    return "NONE", None

def booked_physical_slots():
    """O dang duoc dat trong khung gio hoat dong hoac dang cho xe check-in."""
    rows = load_reservation_records()
    now = time.time()
    held = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        status = row.get("status")

        if status not in ("BOOKED", "ARRIVED"):
            continue

        try:
            start_ts = float(row.get("start_ts", 0))
            end_ts = float(row.get("end_ts", 0))
        except (TypeError, ValueError):
            continue

        if status == "ARRIVED" or start_ts <= now < end_ts:
            display_slot = str(row.get("slot", "")).upper()
            physical_slot = DISPLAY_TO_PHYSICAL.get(display_slot)

            if physical_slot:
                held.add(physical_slot)

    return held
