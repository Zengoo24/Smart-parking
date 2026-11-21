#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import signal
from datetime import datetime
import threading
import time

import cv2
import RPi.GPIO as GPIO

try:
    from smbus import SMBus
except ImportError:
    from smbus2 import SMBus

from config import *

from hardware import (
    I2CLCD,
    initialize_gpio,
    write_595,
    update_slot_leds,
    read_74hc165,
    decode_ir,
    Debouncer,
    create_reader,
    read_uid,
    move_servo,
)

from storage import (
    load_active,
    save_active,
    log_history,
    log_plate_history,
    calculate_fee,
    duration_text,
    format_plate,
    normalize_plate,
    valid_plate,
    find_uid_by_plate,
    ParkingLot,
)

from camera_alpr import PlateRecognitionSystem

try:
    from web_dashboard import (
        start_web_server,
        set_plate_system,
        update_web_state,
        format_ts,
        pop_pending_resets,
    )
except ImportError:
    from web_dashboard import (
        start_web_server,
        set_plate_system,
        update_web_state,
        format_ts,
    )

    def pop_pending_resets():
        return []


# RC522 duoc kiem tra theo chu ky 50 ms (toi da 20 lan/giay moi cong).
RFID_POLL_INTERVAL = 0.05
LCD_INIT_RETRIES = 3
LCD_WRITE_RETRIES = 1
LCD_WRITE_ERROR_COOLDOWN = 1.0
# Neu IR cong vao khong con kich hoat sau khi barie mo, tu dong dong sau 8 giay.
ENTRY_OPEN_NO_IR_TIMEOUT = 8.0
WEB_FEE_DISPLAY_SECONDS = 8.0


def get_card_display(uid):
    """Dinh dang UID HEX de hien thi tren website, moi byte cach nhau boi dau cach."""
    if not uid:
        return ""
    cleaned = "".join(ch for ch in str(uid).upper() if ch in "0123456789ABCDEF")
    return " ".join(cleaned[i:i + 2] for i in range(0, len(cleaned), 2))


def build_active_vehicle_payload(active):
    """Tao danh sach xe dang gui cho Web; lay vi tri tu ParkingLot/storage."""
    vehicles = []
    for uid, record in active.items():
        if not isinstance(record, dict):
            continue

        parked_ts = record.get("parked_ts")
        current_slot = record.get("slot")
        if parked_ts is not None and not current_slot:
            current_slot = "Dang di chuyen"

        vehicles.append({
            "plate": format_plate(record.get("plate")) if record.get("plate") else "",
            "rfid": get_card_display(uid),
            "slot": str(current_slot or ""),
            "parked_ts": parked_ts,
            "entry_ts": record.get("entry_ts"),
            "awaiting_slot": bool(record.get("awaiting_slot")),
        })

    vehicles.sort(
        key=lambda vehicle: float(
            vehicle.get("parked_ts") or vehicle.get("entry_ts") or 0
        )
    )
    return vehicles


# ============================================================
# DU LIEU TRANG THAI CHO MQTT BRIDGE
# ============================================================

PARKING_LIVE_FILE = os.path.join(os.path.expanduser("~"), "parking_live.json")
MQTT_LIVE_UPDATE_INTERVAL = 1.0
_last_live_write_error = 0.0


def write_parking_live_state(
    occupied=None,
    sensor_ready=False,
    entry_open=False,
    exit_open=False,
    active_sessions=None,
    controller_running=False,
):
    """Ghi trang thai 4 o do/gate cho bridge; khong ghi UID hoac bien so."""
    global _last_live_write_error

    slots = {}
    for index, slot_name in enumerate(SLOT_NAMES):
        if sensor_ready and occupied is not None and index < len(occupied):
            slots[slot_name] = "OCCUPIED" if bool(occupied[index]) else "FREE"
        else:
            slots[slot_name] = "UNKNOWN"

    gates = {
        "entry": ("OPEN" if entry_open else "CLOSED") if controller_running else "UNKNOWN",
        "exit": ("OPEN" if exit_open else "CLOSED") if controller_running else "UNKNOWN",
    }
    payload = {
        "schema_version": 1,
        "device": "raspberrypi4b",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_at_epoch": time.time(),
        "slots": slots,
        "gates": gates,
        "sensor_ready": bool(sensor_ready and controller_running),
        "controller_running": bool(controller_running),
        "controller_status": "RUNNING" if controller_running else "STOPPED",
        "active_sessions": max(0, int(active_sessions)) if active_sessions is not None else None,
    }

    temp_path = PARKING_LIVE_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, PARKING_LIVE_FILE)
        _last_live_write_error = 0.0
        return True
    except OSError as exc:
        now = time.monotonic()
        if now - _last_live_write_error >= 10.0:
            print("[MQTT LIVE WARN] Khong ghi duoc parking_live.json: {}".format(exc), flush=True)
            _last_live_write_error = now
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


# ============================================================
# QUAN LY TRANG THAI TUNG CONG
# ============================================================

