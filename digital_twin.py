#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Digital Twin + Reservation cho mo hinh bai do xe Raspberry Pi."""

import csv
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

HOME = Path.home()
LIVE_FILE = HOME / "parking_live.json"
ACTIVE_FILE = HOME / "parking_data.json"
HISTORY_FILE = HOME / "parking_history.csv"
RESERVATIONS_FILE = HOME / "parking_reservations.json"
CARDS_FILE = HOME / "parking_cards.json"
UID_SCAN_REQUEST_FILE = HOME / "parking_uid_request.json"
UID_SCAN_RESULT_FILE = HOME / "parking_uid_result.json"
SLOTS = ("P1", "P2", "P3", "P4", "P5")
# Nhan tren giao dien -> vi tri cam bien vat ly.
DISPLAY_SOURCE = {
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "P4": "P4",
    "P5": "P5",
}
app = Flask(__name__)


def read_json(path, default=None):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def atomic_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def load_reservations():
    data = read_json(RESERVATIONS_FILE, {"reservations": []})
    rows = data.get("reservations", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save_reservations(rows):
    atomic_json(RESERVATIONS_FILE, {"reservations": rows})


def load_cards():
    """Load saved RFID cards; seed known historic UIDs once on first run."""
    if CARDS_FILE.exists():
        data = read_json(CARDS_FILE, {"cards": []})
        rows = data.get("cards", []) if isinstance(data, dict) else []
        return [r for r in rows if isinstance(r, dict) and r.get("uid")]

    # Import UID already seen in history, active sessions, and reservations.
    uids = set()
    active = read_json(ACTIVE_FILE, {})
    if isinstance(active, dict):
        uids.update(str(uid).upper() for uid in active)
    reservations = load_reservations()
    uids.update(str(r.get("uid", "")).upper() for r in reservations if r.get("uid"))
    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    row = {str(k or "").strip().lower(): v for k, v in row.items()}
                    uid = str(row.get("uid", "")).strip().upper()
                    if uid:
                        uids.add(uid)
        except (OSError, csv.Error, UnicodeError):
            pass

    cards = [
        {"uid": uid, "name": "Thẻ " + uid}
        for uid in sorted(uids)
        if re.fullmatch(r"[0-9A-F]{8,32}", uid) and len(uid) % 2 == 0
    ]
    save_cards(cards)
    return cards


def save_cards(cards):
    atomic_json(CARDS_FILE, {"cards": cards})


def fmt_time(value):
    try:
        return datetime.fromtimestamp(float(value)).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        return str(value or "—")


def fmt_duration(start):
    try:
        seconds = max(0, int(time.time() - float(start)))
    except (ValueError, TypeError):
        return "Chưa bắt đầu"
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours} giờ {minutes} phút" if hours else f"{minutes} phút"


def read_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        with HISTORY_FILE.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeError):
        return []

    out = []
    for row in rows[-10:]:
        # Ho tro header ca tieng Anh va tieng Viet cua cac ban code cu.
        row = {str(k or "").strip().lower(): v for k, v in row.items()}
        out.append({
            "event": row.get("event") or row.get("su_kien") or "—",
            "uid": row.get("uid") or "—",
            "slot": row.get("slot") or row.get("vi_tri") or row.get("vitri") or "—",
            "time": row.get("timestamp") or row.get("thoi_gian") or row.get("thoigian") or "—",
            "fee": row.get("fee_vnd") or row.get("fee") or row.get("phi_vnd") or row.get("phi") or "0",
        })
    return list(reversed(out))



