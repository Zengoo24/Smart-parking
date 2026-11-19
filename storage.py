#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Luu tru va quan ly du lieu cho he thong bai do xe 4 o.

Tuong thich voi main.py hien tai:
- parking_data.json: cac xe dang o trong bai.
- parking_history.csv: lich su vao bai, do vao o va ra bai.
- parking_plate_history.csv: lich su gan voi bien so.

Cac ham giu nguyen ten va tham so de khong phai sua main.py.
"""

import csv
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime

from config import *


# ============================================================
# KHOA FILE
# ============================================================

_JSON_LOCK = threading.RLock()
_CSV_LOCK = threading.RLock()

_PLATE_RE = re.compile(r"\d{2}[A-Z]\d{5}")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


# ============================================================
# HAM NOI BO
# ============================================================

def _ensure_parent(path):
    """Tao thu muc cha neu chua ton tai."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _to_float(value, default=None):
    """Chuyen gia tri sang float an toan."""
    if value is None:
        return default
    try:
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def _append_csv(path, header, row):
    """Ghi mot dong CSV; tu them header neu file chua co noi dung."""
    _ensure_parent(path)

    with _CSV_LOCK:
        needs_header = (
            not os.path.exists(path)
            or os.path.getsize(path) == 0
        )

        with open(path, "a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file, lineterminator="\n")

            if needs_header:
                writer.writerow(header)

            writer.writerow(row)
            file.flush()


# ============================================================
# CHUAN HOA BIEN SO
# ============================================================

def normalize_plate(value):
    """Bo dau cach/ky tu dac biet va dua bien so ve chu hoa."""
    if value is None:
        return ""
    return _NON_ALNUM_RE.sub("", str(value).upper())


def valid_plate(value):
    """Kiem tra dinh dang dang dung: 2 so + 1 chu + 5 so.

    Vi du: 29B15683.
    Ham nay kiem tra cau truc, khong khang dinh bien so co that.
    """
    return bool(_PLATE_RE.fullmatch(normalize_plate(value)))


def format_plate(value):
    """Dinh dang bien so de hien thi, giu quy uoc cua chuong trinh hien tai."""
    plate = normalize_plate(value)
    if valid_plate(plate):
        return "{} {}.{}".format(
            plate[:3],
            plate[3:6],
            plate[6:8],
        )
    return plate or "KHONG RO"


# ============================================================
# LUU DU LIEU XE DANG GUI
# ============================================================

def save_active(active):
    """Luu JSON theo cach atomic de tranh file bi cat mat neu mat dien.

    Ghi file tam cung thu muc, flush + fsync, sau do os.replace.
    """
    if not isinstance(active, dict):
        raise TypeError("active phai la dictionary UID -> thong tin xe")

    _ensure_parent(DATA_FILE)
    temp_path = None

    with _JSON_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=os.path.basename(DATA_FILE) + ".",
                suffix=".tmp",
                dir=os.path.dirname(os.path.abspath(DATA_FILE)),
                delete=False,
            ) as file:
                temp_path = file.name
                json.dump(
                    active,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())

            os.replace(temp_path, DATA_FILE)
            temp_path = None

            # Bao dam metadata cua thu muc duoc day xuong bo nho tren Linux.
            try:
                dir_fd = os.open(
                    os.path.dirname(os.path.abspath(DATA_FILE)) or ".",
                    os.O_RDONLY,
                )
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def load_active():
    """Doc du lieu xe dang gui va bo sung cac truong con thieu.

    Cac truong la du lieu bo sung, khong xoa cac truong khac cua ban ghi cu.
    """
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with _JSON_LOCK:
            with open(DATA_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)

        if not isinstance(data, dict):
            print(
                "[DATA WARNING] parking_data.json khong phai dictionary; dung du lieu rong.",
                flush=True,
            )
            return {}

        changed = False
        defaults = {
            "entry_ts": None,
            "parked_ts": None,
            "start_ts": None,
            "slot": None,
            "last_slot": None,
            "plate": None,
        }

        for uid, record in data.items():
            if not isinstance(record, dict):
                print(
                    "[DATA WARNING] Bo qua ban ghi UID {} khong hop le.".format(uid),
                    flush=True,
                )
                continue

            for key, default_value in defaults.items():
                if key not in record:
                    record[key] = default_value
                    changed = True

            if "awaiting_slot" not in record:
                record["awaiting_slot"] = record.get("parked_ts") is None
                changed = True

            # Chuan hoa bien so cu neu co; khong tu y sua neu khong hop le.
            if record.get("plate"):
                normalized = normalize_plate(record["plate"])
                if normalized != record["plate"]:
                    record["plate"] = normalized
                    changed = True

        if changed:
            save_active(data)

        return data

    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(
            "[DATA ERROR] Khong doc duoc {}: {}".format(DATA_FILE, exc),
            flush=True,
        )
        return {}