class GateContext:
    """Luu va reset dong bo trang thai cua mot cong VAO/RA."""

    def __init__(self, name):
        self.name = name
        self.last_uid = None
        self.last_uid_time = 0.0
        self.reset()

    def reset(self):
        # Khong reset last_uid/last_uid_time de giu cooldown sau giao dich.
        self.state = "IDLE"
        self.wait_since = 0.0
        self.card_wait_since = None
        self.pre_open_since = 0.0
        self.confirmed_plate = None
        self.pending_uid = None
        self.rfid_poll_time = 0.0
        self.rfid_wait_log = 0.0
        self.seen_after_open = False
        self.opened_at = None
        self.clear_since = None
        self.pending_exit = None

    def to_wait_clear(self):
        self.state = "WAIT_CLEAR"
        self.pending_uid = None


# ============================================================
# LCD: KHOI TAO CO THU LAI VA GHI AN TOAN
# ============================================================

def create_lcd_with_retry(bus, channel, columns, rows, label):
    """Thu khoi tao LCD lai neu TCA9548A/I2C gap loi tam thoi."""
    last_error = None

    for attempt in range(1, LCD_INIT_RETRIES + 1):
        try:
            lcd = I2CLCD(bus, channel, columns, rows)
            print(
                "[LCD] {} khoi tao OK (TCA CH{}, {}x{})".format(
                    label, channel, columns, rows
                ),
                flush=True,
            )
            return lcd
        except OSError as exc:
            last_error = exc
            print(
                "[LCD INIT WARN] {} lan {}/{}: {}".format(
                    label, attempt, LCD_INIT_RETRIES, exc
                ),
                flush=True,
            )
            if attempt < LCD_INIT_RETRIES:
                time.sleep(0.5)

    raise RuntimeError(
        "Khong khoi tao duoc {} sau {} lan thu. Loi cuoi: {}. "
        "Kiem tra TCA9548A dia chi 0x{:02X}, nguon 3.3V/5V, GND, SDA va SCL.".format(
            label, LCD_INIT_RETRIES, last_error, TCA_ADDR
        )
    )


def safe_lcd_display(lcd, lines, label, retry_state, force=False):
    """Ghi LCD; thu lai mot lan va gioi han tan suat khi I2C dang loi."""
    now = time.monotonic()
    if now < retry_state.get(label, 0.0):
        return False

    for attempt in range(LCD_WRITE_RETRIES + 1):
        try:
            lcd.display(lines, force=(force or attempt > 0))
            retry_state[label] = 0.0
            return True
        except OSError as exc:
            print(
                "[LCD I2C WARN] {} loi ghi (lan {}): {}".format(
                    label, attempt + 1, exc
                ),
                flush=True,
            )
            if attempt < LCD_WRITE_RETRIES:
                try:
                    recover = getattr(lcd, "recover", None)
                    if callable(recover):
                        recover()
                except OSError as recover_exc:
                    print(
                        "[LCD RECOVERY WARN] {}: {}".format(label, recover_exc),
                        flush=True,
                    )
                time.sleep(0.05)
        except Exception as exc:
            print("[LCD ERROR] {}: {}".format(label, exc), flush=True)
            break

    retry_state[label] = time.monotonic() + LCD_WRITE_ERROR_COOLDOWN
    return False


# ============================================================
# CHUONG TRINH CHINH
# ============================================================