def parse_event_time(value):
    """Parse Unix epoch or ISO timestamp stored by the parking logger."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(float(value))
    except (ValueError, TypeError, OSError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


def num(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return default


def event_kind(row):
    raw = str(row.get("event") or row.get("su_kien") or row.get("sukien") or "").strip().upper()
    aliases = {
        "ENTRY": "ENTRY", "ENTER": "ENTRY", "IN": "ENTRY", "VAO": "ENTRY", "XE VAO": "ENTRY",
        "EXIT": "EXIT", "LEAVE": "EXIT", "OUT": "EXIT", "RA": "EXIT", "XE RA": "EXIT",
        "PARKED": "PARKED", "PARK": "PARKED", "DO": "PARKED", "BAT DAU DO": "PARKED",
    }
    return aliases.get(raw, raw)


def display_slot_for_history(value):
    slot = str(value or "").strip().upper()
    if slot not in SLOTS:
        return None
    return next((display for display, physical in DISPLAY_SOURCE.items() if physical == slot), slot)


def calculate_statistics():
    """Summarize CSV history and reservation JSON; no metric is inferred from current IR alone."""
    records = []
    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open(encoding="utf-8-sig", newline="") as f:
                for raw in csv.DictReader(f):
                    row = {str(k or "").strip().lower(): v for k, v in raw.items()}
                    kind = event_kind(row)
                    timestamp = (row.get("timestamp") or row.get("thoi_gian") or row.get("thoigian")
                                 or row.get("datetime") or row.get("time") or "")
                    dt = parse_event_time(timestamp)
                    if not kind or dt is None:
                        continue
                    uid = str(row.get("uid") or "").strip().upper()
                    slot_value = row.get("slot") or row.get("vi_tri") or row.get("vitri") or row.get("parking_slot") or ""
                    duration_value = (row.get("parking_seconds") or row.get("thoi_gian_giay")
                                      or row.get("duration_seconds") or row.get("duration_sec") or "")
                    fee_value = (row.get("fee_vnd") or row.get("phi_vnd") or row.get("fee")
                                 or row.get("phi") or "0")
                    records.append({
                        "event": kind, "uid": uid, "time": dt,
                        "slot": display_slot_for_history(slot_value),
                        "duration": num(duration_value, -1),
                        "fee": max(0, num(fee_value, 0)),
                    })
        except (OSError, csv.Error, UnicodeError):
            records = []

    now = datetime.now()
    today = now.date()
    entries = [r for r in records if r["event"] == "ENTRY"]
    exits = [r for r in records if r["event"] == "EXIT"]
    parked = [r for r in records if r["event"] == "PARKED"]
    today_entries = [r for r in entries if r["time"].date() == today]
    today_exits = [r for r in exits if r["time"].date() == today]

    durations = [r["duration"] for r in exits if r["duration"] >= 0]
    # Older CSV formats may omit parking_seconds; pair PARKED and EXIT by UID as fallback.
    if not durations:
        starts = {}
        inferred = []
        for r in records:
            if r["event"] == "PARKED" and r["uid"]:
                starts[r["uid"]] = r["time"]
            elif r["event"] == "EXIT" and r["uid"] in starts:
                inferred.append(max(0, int((r["time"] - starts.pop(r["uid"])).total_seconds())))
        durations = inferred

    fees_total = sum(r["fee"] for r in exits)
    fees_today = sum(r["fee"] for r in today_exits)
    hour_counts = [0] * 24
    for r in entries:
        hour_counts[r["time"].hour] += 1
    peak_hour = "Chưa có dữ liệu"
    if any(hour_counts):
        hour = max(range(24), key=lambda h: hour_counts[h])
        peak_hour = f"{hour:02d}:00–{hour:02d}:59 ({hour_counts[hour]} lượt)"

    slot_counts = {slot: 0 for slot in SLOTS}
    for r in parked:
        if r["slot"] in slot_counts:
            slot_counts[r["slot"]] += 1
    # If an older log lacks PARKED records, use EXIT-by-slot as a clearly approximate fallback.
    usage_source = "PARKED"
    if not any(slot_counts.values()):
        usage_source = "EXIT (ước tính từ lịch sử ra)"
        for r in exits:
            if r["slot"] in slot_counts:
                slot_counts[r["slot"]] += 1
    max_slot_count = max(slot_counts.values(), default=0)
    slot_usage = [{
        "slot": slot,
        "count": slot_counts[slot],
        "percent": round(100 * slot_counts[slot] / max_slot_count) if max_slot_count else 0,
    } for slot in SLOTS]

    daily = []
    for days_ago in range(6, -1, -1):
        day = today.fromordinal(today.toordinal() - days_ago)
        day_records = [r for r in records if r["time"].date() == day]
        day_entries = sum(r["event"] == "ENTRY" for r in day_records)
        day_exits = [r for r in day_records if r["event"] == "EXIT"]
        daily.append({
            "date": day.strftime("%d/%m"),
            "entries": day_entries,
            "exits": len(day_exits),
            "fees": sum(r["fee"] for r in day_exits),
        })

    reservations = load_reservations()
    active_reservations = 0
    cancelled = 0
    completed = 0
    expired = 0
    for item in reservations:
        status = str(item.get("status", "BOOKED")).upper()
        try:
            end_ts = float(item.get("end_ts", 0))
        except (TypeError, ValueError):
            end_ts = 0
        if status in ("BOOKED", "ARRIVED", "IN_USE") and end_ts > time.time():
            active_reservations += 1
        elif status == "CANCELLED":
            cancelled += 1
        elif status == "COMPLETED":
            completed += 1
        elif status == "EXPIRED" or (status == "BOOKED" and end_ts <= time.time()):
            expired += 1

    return {
        "entries_total": len(entries), "exits_total": len(exits),
        "entries_today": len(today_entries), "exits_today": len(today_exits),
        "fees_total": fees_total, "fees_today": fees_today,
        "avg_parking_seconds": round(sum(durations) / len(durations)) if durations else None,
        "completed_sessions": len(exits), "peak_hour": peak_hour,
        "slot_usage": slot_usage, "usage_source": usage_source,
        "daily": daily, "reservations_total": len(reservations),
        "reservations_active": active_reservations, "reservations_cancelled": cancelled,
        "reservations_completed": completed, "reservations_expired": expired,
        "history_rows": len(records),
    }

def reservation_state(item, now):
    status = str(item.get("status", "BOOKED")).upper()
    try:
        start, end = float(item["start_ts"]), float(item["end_ts"])
    except (KeyError, TypeError, ValueError):
        start, end = 0, 0
    if status == "BOOKED":
        if end <= now:
            return "HẾT HẠN"
        if start <= now < end:
            return "ĐANG HIỆU LỰC"
        return "SẮP ĐẾN"
    return {
        "ARRIVED": "ĐÃ ĐẾN CỔNG",
        "IN_USE": "ĐANG ĐỖ",
        "COMPLETED": "HOÀN THÀNH",
        "CANCELLED": "ĐÃ HỦY",
        "EXPIRED": "HẾT HẠN",
    }.get(status, status)


def load_state():
    live = read_json(LIVE_FILE)
    active = read_json(ACTIVE_FILE)
    active = active if isinstance(active, dict) else {}
    raw_slots = live.get("slots", {}) if isinstance(live, dict) else {}
    raw_gates = live.get("gates", {}) if isinstance(live, dict) else {}
    last_rfid = live.get("last_rfid", {}) if isinstance(live, dict) else {}
    if not isinstance(last_rfid, dict):
        last_rfid = {}
    now = time.time()

    rows = load_reservations()
    held = {}
    for item in rows:
        if item.get("status") not in ("BOOKED", "ARRIVED", "IN_USE"):
            continue
        try:
            end = float(item["end_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > now and item.get("slot") in SLOTS:
            held[item["slot"]] = item

    slots = []
    for display_name in SLOTS:
        physical = DISPLAY_SOURCE[display_name]
        status = str(raw_slots.get(physical, "UNKNOWN")).upper() if isinstance(raw_slots, dict) else "UNKNOWN"
        status = status if status in ("FREE", "OCCUPIED") else "UNKNOWN"
        reservation = held.get(physical)
        if status == "FREE" and reservation:
            status = "RESERVED"
        uid = next((u for u, rec in active.items()
                    if isinstance(rec, dict) and rec.get("slot") == physical), "")
        slots.append({
            "id": display_name, "status": status, "uid": uid,
            "reservation_uid": reservation.get("uid", "") if reservation else "",
        })

    sessions = []
    for uid, rec in active.items():
        if not isinstance(rec, dict):
            continue
        physical = rec.get("slot") or rec.get("last_slot")
        display_slot = next((d for d, s in DISPLAY_SOURCE.items() if s == physical), physical) if physical else "Chưa gán chỗ"
        start = rec.get("parked_ts")
        sessions.append({
            "uid": uid, "slot": display_slot,
            "status": "ĐANG ĐỖ" if start else "CHỜ VÀO CHỖ",
            "start": fmt_time(start or rec.get("entry_ts")),
            "duration": fmt_duration(start) if start else "Chưa tính giờ",
        })
    sessions.sort(key=lambda x: (x["slot"], x["uid"]))

    reservation_view = []
    for item in sorted(rows, key=lambda x: float(x.get("created_ts", 0) or 0), reverse=True)[:10]:
        physical = item.get("slot")
        display_slot = next((d for d, s in DISPLAY_SOURCE.items() if s == physical), physical or "—")
        reservation_view.append({
            "id": str(item.get("id", "")), "uid": str(item.get("uid", "—")),
            "slot": str(display_slot), "start": fmt_time(item.get("start_ts")),
            "end": fmt_time(item.get("end_ts")),
            "status": reservation_state(item, now),
            "can_cancel": item.get("status") == "BOOKED",
        })

    known = [s for s in slots if s["status"] != "UNKNOWN"]
    sensor_ready = len(known) == 5
    actual_occupied = sum(s["status"] == "OCCUPIED" for s in slots) if sensor_ready else None
    free_count = sum(s["status"] == "FREE" for s in slots) if sensor_ready else None
    if not sensor_ready:
        guidance = "Chưa đủ dữ liệu IR để hướng dẫn."
    else:
        free_names = [s["id"] for s in slots if s["status"] == "FREE"]
        guidance = (f"Gợi ý chỗ đỗ: {free_names[0]} | Còn trống: {', '.join(free_names)}"
                    if free_names else "Không còn chỗ chưa đặt trước. Kiểm tra bảng trạng thái bên dưới.")

    return {
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "statistics": calculate_statistics(),
        "slots": slots, "sessions": sessions, "history": read_history(),
        "reservations": reservation_view, "cards": load_cards(),
        "occupied": actual_occupied, "free": free_count, "session_count": len(active),
        "reserved_count": sum(s["status"] == "RESERVED" for s in slots),
        "sensor_ready": sensor_ready, "guidance": guidance,
        "entry_gate": str(raw_gates.get("entry", "UNKNOWN")).upper() if isinstance(raw_gates, dict) else "UNKNOWN",
        "exit_gate": str(raw_gates.get("exit", "UNKNOWN")).upper() if isinstance(raw_gates, dict) else "UNKNOWN",
        "last_rfid": last_rfid,
    }


def parse_local_time(value):
    # datetime-local tu trinh duyet la gio dia phuong cua Raspberry Pi.
    return datetime.fromisoformat(value).timestamp()


@app.get("/")
def home():
    return render_template("digital_twin.html")


@app.get("/api/state")
@app.get("/api/status")
def api_state():
    return jsonify(load_state())


@app.post("/api/cards")
def register_card():
    body = request.get_json(silent=True) or {}
    uid = re.sub(r"[\s:-]", "", str(body.get("uid", "")).upper())
    name = str(body.get("name", "")).strip()[:40]
    if not re.fullmatch(r"[0-9A-F]{8,32}", uid) or len(uid) % 2:
        return jsonify(ok=False, message="UID không hợp lệ. Hãy đọc thẻ hoặc nhập UID hex đã biết."), 400
    if not name:
        return jsonify(ok=False, message="Hãy đặt tên dễ nhớ cho thẻ, ví dụ: Thẻ xe gia đình."), 400

    cards = load_cards()
    found = False
    for card in cards:
        if str(card.get("uid", "")).upper() == uid:
            card["name"] = name
            found = True
            break
    if not found:
        cards.append({"uid": uid, "name": name})
    cards.sort(key=lambda item: str(item.get("name", "")).lower())
    save_cards(cards)
    return jsonify(ok=True, message=f"Đã lưu thẻ '{name}' ({uid}). Có thể chọn thẻ này khi đặt chỗ từ xa.", cards=cards), 200


@app.post("/api/uid/read")
def request_uid_scan():
    """Yêu cau main-loop doc UID; route nay khong tu truy cap SPI/GPIO."""
    now = time.time()
    previous_request = read_json(UID_SCAN_REQUEST_FILE, {})
    previous_result = read_json(UID_SCAN_RESULT_FILE, {})
    if isinstance(previous_request, dict):
        same_id = previous_request.get("request_id")
        pending = (
            same_id
            and float(previous_request.get("expires_ts", 0) or 0) > now
            and not (
                isinstance(previous_result, dict)
                and previous_result.get("request_id") == same_id
                and previous_result.get("status") in ("FOUND", "TIMEOUT", "BUSY", "ERROR")
            )
        )
        if pending:
            return jsonify(ok=False, message="Đang có yêu cầu đọc thẻ khác. Hãy chờ hoặc thử lại sau."), 409

    request_id = uuid.uuid4().hex
    req = {"request_id": request_id, "created_ts": now, "expires_ts": now + 30.0}
    atomic_json(UID_SCAN_REQUEST_FILE, req)
    atomic_json(UID_SCAN_RESULT_FILE, {
        "request_id": request_id, "status": "PENDING", "uid": "",
        "message": "Đưa thẻ gần RC522 cổng vào. Lệnh này chỉ đọc UID, không mở barie.",
        "updated_ts": now,
    })
    return jsonify(ok=True, request_id=request_id,
                   message="Đưa thẻ gần RC522 cổng vào trong 30 giây."), 202


@app.get("/api/uid/read/<request_id>")
def get_uid_scan_result(request_id):
    result = read_json(UID_SCAN_RESULT_FILE, {})
    if isinstance(result, dict) and result.get("request_id") == request_id:
        return jsonify(result)
    req = read_json(UID_SCAN_REQUEST_FILE, {})
    if isinstance(req, dict) and req.get("request_id") == request_id:
        return jsonify(request_id=request_id, status="PENDING", uid="",
                       message="Đưa thẻ gần RC522 cổng vào...")
    return jsonify(request_id=request_id, status="TIMEOUT", uid="",
                   message="Yêu cầu đọc UID đã hết hạn. Hãy bấm đọc thẻ để thử lại.")


@app.post("/api/reservations")
def create_reservation():
    body = request.get_json(silent=True) or {}
    uid = re.sub(r"[\s:-]", "", str(body.get("uid", "")).upper())
    slot_display = str(body.get("slot", "")).upper()
    if not re.fullmatch(r"[0-9A-F]{8,32}", uid) or len(uid) % 2:
        return jsonify(ok=False, message="UID không hợp lệ. Nhập UID RFID dạng hex, ví dụ 8C2E2707."), 400
    if slot_display not in DISPLAY_SOURCE:
        return jsonify(ok=False, message="Vị trí đỗ không hợp lệ."), 400
    try:
        start_ts = parse_local_time(str(body.get("start", "")))
        end_ts = parse_local_time(str(body.get("end", "")))
    except (ValueError, TypeError, OverflowError):
        return jsonify(ok=False, message="Thời gian đặt chỗ không hợp lệ."), 400
    now = time.time()
    if start_ts < now - 30:
        return jsonify(ok=False, message="Thời gian bắt đầu phải ở hiện tại hoặc tương lai."), 400
    if end_ts <= start_ts:
        return jsonify(ok=False, message="Thời gian kết thúc phải sau thời gian bắt đầu."), 400
    if end_ts - start_ts > 24 * 3600:
        return jsonify(ok=False, message="Mỗi lượt đặt chỗ tối đa 24 giờ trong bản thử nghiệm."), 400

    physical_slot = DISPLAY_SOURCE[slot_display]
    live = read_json(LIVE_FILE)
    raw_slots = live.get("slots", {}) if isinstance(live, dict) else {}
    if not isinstance(raw_slots, dict) or any(raw_slots.get(name) not in ("FREE", "OCCUPIED") for name in SLOTS):
        return jsonify(ok=False, message="Chưa có đủ dữ liệu IR; chưa thể nhận đặt chỗ."), 409
    if raw_slots.get(physical_slot) != "FREE":
        return jsonify(ok=False, message=f"Vị trí {slot_display} hiện không trống."), 409

    active = read_json(ACTIVE_FILE)
    if not isinstance(active, dict):
        active = {}
    if uid in active:
        return jsonify(ok=False, message="UID này đang có phiên trong bãi, không thể đặt thêm."), 409

    rows = load_reservations()
    for item in rows:
        if item.get("status") not in ("BOOKED", "ARRIVED", "IN_USE"):
            continue
        try:
            old_start, old_end = float(item["start_ts"]), float(item["end_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        overlaps = start_ts < old_end and end_ts > old_start
        if overlaps and item.get("slot") == physical_slot:
            return jsonify(ok=False, message="Vị trí này đã có đặt chỗ trùng thời gian."), 409
        if overlaps and str(item.get("uid", "")).upper() == uid:
            return jsonify(ok=False, message="UID này đã có đặt chỗ trùng thời gian."), 409

    reservation = {
        "id": uuid.uuid4().hex[:8].upper(), "uid": uid,
        "slot": physical_slot, "start_ts": start_ts, "end_ts": end_ts,
        "status": "BOOKED", "created_ts": now,
    }
    rows.append(reservation)
    save_reservations(rows)
    shown_physical = physical_slot
    return jsonify(ok=True, message=f"Đã đặt {slot_display} (cảm biến {shown_physical}) cho UID {uid}.", reservation_id=reservation["id"]), 201


@app.post("/api/reservations/<reservation_id>/cancel")
def cancel_reservation(reservation_id):
    rows = load_reservations()
    for item in rows:
        if str(item.get("id")) == reservation_id:
            if item.get("status") != "BOOKED":
                return jsonify(ok=False, message="Chỉ hủy được đặt chỗ chưa đến cổng."), 409
            item["status"] = "CANCELLED"
            item["updated_ts"] = time.time()
            save_reservations(rows)
            return jsonify(ok=True, message="Đã hủy đặt chỗ."), 200
    return jsonify(ok=False, message="Không tìm thấy mã đặt chỗ."), 404


@app.delete("/api/reservations/<reservation_id>")
def delete_reservation(reservation_id):
    """Alias DELETE để tương thích với giao diện Digital Twin mới."""
    return cancel_reservation(reservation_id)


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    print("Digital Twin + Reservation dang chay tren cong 5000.", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