# ============================================================
# GHI LICH SU GUI XE
# ============================================================

def _parking_seconds(start_ts, end_ts):
    start_value = _to_float(start_ts)
    end_value = _to_float(end_ts)
    if start_value is None or end_value is None:
        return 0
    return max(0, int(end_value - start_value))


def log_history(event, uid, slot, start_ts, end_ts, fee):
    """Ghi lich su su kien vao/ra/do o vao CSV."""
    duration = _parking_seconds(start_ts, end_ts)
    timestamp = datetime.now().isoformat(timespec="seconds")

    _append_csv(
        HISTORY_FILE,
        [
            "event",
            "uid",
            "slot",
            "timestamp",
            "parking_seconds",
            "fee_vnd",
        ],
        [
            str(event),
            str(uid or ""),
            str(slot or ""),
            timestamp,
            duration,
            max(0, int(_to_float(fee, 0) or 0)),
        ],
    )


# ============================================================
# GHI LICH SU BIEN SO
# ============================================================

def log_plate_history(event, plate, uid, slot, start_ts, end_ts, fee):
    """Ghi lich su bien so, UID, vi tri va phi vao CSV."""
    duration = _parking_seconds(start_ts, end_ts)
    timestamp = datetime.now().isoformat(timespec="seconds")

    _append_csv(
        PLATE_HISTORY_FILE,
        [
            "event",
            "plate",
            "uid",
            "slot",
            "timestamp",
            "parking_seconds",
            "fee_vnd",
        ],
        [
            str(event),
            normalize_plate(plate),
            str(uid or ""),
            str(slot or ""),
            timestamp,
            duration,
            max(0, int(_to_float(fee, 0) or 0)),
        ],
    )


# ============================================================
# TINH TIEN GUI XE
# ============================================================

def calculate_fee(parked_ts, end_ts):
    """Tinh phi theo gio, lam tron len va toi thieu 1 gio neu da do bai.

    parked_ts=None hoac timestamp khong hop le thi tra ve 0.
    end_ts=None thi dung thoi diem hien tai.
    """
    start_value = _to_float(parked_ts)
    if start_value is None:
        return 0

    end_value = _to_float(end_ts, time.time())
    if end_value is None:
        end_value = time.time()

    seconds = max(0.0, end_value - start_value)
    hours = max(1, int(math.ceil(seconds / 3600.0)))
    return hours * int(FEE_PER_HOUR)


