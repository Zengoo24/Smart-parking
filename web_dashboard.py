#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask dashboard cho he thong bai do xe thong minh.

API:
    start_web_server(plate_system=None, host='0.0.0.0', port=5000)
    update_web_state(**kwargs)
    format_ts(timestamp)
    pop_pending_resets()
"""

import copy
import logging
import math
import threading
import time
from datetime import datetime

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from config import ROI_REFERENCE_WIDTH, ROI_REFERENCE_HEIGHT, FEE_PER_HOUR

# Tat log truy cap HTTP /api/status de giam I/O va khong troi log he thong
logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__, template_folder="templates")

_state_lock = threading.RLock()
_plate_system_lock = threading.RLock()
_plate_system_ref = None
_server_thread = None
_server_started = False
_control_lock = threading.RLock()
_on_start = None
_on_stop = None
_status_provider = None

_reset_lock = threading.Lock()
_pending_resets = []

_web_state = {
    "occupied_count": 0,
    "total_slots": 4,
    "entry_plate": "",
    "entry_rfid": "",
    "exit_plate": "",
    "exit_rfid": "",
    "last_entry_time": "",
    "last_exit_time": "",
    "last_duration": "",
    "last_fee": "0 VND",
    "exit_slot": "",
    "receipt_plate": "",
    "receipt_rfid": "",
    "receipt_slot": "",
    "receipt_entry_time": "",
    "receipt_exit_time": "",
    "receipt_duration": "",
    "receipt_fee": "0 VND",
    "is_exiting": False,
    "active_vehicles": [],
    "fee_per_hour": int(FEE_PER_HOUR),
    "total_live_fee": 0,
    "total_live_fee_text": "0 VND",
    "system_status": "Dashboard san sang",
    "controller_status": "STOPPED",
    "controller_running": False,
    "controller_error": "",
}


def update_web_state(**kwargs):
    """Cap nhat du lieu dashboard an toan giua main loop va Flask threads."""
    allowed = set(_web_state)
    clean = {key: value for key, value in kwargs.items() if key in allowed}
    with _state_lock:
        _web_state.update(clean)


def format_ts(ts):
    """Dinh dang Unix timestamp theo gio dia phuong cua Raspberry Pi."""
    if ts in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S - %d/%m/%Y")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def pop_pending_resets():
    """Lay danh sach (uid, plate, reset_all) de main.py xu ly trong vong lap chinh."""
    with _reset_lock:
        if not _pending_resets:
            return []
        items = list(_pending_resets)
        _pending_resets.clear()
        return items


def _reset_when_stopped(clean_uid, clean_plate, reset_all):
    """Xoa truc tiep trong parking_data.json khi he thong dang dung."""
    from storage import load_active, save_active, normalize_plate, format_plate

    active = load_active()
    removed = 0
    for uid_key in list(active.keys()):
        rec = active.get(uid_key)
        rec_plate = normalize_plate(rec.get("plate")) if isinstance(rec, dict) else ""
        if reset_all or (clean_uid and uid_key == clean_uid) or (clean_plate and rec_plate == clean_plate):
            active.pop(uid_key, None)
            removed += 1

    if removed > 0:
        save_active(active)

    vehicles = []
    for uid_key, record in active.items():
        if not isinstance(record, dict):
            continue
        parked_ts = record.get("parked_ts")
        current_slot = record.get("slot")
        if parked_ts is not None and not current_slot:
            current_slot = "Dang di chuyen"
        cleaned_hex = "".join(ch for ch in str(uid_key).upper() if ch in "0123456789ABCDEF")
        vehicles.append({
            "plate": format_plate(record.get("plate")) if record.get("plate") else "",
            "rfid": " ".join(cleaned_hex[i:i + 2] for i in range(0, len(cleaned_hex), 2)),
            "slot": str(current_slot or ""),
            "parked_ts": parked_ts,
            "entry_ts": record.get("entry_ts"),
            "awaiting_slot": bool(record.get("awaiting_slot")),
        })

    update_web_state(
        active_vehicles=vehicles,
        entry_plate="",
        entry_rfid="",
        exit_plate="",
        exit_rfid="",
    )
    return removed


def _make_blank_jpeg(width=480, height=300, color=(30, 35, 42), message="Dang cho hinh anh"):
    image = np.full((height, width, 3), color, dtype=np.uint8)
    text_size = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.62, 2)[0]
    tx = max(12, (width - text_size[0]) // 2)
    ty = max(24, (height + text_size[1]) // 2)
    cv2.putText(
        image, message, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX,
        0.62, (120, 128, 138) if sum(color) > 400 else (235, 235, 235), 2, cv2.LINE_AA,
    )
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
    return buffer.tobytes() if ok else b""


_BLANK_CAM = _make_blank_jpeg()
_BLANK_CHARS = _make_blank_jpeg(480, 180, (245, 246, 248), "Chua doc duoc ky tu")

# Cache toa do ROI da quy doi theo kich thuoc khung hinh camera
_roi_rect_cache = {}
_last_frame_times = {"VAO": -1.0, "RA": -1.0}


def _get_scaled_roi_rect(gate, width, height, roi):
    cache_key = (gate, width, height, tuple(roi))
    rect = _roi_rect_cache.get(cache_key)
    if rect is not None:
        return rect

    sx = width / float(max(1, int(ROI_REFERENCE_WIDTH)))
    sy = height / float(max(1, int(ROI_REFERENCE_HEIGHT)))
    x1, y1, x2, y2 = roi
    x1 = max(0, min(width - 1, int(round(x1 * sx))))
    x2 = max(x1 + 1, min(width, int(round(x2 * sx))))
    y1 = max(0, min(height - 1, int(round(y1 * sy))))
    y2 = max(y1 + 1, min(height, int(round(y2 * sy))))
    rect = (x1, y1, x2, y2)
    _roi_rect_cache[cache_key] = rect
    return rect


def _get_gate_preview_frame(gate):
    """Lay frame moi nhat (bo qua neu frame_time khong doi) va ve khung ROI giu nguyen kich thuoc."""
    with _plate_system_lock:
        plate_system = _plate_system_ref

    if plate_system is None or gate not in ("VAO", "RA"):
        _last_frame_times[gate] = -1.0
        return None, True

    try:
        camera = plate_system.cameras.get(gate)
        if camera is None:
            return None, True

        frame, frame_time = camera.get_frame()
        ft = float(frame_time or 0.0)
        if frame is None or getattr(frame, "size", 0) == 0:
            return None, True
        if time.monotonic() - ft > 1.5:
            _last_frame_times[gate] = -1.0
            return None, True

        # Neu khung hinh giong het lan truoc -> khong ve lai, khong resize/encode lai
        if ft == _last_frame_times.get(gate):
            return None, False
        _last_frame_times[gate] = ft

        roi = plate_system.config.get(gate, {}).get("roi")
        if roi and len(roi) == 4:
            height, width = frame.shape[:2]
            x1, y1, x2, y2 = _get_scaled_roi_rect(gate, width, height, roi)
            cv2.rectangle(frame, (x1, y1), (x2 - 1, y2 - 1), (0, 255, 255), 3)
            cv2.putText(
                frame, "ROI {}".format(gate),
                (x1, max(26, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                0.75, (0, 255, 255), 2, cv2.LINE_AA,
            )
        return frame, True
    except Exception:
        return None, True


def _split_plate_two_lines(plate_text):
    """Tach chuoi bien so da doc thanh 2 dong (dong tren: ma tinh/seri, dong duoi: so thu tu)."""
    text = str(plate_text or "").strip().upper()
    if not text:
        return "", ""

    if "-" in text:
        parts = [p.strip() for p in text.split("-", 1)]
        return parts[0], parts[1]

    clean = "".join(ch for ch in text if ch.isalnum())
    if len(clean) <= 4:
        return clean, ""

    # Bien 9 ky tu (vd 29E112345) hoac ky tu thu 4 la chu (vd 29MD12345) -> 4 ky tu dong tren
    if len(clean) >= 8 and (len(clean) >= 9 or clean[3].isalpha()):
        top, bottom = clean[:4], clean[4:]
    else:
        top, bottom = clean[:3], clean[3:]

    if len(bottom) == 5 and bottom.isdigit():
        bottom = bottom[:3] + "." + bottom[3:]
    return top, bottom


def _get_recognized_plate_text(gate):
    """Lay chuoi ky tu bien so da doc duoc tu _web_state hoac plate_system.states."""
    key = "entry_plate" if gate == "VAO" else "exit_plate"
    with _state_lock:
        text = str(_web_state.get(key) or "").strip()
    if text:
        return text

    with _plate_system_lock:
        ps = _plate_system_ref
    if ps is not None:
        try:
            st = ps.states.get(gate, {})
            for field in ("confirmed_plate", "plate", "last_plate", "ocr_text"):
                val = st.get(field)
                if val:
                    return str(val).strip().upper()
        except Exception:
            pass
    return ""


def _render_recognized_chars_jpeg(plate_text):
    """Tao anh ky tu da doc duoc (chu trang nen den 2 dong tuong phan cao)."""
    if not plate_text:
        return _BLANK_CHARS

    width, height = 480, 180
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    # Vien trang mong ben trong giong khung bien so
    cv2.rectangle(canvas, (6, 6), (width - 7, height - 7), (220, 220, 220), 2)

    top_line, bottom_line = _split_plate_two_lines(plate_text)
    font = cv2.FONT_HERSHEY_DUPLEX

    if top_line and bottom_line:
        # Dong tren
        (tw1, th1), _ = cv2.getTextSize(top_line, font, 1.85, 4)
        x1 = max(14, (width - tw1) // 2)
        y1 = 74
        cv2.putText(canvas, top_line, (x1, y1), font, 1.85, (255, 255, 255), 4, cv2.LINE_AA)

        # Dong duoi
        (tw2, th2), _ = cv2.getTextSize(bottom_line, font, 1.95, 4)
        x2 = max(14, (width - tw2) // 2)
        y2 = 152
        cv2.putText(canvas, bottom_line, (x2, y2), font, 1.95, (255, 255, 255), 4, cv2.LINE_AA)
    else:
        single = top_line or plate_text
        (tw, th), _ = cv2.getTextSize(single, font, 1.85, 4)
        x = max(14, (width - tw) // 2)
        y = (height + th) // 2
        cv2.putText(canvas, single, (x, y), font, 1.85, (255, 255, 255), 4, cv2.LINE_AA)

    ok, buffer = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return buffer.tobytes() if ok else _BLANK_CHARS


def _encode_frame(image, fallback, quality=68):
    if image is None or getattr(image, "size", 0) == 0:
        return fallback
    try:
        if len(image.shape) == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        ok, buffer = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        )
        return buffer.tobytes() if ok else fallback
    except Exception:
        return fallback


_STREAM_CACHE_LOCK = threading.Lock()
_STREAM_CACHE_STOP = threading.Event()
_STREAM_CACHE_THREAD = None
_STREAM_CACHE_INTERVAL = 0.10  # ~10 FPS cho camera live
_STREAM_CACHE = {
    gate: {"frame": _BLANK_CAM, "crop": _BLANK_CHARS}
    for gate in ("VAO", "RA")
}
_LAST_RENDERED_PLATE = {"VAO": None, "RA": None}
_STREAM_SUBSCRIBERS = {
    gate: {"frame": 0, "crop": 0}
    for gate in ("VAO", "RA")
}


def _resize_stream_image(image, max_width, max_height):
    if image is None or getattr(image, "size", 0) == 0:
        return None
    height, width = image.shape[:2]
    scale = min(max_width / float(width), max_height / float(height), 1.0)
    if scale >= 1.0:
        return image
    # INTER_LINEAR nhanh hon ~3 lan so voi INTER_AREA tren CPU ARM Pi 4B
    return cv2.resize(
        image,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_LINEAR,
    )


def _camera_cache_loop():
    while not _STREAM_CACHE_STOP.is_set():
        loop_started = time.monotonic()
        for gate in ("VAO", "RA"):
            with _STREAM_CACHE_LOCK:
                wants_frame = _STREAM_SUBSCRIBERS[gate]["frame"] > 0
                wants_crop = _STREAM_SUBSCRIBERS[gate]["crop"] > 0

            # 1. Cap nhat anh ky tu da doc duoc (chi render lai khi chuoi bien so thay doi)
            if wants_crop:
                plate_text = _get_recognized_plate_text(gate)
                if plate_text != _LAST_RENDERED_PLATE[gate]:
                    chars_jpeg = _render_recognized_chars_jpeg(plate_text)
                    _LAST_RENDERED_PLATE[gate] = plate_text
                    with _STREAM_CACHE_LOCK:
                        _STREAM_CACHE[gate]["crop"] = chars_jpeg

            # 2. Cap nhat luong camera chinh (chi nen khi co frame_time moi)
            if wants_frame:
                frame, updated = _get_gate_preview_frame(gate)
                if updated:
                    if frame is not None:
                        frame = _resize_stream_image(frame, 640, 400)
                        frame_jpeg = _encode_frame(frame, _BLANK_CAM, quality=65)
                    else:
                        frame_jpeg = _BLANK_CAM
                    with _STREAM_CACHE_LOCK:
                        _STREAM_CACHE[gate]["frame"] = frame_jpeg

        elapsed = time.monotonic() - loop_started
        _STREAM_CACHE_STOP.wait(max(0.01, _STREAM_CACHE_INTERVAL - elapsed))


def _ensure_camera_cache_thread():
    global _STREAM_CACHE_THREAD
    if _STREAM_CACHE_THREAD is not None and _STREAM_CACHE_THREAD.is_alive():
        return
    _STREAM_CACHE_STOP.clear()
    _STREAM_CACHE_THREAD = threading.Thread(
        target=_camera_cache_loop, name="WEB-CAMERA-JPEG-CACHE", daemon=True
    )
    _STREAM_CACHE_THREAD.start()


def _mjpeg_generator(gate, kind="frame"):
    key = "crop" if kind == "crop" else "frame"
    interval = 0.25 if key == "crop" else _STREAM_CACHE_INTERVAL
    with _STREAM_CACHE_LOCK:
        _STREAM_SUBSCRIBERS[gate][key] += 1
    try:
        while True:
            with _STREAM_CACHE_LOCK:
                frame_bytes = _STREAM_CACHE[gate][key]
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n" + frame_bytes + b"\r\n"
            )
            time.sleep(interval)
    finally:
        with _STREAM_CACHE_LOCK:
            _STREAM_SUBSCRIBERS[gate][key] = max(0, _STREAM_SUBSCRIBERS[gate][key] - 1)


@app.route("/")
def index():
    return render_template("index.html")


def _money_text(value):
    return "{:,} VND".format(max(0, int(value or 0))).replace(",", ".")


def _duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def _add_live_billing(data):
    """Tinh phi tam tinh phia server, cung quy tac voi storage.calculate_fee()."""
    now = time.time()
    hourly_rate = max(0, int(data.get("fee_per_hour", FEE_PER_HOUR) or 0))
    vehicles = data.get("active_vehicles") or []
    calculated = []
    total_fee = 0

    for source in vehicles:
        vehicle = dict(source)
        parked_ts = vehicle.get("parked_ts")
        try:
            parked_ts = float(parked_ts) if parked_ts not in (None, "") else None
        except (TypeError, ValueError):
            parked_ts = None

        if parked_ts is None:
            vehicle["duration_text"] = "Chua vao o do"
            vehicle["estimated_fee"] = 0
            vehicle["estimated_fee_text"] = "Chua tinh"
            vehicle["billing_status"] = "Dang cho vao o"
        else:
            elapsed_seconds = max(0.0, now - parked_ts)
            billable_hours = max(1, int(math.ceil(elapsed_seconds / 3600.0)))
            estimated_fee = billable_hours * hourly_rate
            vehicle["duration_text"] = _duration_text(elapsed_seconds)
            vehicle["estimated_fee"] = estimated_fee
            vehicle["estimated_fee_text"] = _money_text(estimated_fee)
            vehicle["billing_status"] = "Dang tinh phi"
            total_fee += estimated_fee

        calculated.append(vehicle)

    data["active_vehicles"] = calculated
    data["active_vehicle_count"] = len(calculated)
    data["total_live_fee"] = total_fee
    data["total_live_fee_text"] = _money_text(total_fee)
    data["fee_per_hour"] = hourly_rate
    data["fee_per_hour_text"] = _money_text(hourly_rate) + "/gio"
    return data


@app.route("/api/status")
def api_status():
    with _state_lock:
        data = copy.deepcopy(_web_state)
    with _control_lock:
        provider = _status_provider
    if provider is not None:
        try:
            data.update(provider())
        except Exception as exc:
            data["controller_error"] = str(exc)
    _add_live_billing(data)
    data["server_time"] = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    return jsonify(data)


@app.route("/api/control/start", methods=["POST"])
def api_control_start():
    with _control_lock:
        callback = _on_start
    if callback is None:
        return jsonify({"ok": False, "message": "Chua cau hinh lenh khoi chay"}), 503
    try:
        result = callback()
        status = 200 if result.get("ok") else 409
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route("/api/control/stop", methods=["POST"])
def api_control_stop():
    with _control_lock:
        callback = _on_stop
    if callback is None:
        return jsonify({"ok": False, "message": "Chua cau hinh lenh dung"}), 503
    try:
        result = callback()
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route("/api/session/reset", methods=["POST"])
def api_session_reset():
    payload = request.get_json(silent=True) or {}
    reset_all = bool(payload.get("reset_all"))
    clean_uid = "".join(ch for ch in str(payload.get("uid", "")).upper() if ch in "0123456789ABCDEF")
    clean_plate = "".join(ch for ch in str(payload.get("plate", "")).upper() if ch.isalnum())

    if not reset_all and not clean_uid and not clean_plate:
        return jsonify({"ok": False, "message": "Thiếu thông tin thẻ hoặc biển số cần reset"}), 400

    with _plate_system_lock:
        is_running = _plate_system_ref is not None

    if is_running:
        with _reset_lock:
            _pending_resets.append((clean_uid, clean_plate, reset_all))
        label = "tất cả thẻ" if reset_all else (clean_plate or clean_uid)
        return jsonify({"ok": True, "message": "Đã reset phiên cho " + label}), 200

    try:
        removed = _reset_when_stopped(clean_uid, clean_plate, reset_all)
        return jsonify({"ok": True, "message": "Đã xóa {} phiên thẻ khỏi dữ liệu".format(removed)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "message": "Lỗi reset: {}".format(exc)}), 500


@app.route("/video_feed/<gate>")
def video_feed(gate):
    gate = gate.upper()
    if gate not in ("VAO", "RA"):
        return "Cong camera khong hop le", 404
    return Response(
        _mjpeg_generator(gate, "frame"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "X-Accel-Buffering": "no"},
    )


@app.route("/plate_crop/<gate>")
def plate_crop(gate):
    gate = gate.upper()
    if gate not in ("VAO", "RA"):
        return "Cong camera khong hop le", 404
    return Response(
        _mjpeg_generator(gate, "crop"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "X-Accel-Buffering": "no"},
    )


def set_plate_system(plate_system):
    """Gan/bo doi tuong ANPR dang chay de dashboard lay khung hinh camera."""
    global _plate_system_ref
    with _plate_system_lock:
        _plate_system_ref = plate_system
    _last_frame_times["VAO"] = -1.0
    _last_frame_times["RA"] = -1.0
    _LAST_RENDERED_PLATE["VAO"] = None
    _LAST_RENDERED_PLATE["RA"] = None


def start_web_server(
    plate_system=None, host="0.0.0.0", port=5000,
    on_start=None, on_stop=None, status_provider=None,
):
    """Khoi dong Flask nen va dang ky callback dieu khien controller."""
    global _plate_system_ref, _server_thread, _server_started
    global _on_start, _on_stop, _status_provider

    with _plate_system_lock:
        _plate_system_ref = plate_system
    with _control_lock:
        _on_start = on_start
        _on_stop = on_stop
        _status_provider = status_provider

    _ensure_camera_cache_thread()

    if _server_started and _server_thread is not None and _server_thread.is_alive():
        print("[WEB] Dashboard da chay tren cong {}".format(port), flush=True)
        return _server_thread

    def run_server():
        app.run(
            host=host,
            port=int(port),
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    _server_thread = threading.Thread(
        target=run_server, name="FLASK-DASHBOARD", daemon=True
    )
    _server_thread.start()
    _server_started = True
    print(
        "[WEB] Dashboard dang chay tai http://<IP-RASPBERRY-PI>:{}".format(port),
        flush=True,
    )
    return _server_thread