def run_controller(stop_event):
    """Chay phan cung trong worker thread; Flask dashboard quan ly start/stop."""
    initialize_gpio()

    bus = None
    servo_entry = None
    servo_exit = None
    reader_entry = None
    reader_exit = None
    plate_system = None

    try:
        # ----------------------------------------------------
        # 1. KHOI TAO I2C VA MOT LCD1602 DUY NHAT
        # ----------------------------------------------------
        bus = SMBus(1)
        lcd = create_lcd_with_retry(
            bus, LCD1602_CHANNEL, 16, 2, "LCD1602 DUY NHAT"
        )

        # ----------------------------------------------------
        # 2. KHOI TAO SERVO - GIU NGUYEN THONG SO DA TEST
        # ----------------------------------------------------
        servo_entry = GPIO.PWM(SERVO_ENTRY_PIN, SERVO_FREQUENCY)
        servo_exit = GPIO.PWM(SERVO_EXIT_PIN, SERVO_FREQUENCY)

        servo_entry.start(SERVO_ENTRY_CLOSED_DUTY)
        servo_exit.start(SERVO_EXIT_CLOSED_DUTY)
        time.sleep(0.4)
        servo_entry.ChangeDutyCycle(0)
        servo_exit.ChangeDutyCycle(0)

        # ----------------------------------------------------
        # 3. KHOI TAO RFID
        # ----------------------------------------------------
        try:
            reader_entry = create_reader(ENTRY_RFID_DEVICE, ENTRY_RFID_RST)
            print("[RFID] RC522 CONG VAO KHOI TAO OK", flush=True)
        except Exception as exc:
            print("[RFID ERROR] CONG VAO: {}".format(exc), flush=True)

        try:
            reader_exit = create_reader(EXIT_RFID_DEVICE, EXIT_RFID_RST)
            print("[RFID] RC522 CONG RA KHOI TAO OK", flush=True)
        except Exception as exc:
            print("[RFID ERROR] CONG RA: {}".format(exc), flush=True)

        # ----------------------------------------------------
        # 4. DU LIEU, O DO VA ANPR
        # ----------------------------------------------------
        active = load_active()
        lot = ParkingLot(active)

        plate_system = PlateRecognitionSystem()
        set_plate_system(plate_system)

        update_web_state(
            occupied_count=0,
            total_slots=len(SLOT_NAMES),
            active_vehicles=build_active_vehicle_payload(active),
            system_status="San sang - Cho xe",
        )

        # ----------------------------------------------------
        # 5. KHOI TAO LED 74HC595
        # ----------------------------------------------------
        write_595(0x00)
        GPIO.output(OE595, GPIO.LOW)

        # ----------------------------------------------------
        # 6. LCD1602 CHI HIEN THI TINH TRANG BAI DO
        # ----------------------------------------------------
        last_lcd_lines = None
        last_web_active_vehicles = None
        last_live_write = 0.0

        lcd_retry_state = {"LCD1602": 0.0}

        e_gate = GateContext("VAO")
        x_gate = GateContext("RA")

        # Hien phi giao dich 8 giay sau khi xe ra, sau do tu reset ve 0.
        fee_reset_deadline = None

        def stop_plate_session(gate_name):
            if plate_system is None:
                return
            try:
                if plate_system.states[gate_name]["active"]:
                    plate_system.end_session(gate_name)
            except Exception as exc:
                print(
                    "[CAMERA SESSION ERROR] {}: {}".format(gate_name, exc),
                    flush=True,
                )

        def clear_plate_session(gate_name):
            if plate_system is None:
                return
            try:
                stop_plate_session(gate_name)
                plate_system.clear_plate(gate_name)
            except Exception as exc:
                print(
                    "[CAMERA CLEAR ERROR] {}: {}".format(gate_name, exc),
                    flush=True,
                )

        def render_lcd(occupied, force=False):
            """LCD1602 chi hien thi so cho trong va danh sach o trong."""
            nonlocal last_lcd_lines

            free = lot.free_slots(occupied)
            lines = [
                "Trong: {}/4".format(len(free)),
                " ".join(free) if free else "KHONG CON CHO",
            ]

            if force or lines != last_lcd_lines:
                if safe_lcd_display(
                    lcd, lines, "LCD1602", lcd_retry_state, force=force
                ):
                    last_lcd_lines = lines[:]

        # ----------------------------------------------------
        # 7. KHOI TAO IR VA HIEN THI LCD BAN DAU
        # ----------------------------------------------------
        debouncer = Debouncer(6, IR_DEBOUNCE)
        previous_occupied = None
        startup_time = time.monotonic()

        safe_lcd_display(
            lcd,
            ["BAI DO XE 4 O", "Dang doc cam bien"],
            "LCD1602",
            lcd_retry_state,
            force=True,
        )
        last_lcd_lines = None

        print("\n========================================", flush=True)
        print("BAI DO XE THONG MINH - 4 O", flush=True)
        print("2 CONG - 2 RC522 - 1 LCD1602", flush=True)
        print("CAMERA VAO: {}".format(ENTRY_CAMERA_DEVICE), flush=True)
        print("CAMERA RA: {}".format(EXIT_CAMERA_DEVICE), flush=True)
        print("ANPR: CONTOUR + PERSPECTIVE WARP + TEMPLATE MATCHING", flush=True)
        print("IR LOW = CO XE | STATE MACHINE", flush=True)
        print("TRINH TU: IR -> ANPR -> RFID -> DOI CHIEU -> SERVO", flush=True)
        print("WEB DASHBOARD: http://<IP-RASPBERRY-PI>:5000", flush=True)
        print("========================================\n", flush=True)

        # ====================================================
        # 8. VONG LAP CHINH
        # ====================================================
        update_web_state(controller_status="RUNNING", system_status="He thong dang hoat dong")

        while not stop_event.is_set():
            now = time.monotonic()

            if fee_reset_deadline is not None and now >= fee_reset_deadline:
                update_web_state(last_fee="0 VND", is_exiting=False)
                fee_reset_deadline = None
                print("[WEB] Da reset phi giao dich ve 0 VND", flush=True)

            # ------------------------------------------------
            # 8.1 DOC CAM BIEN IR QUA 74HC165
            # ------------------------------------------------
            raw_value = read_74hc165()
            ir_values = decode_ir(raw_value)
            states, debounce_ready = debouncer.update(ir_values, now)

            occupied = states[:4]
            entry_ir = states[4]
            exit_ir = states[5]
            update_web_state(
                occupied_count=sum(1 for value in occupied if value),
                total_slots=len(SLOT_NAMES),
            )

            if previous_occupied is None:
                if debounce_ready and (now - startup_time) >= (IR_DEBOUNCE + 0.1):
                    previous_occupied = occupied[:]
                    print(
                        "IR ban dau:",
                        " ".join(
                            "{}={}".format(
                                SLOT_NAMES[i], "XE" if occupied[i] else "TRONG"
                            )
                            for i in range(4)
                        ),
                        flush=True,
                    )
            else:
                for i, (before, current) in enumerate(zip(previous_occupied, occupied)):
                    if not before and current:
                        assigned_uid = lot.on_slot_occupied(i)
                        if assigned_uid:
                            record = active.get(assigned_uid, {})
                            update_web_state(
                                entry_plate=format_plate(record.get("plate")) if record.get("plate") else "",
                                entry_rfid=get_card_display(assigned_uid),
                                last_entry_time=format_ts(record.get("parked_ts") or record.get("entry_ts")),
                                system_status="Xe da vao vi tri {}".format(SLOT_NAMES[i]),
                            )
                    elif before and not current:
                        lot.on_slot_empty(i)
                previous_occupied = occupied[:]

            # ------------------------------------------------
            # 8.2 CAP NHAT CAMERA ANPR VA LED O DO
            # ------------------------------------------------
            if e_gate.state in ("IDLE", "WAIT_CARD"):
                plate_system.update_presence("VAO", entry_ir, now)

            if x_gate.state in ("IDLE", "WAIT_CARD"):
                plate_system.update_presence("RA", exit_ir, now)

            plate_system.collect_results()
            update_slot_leds(occupied)

            # =================================================
            # 9. XU LY CONG VAO
            # =================================================
            if e_gate.state == "IDLE" and entry_ir:
                e_gate.reset()
                e_gate.state = "WAIT_CARD"
                e_gate.wait_since = now
                update_web_state(
                    entry_plate="",
                    entry_rfid="",
                    system_status="Cong vao: dang nhan dien bien so",
                )
                print("\n[ENTRY] XE DEN CONG VAO - BAT DAU ANPR", flush=True)

            elif e_gate.state == "WAIT_CARD":
                # 9.1 Nhan dien bien so.
                if e_gate.confirmed_plate is None:
                    detected_plate = plate_system.get_plate("VAO")
                    if detected_plate:
                        norm_plate = normalize_plate(detected_plate)
                        if valid_plate(norm_plate):
                            e_gate.confirmed_plate = norm_plate
                            e_gate.card_wait_since = now
                            update_web_state(
                                entry_plate=format_plate(norm_plate),
                                entry_rfid="",
                                system_status="Cong vao: da nhan dien bien so - moi quet RFID",
                            )
                            print(
                                "[ENTRY] DA NHAN DIEN BIEN SO {} - MOI QUET RFID".format(norm_plate),
                                flush=True,
                            )

                # 9.2 Quet RFID theo chu ky 50 ms, chi sau khi da co bien so.
                if (
                    e_gate.confirmed_plate
                    and e_gate.pending_uid is None
                    and (now - e_gate.rfid_poll_time) >= RFID_POLL_INTERVAL
                ):
                    e_gate.rfid_poll_time = now
                    if reader_entry is None:
                        if (now - e_gate.rfid_wait_log) >= 2.0:
                            print("[RFID ERROR] RC522 CONG VAO CHUA KHOI TAO", flush=True)
                            e_gate.rfid_wait_log = now
                    else:
                        uid = read_uid(reader_entry)
                        if uid:
                            if uid != e_gate.last_uid or (now - e_gate.last_uid_time) >= RFID_COOLDOWN:
                                e_gate.last_uid = uid
                                e_gate.last_uid_time = now
                                update_web_state(
                                    entry_rfid=get_card_display(uid),
                                    system_status="Cong vao: da doc UID RFID - dang doi chieu",
                                )
                                print("[RFID VAO] UID={}".format(uid), flush=True)

                                if uid in active:
                                    print("[ENTRY DENY] UID DA CO PHIEN", flush=True)
                                    update_web_state(system_status="Tu choi vao: the RFID dang co phien gui")
                                    stop_plate_session("VAO")
                                    e_gate.to_wait_clear()

                                elif not lot.can_admit(occupied):
                                    print("[ENTRY DENY] BAI DA DAY", flush=True)
                                    update_web_state(system_status="Tu choi vao: bai da day")
                                    stop_plate_session("VAO")
                                    e_gate.to_wait_clear()

                                else:
                                    e_gate.pending_uid = uid
                                    print("[ENTRY] RFID HOP LE - DANG DOI CHIEU", flush=True)
                        elif (now - e_gate.rfid_wait_log) >= 2.0:
                            print("[RFID VAO] DANG CHO THE RFID", flush=True)
                            e_gate.rfid_wait_log = now

                # 9.3 Doi chieu bien so va UID.
                if (
                    e_gate.state == "WAIT_CARD"
                    and e_gate.confirmed_plate
                    and e_gate.pending_uid
                ):
                    uid = e_gate.pending_uid
                    plate = e_gate.confirmed_plate
                    duplicate_uid = find_uid_by_plate(active, plate)

                    if duplicate_uid is not None and duplicate_uid != uid:
                        print("[ENTRY DENY] BIEN SO DA TON TAI: {}".format(plate), flush=True)
                        stop_plate_session("VAO")
                        e_gate.to_wait_clear()

                    elif not lot.can_admit(occupied):
                        stop_plate_session("VAO")
                        e_gate.to_wait_clear()

                    else:
                        timestamp = time.time()
                        active[uid] = {
                            "entry_ts": timestamp,
                            "parked_ts": None,
                            "start_ts": None,
                            "slot": None,
                            "last_slot": None,
                            "awaiting_slot": True,
                            "plate": plate,
                        }

                        save_active(active)
                        log_history("ENTRY", uid, "", None, timestamp, 0)
                        log_plate_history("ENTRY", plate, uid, "", None, timestamp, 0)

                        update_web_state(
                            entry_plate=format_plate(plate),
                            entry_rfid=get_card_display(uid),
                            last_entry_time=format_ts(timestamp),
                            system_status="Xe vao hop le - dang mo barie vao",
                        )

                        print("[ENTRY ACCEPT] UID={} PLATE={}".format(uid, plate), flush=True)

                        stop_plate_session("VAO")
                        e_gate.state = "PRE_OPEN"
                        e_gate.pre_open_since = now

                # 9.4 Timeout nhan dien hoac timeout quet the.
                if (
                    e_gate.state == "WAIT_CARD"
                    and e_gate.confirmed_plate is None
                    and (now - e_gate.wait_since) >= CARD_WAIT_TIMEOUT
                ):
                    print("[ENTRY TIMEOUT] KHONG NHAN DIEN DUOC BIEN SO", flush=True)
                    update_web_state(system_status="Cong vao: het thoi gian nhan dien bien so")
                    stop_plate_session("VAO")
                    e_gate.to_wait_clear()

                elif (
                    e_gate.state == "WAIT_CARD"
                    and e_gate.confirmed_plate is not None
                    and e_gate.pending_uid is None
                    and e_gate.card_wait_since is not None
                    and (now - e_gate.card_wait_since) >= CARD_WAIT_TIMEOUT
                ):
                    print("[ENTRY TIMEOUT] KHONG QUET THE RFID", flush=True)
                    update_web_state(system_status="Cong vao: het thoi gian cho quet RFID")
                    stop_plate_session("VAO")
                    e_gate.to_wait_clear()

            elif e_gate.state == "PRE_OPEN":
                if (now - e_gate.pre_open_since) >= GATE_PRE_OPEN_DELAY:
                    e_gate.seen_after_open = bool(entry_ir)
                    move_servo(servo_entry, True)
                    e_gate.opened_at = time.monotonic()
                    e_gate.state = "OPEN"
                    e_gate.clear_since = None
                    update_web_state(system_status="Barie cong vao dang mo - cho xe qua")
                    print("[SERVO VAO] DA GUI LENH MO GPIO13", flush=True)

            elif e_gate.state == "WAIT_CLEAR":
                if not entry_ir:
                    e_gate.reset()
                    clear_plate_session("VAO")

            elif e_gate.state == "OPEN":
                if entry_ir:
                    e_gate.seen_after_open = True
                    e_gate.clear_since = None
                elif e_gate.seen_after_open:
                    if e_gate.clear_since is None:
                        e_gate.clear_since = now
                    elif (now - e_gate.clear_since) >= GATE_CLEAR_STABLE:
                        move_servo(servo_entry, False)
                        print("[SERVO VAO] DA DONG BARIE", flush=True)
                        update_web_state(system_status="Barie cong vao da dong")
                        e_gate.reset()
                        clear_plate_session("VAO")
                elif (
                    e_gate.opened_at is not None
                    and (now - e_gate.opened_at) >= ENTRY_OPEN_NO_IR_TIMEOUT
                ):
                    move_servo(servo_entry, False)
                    print(
                        "[SERVO VAO] FAILSAFE DONG BARIE: IR KHONG KICH HOAT SAU KHI MO",
                        flush=True,
                    )
                    update_web_state(system_status="Barie cong vao da dong (failsafe)")
                    e_gate.reset()
                    clear_plate_session("VAO")

            # =================================================
            # 10. XU LY CONG RA
            # =================================================
            if x_gate.state == "IDLE" and exit_ir:
                x_gate.reset()
                x_gate.state = "WAIT_CARD"
                x_gate.wait_since = now
                update_web_state(
                    exit_plate="",
                    exit_rfid="",
                    system_status="Cong ra: dang nhan dien bien so",
                )
                print("\n[EXIT] XE DEN CONG RA - BAT DAU ANPR", flush=True)

            elif x_gate.state == "WAIT_CARD":
                # 10.1 Nhan dien bien so.
                if x_gate.confirmed_plate is None:
                    detected_plate = plate_system.get_plate("RA")
                    if detected_plate:
                        norm_plate = normalize_plate(detected_plate)
                        if valid_plate(norm_plate):
                            x_gate.confirmed_plate = norm_plate
                            x_gate.card_wait_since = now
                            update_web_state(
                                exit_plate=format_plate(norm_plate),
                                exit_rfid="",
                                system_status="Cong ra: da nhan dien bien so - moi quet RFID",
                            )
                            print(
                                "[EXIT] DA NHAN DIEN BIEN SO {} - MOI QUET RFID".format(norm_plate),
                                flush=True,
                            )

                # 10.2 Quet RFID theo chu ky 50 ms, chi sau khi da co bien so.
                if (
                    x_gate.confirmed_plate
                    and x_gate.pending_uid is None
                    and (now - x_gate.rfid_poll_time) >= RFID_POLL_INTERVAL
                ):
                    x_gate.rfid_poll_time = now
                    if reader_exit is None:
                        if (now - x_gate.rfid_wait_log) >= 2.0:
                            print("[RFID ERROR] RC522 CONG RA CHUA KHOI TAO", flush=True)
                            x_gate.rfid_wait_log = now
                    else:
                        uid = read_uid(reader_exit)
                        if uid:
                            if uid != x_gate.last_uid or (now - x_gate.last_uid_time) >= RFID_COOLDOWN:
                                x_gate.last_uid = uid
                                x_gate.last_uid_time = now
                                update_web_state(
                                    exit_rfid=get_card_display(uid),
                                    system_status="Cong ra: da doc UID RFID - dang doi chieu",
                                )
                                print("[RFID RA] UID={}".format(uid), flush=True)

                                if uid not in active:
                                    print("[EXIT DENY] UID KHONG CO PHIEN", flush=True)
                                    update_web_state(system_status="Tu choi ra: UID khong co phien gui xe")
                                    stop_plate_session("RA")
                                    x_gate.to_wait_clear()
                                else:
                                    x_gate.pending_uid = uid
                                    print("[EXIT] RFID HOP LE - DANG DOI CHIEU", flush=True)
                        elif (now - x_gate.rfid_wait_log) >= 2.0:
                            print("[RFID RA] DANG CHO THE RFID", flush=True)
                            x_gate.rfid_wait_log = now

                # 10.3 Doi chieu bien so va UID.
                if (
                    x_gate.state == "WAIT_CARD"
                    and x_gate.pending_uid
                    and x_gate.confirmed_plate
                ):
                    uid = x_gate.pending_uid
                    plate = x_gate.confirmed_plate
                    rec = active.get(uid)

                    if rec is None:
                        stop_plate_session("RA")
                        x_gate.to_wait_clear()
                    else:
                        stored_plate = normalize_plate(rec.get("plate"))

                        if stored_plate and stored_plate != plate:
                            print(
                                "[EXIT DENY] UID={} SAVED={} CAMERA={}".format(
                                    uid, stored_plate, plate
                                ),
                                flush=True,
                            )
                            update_web_state(system_status="Tu choi ra: bien so khong khop UID")
                            stop_plate_session("RA")
                            x_gate.to_wait_clear()
                        else:
                            if not stored_plate:
                                rec["plate"] = plate
                                save_active(active)
                                print(
                                    "[EXIT MIGRATE] BO SUNG BIEN SO CHO UID {}".format(uid),
                                    flush=True,
                                )

                            parked_ts = rec.get("parked_ts")
                            fee = calculate_fee(parked_ts, time.time())
                            slot = rec.get("slot") or rec.get("last_slot") or "Chua do bai"

                            x_gate.pending_exit = {
                                "uid": uid,
                                "plate": plate,
                                "slot": slot,
                                "parked_ts": parked_ts,
                                "end_ts": None,
                                "fee": fee,
                            }

                            fee_reset_deadline = None
                            update_web_state(
                                exit_plate=format_plate(plate),
                                exit_rfid=get_card_display(uid),
                                last_entry_time=format_ts(parked_ts),
                                last_fee="{:,} VND".format(fee).replace(",", "."),
                                system_status="Da doi chieu bien so va RFID - dang mo barie ra",
                                exit_slot=str(slot),
                                receipt_plate=format_plate(plate),
                                receipt_rfid=get_card_display(uid),
                                receipt_slot=str(slot),
                                receipt_entry_time=format_ts(parked_ts),
                                receipt_exit_time="Dang xu ly",
                                receipt_duration="Dang xu ly",
                                receipt_fee="{:,} VND".format(fee).replace(",", "."),
                                is_exiting=True,
                            )

                            print(
                                "[EXIT MATCH] UID={} PLATE={} SLOT={} FEE={}".format(
                                    uid, plate, slot, fee
                                ),
                                flush=True,
                            )

                            stop_plate_session("RA")
                            x_gate.state = "PRE_OPEN"
                            x_gate.pre_open_since = now

                # 10.4 Timeout nhan dien hoac timeout quet the.
                if (
                    x_gate.state == "WAIT_CARD"
                    and x_gate.confirmed_plate is None
                    and (now - x_gate.wait_since) >= CARD_WAIT_TIMEOUT
                ):
                    print("[EXIT TIMEOUT] KHONG NHAN DIEN DUOC BIEN SO", flush=True)
                    update_web_state(system_status="Cong ra: het thoi gian nhan dien bien so")
                    stop_plate_session("RA")
                    x_gate.to_wait_clear()

                elif (
                    x_gate.state == "WAIT_CARD"
                    and x_gate.confirmed_plate is not None
                    and x_gate.pending_uid is None
                    and x_gate.card_wait_since is not None
                    and (now - x_gate.card_wait_since) >= CARD_WAIT_TIMEOUT
                ):
                    print("[EXIT TIMEOUT] KHONG QUET THE RFID", flush=True)
                    update_web_state(system_status="Cong ra: het thoi gian cho quet RFID")
                    stop_plate_session("RA")
                    x_gate.to_wait_clear()

            elif x_gate.state == "PRE_OPEN":
                if (now - x_gate.pre_open_since) >= GATE_PRE_OPEN_DELAY:
                    x_gate.seen_after_open = bool(exit_ir)
                    move_servo(servo_exit, True, gate="exit")
                    x_gate.state = "OPEN"
                    x_gate.clear_since = None
                    print("[SERVO RA] DA GUI LENH MO GPIO18", flush=True)

            elif x_gate.state == "WAIT_CLEAR":
                if not exit_ir:
                    x_gate.reset()
                    clear_plate_session("RA")

            elif x_gate.state == "OPEN":
                if exit_ir:
                    x_gate.seen_after_open = True
                    x_gate.clear_since = None
                elif x_gate.seen_after_open:
                    if x_gate.clear_since is None:
                        x_gate.clear_since = now
                    elif (now - x_gate.clear_since) >= GATE_CLEAR_STABLE:
                        item = x_gate.pending_exit

                        if item is not None:
                            item["end_ts"] = time.time()
                            item["fee"] = calculate_fee(
                                item["parked_ts"], item["end_ts"]
                            )
                            final_duration = duration_text(
                                item["parked_ts"], item["end_ts"]
                            )

                            log_history(
                                "EXIT",
                                item["uid"],
                                item["slot"],
                                item["parked_ts"],
                                item["end_ts"],
                                item["fee"],
                            )
                            log_plate_history(
                                "EXIT",
                                item["plate"],
                                item["uid"],
                                item["slot"],
                                item["parked_ts"],
                                item["end_ts"],
                                item["fee"],
                            )

                            lot.finish_exit(item["uid"])
                            update_web_state(
                                exit_plate=format_plate(item["plate"]),
                                exit_rfid=get_card_display(item["uid"]),
                                last_entry_time=format_ts(item["parked_ts"]),
                                last_exit_time=format_ts(item["end_ts"]),
                                last_duration=final_duration,
                                last_fee="{:,} VND".format(item["fee"]).replace(",", "."),
                                system_status="Xe da ra - hoan tat giao dich",
                                exit_slot=str(item.get("slot") or ""),
                                receipt_plate=format_plate(item["plate"]),
                                receipt_rfid=get_card_display(item["uid"]),
                                receipt_slot=str(item.get("slot") or ""),
                                receipt_entry_time=format_ts(item["parked_ts"]),
                                receipt_exit_time=format_ts(item["end_ts"]),
                                receipt_duration=final_duration,
                                receipt_fee="{:,} VND".format(item["fee"]).replace(",", "."),
                                is_exiting=True,
                            )
                            fee_reset_deadline = time.monotonic() + WEB_FEE_DISPLAY_SECONDS

                            print(
                                "[EXIT COMPLETE] UID={} PLATE={} TIME={} FEE={}".format(
                                    item["uid"],
                                    item["plate"],
                                    final_duration,
                                    item["fee"],
                                ),
                                flush=True,
                            )

                        move_servo(servo_exit, False, gate="exit")
                        print("[SERVO RA] DA DONG BARIE", flush=True)

                        x_gate.reset()
                        clear_plate_session("RA")

            # =================================================
            # 11. XU LY RESET THE TU WEB & DONG BO DANH SACH XE
            # =================================================
            for reset_uid, reset_plate, reset_all in pop_pending_resets():
                matched_uids = []
                for uid_key, rec in list(active.items()):
                    rec_plate = normalize_plate(rec.get("plate")) if isinstance(rec, dict) else ""
                    if reset_all or (reset_uid and uid_key == reset_uid) or (reset_plate and rec_plate == reset_plate):
                        matched_uids.append(uid_key)

                for uid_key in matched_uids:
                    old_plate = active[uid_key].get("plate", "")
                    lot.finish_exit(uid_key)
                    print(
                        "[WEB RESET] Da xoa phien UID={} PLATE={}".format(uid_key, old_plate),
                        flush=True,
                    )

                if matched_uids:
                    update_web_state(
                        system_status="Da reset {} the khoi he thong".format(len(matched_uids))
                    )

            web_active_vehicles = build_active_vehicle_payload(active)
            if web_active_vehicles != last_web_active_vehicles:
                update_web_state(active_vehicles=web_active_vehicles)
                last_web_active_vehicles = web_active_vehicles

            if (now - last_live_write) >= MQTT_LIVE_UPDATE_INTERVAL:
                write_parking_live_state(
                    occupied=occupied,
                    sensor_ready=debounce_ready,
                    entry_open=(e_gate.state == "OPEN"),
                    exit_open=(x_gate.state == "OPEN"),
                    active_sessions=len(active),
                    controller_running=True,
                )
                last_live_write = now

            # =================================================
            # 12. CAP NHAT LCD1602 DUY NHAT - CHI HIEN THI BAI DO
            # =================================================
            render_lcd(occupied)

            time.sleep(LOOP_SLEEP)

    except Exception as exc:
        print(
            "\n[MAIN ERROR] {}: {}".format(type(exc).__name__, exc),
            flush=True,
        )
        raise

    finally:
        try:
            set_plate_system(None)
        except Exception:
            pass

        if plate_system is not None:
            try:
                plate_system.close()
            except Exception as exc:
                print("[CAMERA CLEANUP ERROR] {}".format(exc), flush=True)

        try:
            cv2.destroyAllWindows()
            cv2.waitKey(1)
        except Exception:
            pass

        for pwm in (servo_entry, servo_exit):
            if pwm is not None:
                try:
                    pwm.ChangeDutyCycle(0)
                    pwm.stop()
                except Exception:
                    pass

        try:
            GPIO.output(OE595, GPIO.HIGH)
        except Exception:
            pass

        try:
            GPIO.cleanup()
        except Exception:
            pass

        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

        write_parking_live_state(
            active_sessions=len(locals().get("active", {})),
            controller_running=False,
        )

        update_web_state(
            controller_status="STOPPED",
            system_status="He thong da dung - Dashboard van san sang",
        )
        print("Da giai phong GPIO, camera, LCD1602 va servo.", flush=True)