def elapsed_text(start_ts):
    """Thoi gian da do bai, dang HH:MM:SS."""
    start_value = _to_float(start_ts)
    if start_value is None:
        return "Chua do bai"

    seconds = max(0, int(time.time() - start_value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def duration_text(start_ts, end_ts):
    """Khoang thoi gian giua hai timestamp, dang HH:MM:SS."""
    start_value = _to_float(start_ts)
    end_value = _to_float(end_ts)
    if start_value is None or end_value is None:
        return "00:00:00"

    seconds = max(0, int(end_value - start_value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


# ============================================================
# TIM UID THEO BIEN SO
# ============================================================

def find_uid_by_plate(active, plate):
    """Tim UID dang gui co bien so trung voi bien so dau vao."""
    target = normalize_plate(plate)
    if not target or not isinstance(active, dict):
        return None

    for uid, record in active.items():
        if not isinstance(record, dict):
            continue
        stored_plate = normalize_plate(record.get("plate"))
        if stored_plate and stored_plate == target:
            return uid

    return None


# ============================================================
# QUAN LY 4 O DO
# ============================================================

class ParkingLot:
    """Quan ly trang thai xe dang gui va gan vi tri do theo IR.

    self.active tham chieu truc tiep dictionary active cua main.py.
    """

    def __init__(self, active):
        if not isinstance(active, dict):
            raise TypeError("active phai la dictionary")
        self.active = active

    @staticmethod
    def free_slots(occupied):
        """Tra ve danh sach ten o dang trong theo SLOT_NAMES."""
        if occupied is None:
            return list(SLOT_NAMES)

        if len(occupied) < len(SLOT_NAMES):
            raise ValueError(
                "Can {} trang thai o do, nhan duoc {}".format(
                    len(SLOT_NAMES), len(occupied)
                )
            )

        return [
            SLOT_NAMES[index]
            for index in range(len(SLOT_NAMES))
            if not bool(occupied[index])
        ]

    def can_admit(self, occupied):
        """Kiem tra so o trong sau khi tru cac xe da duoc chap nhan vao.

        Xe co awaiting_slot=True duoc tinh la da giu cho, tranh nhan them
        nhieu xe hon so o con trong trong luc xe chua den cam bien o do.
        """
        free_count = len(self.free_slots(occupied))
        pending_count = sum(
            1
            for record in self.active.values()
            if isinstance(record, dict)
            and record.get("awaiting_slot")
            and record.get("parked_ts") is None
        )
        return free_count > pending_count

    def on_slot_occupied(self, index):
        """Gan xe dang cho vao o vua co xe; neu la xe di chuyen thi cap nhat o.

        Neu co nhieu xe dang cho, uu tien xe co entry_ts som nhat (FIFO).
        """
        index = int(index)
        if not 0 <= index < len(SLOT_NAMES):
            raise IndexError("Chi so o do khong hop le: {}".format(index))

        slot_name = SLOT_NAMES[index]

        pending = [
            (uid, record)
            for uid, record in self.active.items()
            if isinstance(record, dict)
            and record.get("awaiting_slot")
            and record.get("parked_ts") is None
        ]

        if pending:
            def entry_order(item):
                timestamp = _to_float(item[1].get("entry_ts"), float("inf"))
                return timestamp if timestamp is not None else float("inf")

            uid, record = min(pending, key=entry_order)
            now = time.time()

            record.update({
                "slot": slot_name,
                "last_slot": slot_name,
                "parked_ts": now,
                "start_ts": now,
                "awaiting_slot": False,
            })
            save_active(self.active)

            log_history("PARKED", uid, slot_name, now, now, 0)
            log_plate_history(
                "PARKED",
                record.get("plate"),
                uid,
                slot_name,
                now,
                now,
                0,
            )

            print(
                "UID {} | bien so {} do tai {}.".format(
                    uid,
                    record.get("plate") or "CHUA CO",
                    slot_name,
                ),
                flush=True,
            )
            return uid

        # Truong hop xe dang gui di chuyen tu o cu sang o moi.
        moving = [
            (uid, record)
            for uid, record in self.active.items()
            if isinstance(record, dict)
            and record.get("parked_ts") is not None
            and record.get("slot") is None
        ]

        if moving:
            uid, record = moving[0]
            record["slot"] = slot_name
            record["last_slot"] = slot_name
            record["awaiting_slot"] = False
            save_active(self.active)

            print(
                "UID {} | bien so {} chuyen sang {}.".format(
                    uid,
                    record.get("plate") or "CHUA CO",
                    slot_name,
                ),
                flush=True,
            )
            return uid

        print(
            "IR phat hien xe tai {} nhung khong co UID dang cho.".format(slot_name),
            flush=True,
        )
        return None

    def on_slot_empty(self, index):
        """Ghi nho o vua trong; giu last_slot de hien thi lich su."""
        index = int(index)
        if not 0 <= index < len(SLOT_NAMES):
            raise IndexError("Chi so o do khong hop le: {}".format(index))

        slot_name = SLOT_NAMES[index]
        changed = False

        for record in self.active.values():
            if isinstance(record, dict) and record.get("slot") == slot_name:
                record["last_slot"] = slot_name
                record["slot"] = None
                changed = True

        if changed:
            save_active(self.active)

    def finish_exit(self, uid):
        """Xoa xe da roi bai va luu lai JSON; tra ve ban ghi vua xoa."""
        removed = self.active.pop(uid, None)
        if removed is not None:
            save_active(self.active)
        return removed
