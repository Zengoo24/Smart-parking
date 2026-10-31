"""Luu the RFID duoc chap nhan va xuat trang thai cho Digital Twin."""

import json
import os
import time
from datetime import datetime

from .config import HOME, LIVE_FILE, SLOT_NAMES

# Trang thai noi bo cua module telemetry.
# Phai khoi tao tai day de controller khong bi NameError khi publish lan dau.
_LIVE_STATUS_CACHE = None
_last_rfid = {
    "uid": "",
    "gate": "",
    "result": "",
    "time": "",
}

def auto_save_rfid_card(uid):
    """Remember a card only when record_rfid_open() confirms a gate opened."""
    value = str(uid or "").strip().upper()
    for separator in (" ", ":", "-"):
        value = value.replace(separator, "")
    if not (8 <= len(value) <= 32 and len(value) % 2 == 0
            and all(ch in "0123456789ABCDEF" for ch in value)):
        return

    cards_path = os.path.join(HOME, "parking_cards.json")
    try:
        if os.path.exists(cards_path):
            with open(cards_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
                raise ValueError("parking_cards.json khong co cau truc {'cards': [...]} hop le")
            cards = data["cards"]
        else:
            cards = []

        for card in cards:
            if not isinstance(card, dict):
                continue
            old_uid = str(card.get("uid", "")).strip().upper()
            for separator in (" ", ":", "-"):
                old_uid = old_uid.replace(separator, "")
            if old_uid == value:
                return  # Da luu; giu nguyen ten the nguoi dung da dat.

        cards.append({"uid": value, "name": "Thẻ " + value})
        cards.sort(key=lambda item: str(item.get("name", "")).casefold()
                   if isinstance(item, dict) else "")
        temp_path = cards_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"cards": cards}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, cards_path)
        print("[RFID] Da tu dong luu the moi: {}".format(value), flush=True)
    except Exception as exc:
        # Khong de loi luu danh sach the lam anh huong dieu khien barie.
        print("[RFID] Canh bao: khong tu dong luu duoc UID {}: {}".format(value, exc), flush=True)

def record_rfid_open(uid, gate):
    global _last_rfid
    auto_save_rfid_card(uid)
    _last_rfid = {
        "uid": str(uid).replace(" ", "").upper(),
        "gate": gate,
        "result": "DA MO CONG",
        "time": time.strftime("%d/%m/%Y %H:%M:%S")
    }

def publish_live_status(occupied, entry_state, exit_state):
    global _LIVE_STATUS_CACHE

    slots = {
        SLOT_NAMES[i]: (
            "OCCUPIED" if occupied[i] else "FREE"
        )
        for i in range(5)
    }

    gates = {
        "entry": "OPEN" if entry_state == "OPEN" else "CLOSED",
        "exit": "OPEN" if exit_state == "OPEN" else "CLOSED",
    }

    signature = (
        tuple((name, slots[name]) for name in SLOT_NAMES),
        tuple(sorted(gates.items())),
        tuple(sorted(_last_rfid.items())),
    )

    if signature == _LIVE_STATUS_CACHE:
        return

    data = {}

    try:
        with open(LIVE_FILE, "r", encoding="utf-8") as file:
            old_data = json.load(file)

        if isinstance(old_data, dict):
            data = old_data

    except (OSError, json.JSONDecodeError):
        pass

    # Chi cap nhat UID khi co lan quet hop le moi.
    # Neu chua quet trong lan chay nay, giu UID da luu truoc do.
    if _last_rfid.get("uid"):
        data["last_rfid"] = dict(_last_rfid)
    elif not isinstance(data.get("last_rfid"), dict):
        data["last_rfid"] = {
            "uid": "",
            "gate": "",
            "result": "",
            "time": ""
        }

    data["device"] = "raspberrypi4b"
    data["updated_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    data["slots"] = slots
    data["gates"] = gates

    data["total_slots"] = 5
    data["free_count"] = sum(
        value == "FREE" for value in slots.values()
    )
    data["occupied_count"] = sum(
        value == "OCCUPIED" for value in slots.values()
    )

    temp_path = LIVE_FILE + ".tmp"

    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")

        os.replace(temp_path, LIVE_FILE)
        _LIVE_STATUS_CACHE = signature

    except OSError as exc:
        print("Loi ghi parking_live.json:", exc, flush=True)