class ControllerManager:
    """Quan ly worker phan cung; Flask process van chay khi controller dung."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread = None
        self._stop_event = None
        self._state = "STOPPED"
        self._last_error = ""

    def status(self):
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            state = self._state
            if not alive and state not in ("ERROR", "STOPPED"):
                state = "STOPPED"
                self._state = state
            return {
                "controller_status": state,
                "controller_running": alive,
                "controller_error": self._last_error,
            }

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "state": self._state, "message": "He thong dang chay hoac dang dung"}

            self._stop_event = threading.Event()
            self._last_error = ""
            self._state = "STARTING"
            self._thread = threading.Thread(
                target=self._worker,
                args=(self._stop_event,),
                name="PARKING-HARDWARE-CONTROLLER",
                daemon=True,
            )
            self._thread.start()

        update_web_state(
            controller_status="STARTING",
            controller_error="",
            system_status="Dang khoi dong he thong...",
        )
        print("[WEB CONTROL] Nhan lenh KHOI CHAY", flush=True)
        return {"ok": True, "state": "STARTING", "message": "Da gui lenh khoi chay"}

    def _worker(self, stop_event):
        with self._lock:
            self._state = "RUNNING"
        try:
            run_controller(stop_event)
        except Exception as exc:
            error_text = "{}: {}".format(type(exc).__name__, exc)
            with self._lock:
                self._state = "ERROR"
                self._last_error = error_text
            update_web_state(
                controller_status="ERROR",
                controller_error=error_text,
                system_status="Loi he thong: {}".format(error_text),
            )
            print("[CONTROLLER ERROR] {}".format(error_text), flush=True)
        else:
            with self._lock:
                self._state = "STOPPED"
            update_web_state(
                controller_status="STOPPED",
                system_status="He thong da dung - Dashboard van san sang",
            )
        finally:
            with self._lock:
                if self._state != "ERROR":
                    self._state = "STOPPED"

    def stop(self):
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "STOPPED"
                update_web_state(
                    controller_status="STOPPED",
                    system_status="He thong dang dung - Dashboard van san sang",
                )
                return {"ok": True, "state": "STOPPED", "message": "He thong dang dung"}

            self._state = "STOPPING"
            if self._stop_event is not None:
                self._stop_event.set()

        update_web_state(
            controller_status="STOPPING",
            system_status="Dang dung he thong va giai phong phan cung...",
        )
        print("[WEB CONTROL] Nhan lenh DUNG HE THONG", flush=True)
        thread.join(timeout=20.0)

        with self._lock:
            if thread.is_alive():
                return {"ok": True, "state": "STOPPING", "message": "Dang cho giai phong camera/GPIO"}
            if self._state != "ERROR":
                self._state = "STOPPED"
            state = self._state

        return {
            "ok": True,
            "state": state,
            "message": "Da dung he thong" if state == "STOPPED" else "He thong ket thuc voi loi",
        }


def main():
    """Dashboard chay nen lien tuc; controller chi chay khi bam nut tren web."""
    manager = ControllerManager()
    shutdown_event = threading.Event()

    def handle_shutdown(signum, _frame):
        print("\n[SYSTEM] Nhan tin hieu dung dashboard ({})".format(signum), flush=True)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    write_parking_live_state(controller_running=False)

    start_web_server(
        plate_system=None,
        host="0.0.0.0",
        port=5000,
        on_start=manager.start,
        on_stop=manager.stop,
        status_provider=manager.status,
    )
    update_web_state(
        controller_status="STOPPED",
        controller_error="",
        occupied_count=0,
        total_slots=len(SLOT_NAMES),
        system_status="Dashboard san sang - Nhan KHOI CHAY de bat dau",
    )

    print("\n========================================", flush=True)
    print("PARKING WEB CONTROL DANG CHAY NEN", flush=True)
    print("Controller chua khoi dong. Dieu khien tren website.", flush=True)
    print("Truy cap: http://<IP-RASPBERRY-PI>:5000", flush=True)
    print("========================================\n", flush=True)

    try:
        while not shutdown_event.wait(1.0):
            pass
    finally:
        manager.stop()
        print("[SYSTEM] Da dung Parking Web Control", flush=True)


if __name__ == "__main__":
    main()
