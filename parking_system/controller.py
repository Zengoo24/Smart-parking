"""Vong dieu phoi chinh cho hai cong va 5 vi tri do."""

import time

try:
    from smbus import SMBus
except ImportError:
    from smbus2 import SMBus

import RPi.GPIO as GPIO

from .config import (
    CARD_WAIT_TIMEOUT, DISPLAY_EVENT_SECONDS, DISPLAY_TO_PHYSICAL,
    ENTRY_RFID_DEVICE, ENTRY_RFID_RST, EXIT_RFID_DEVICE, EXIT_RFID_RST,
    GATE_CLEAR_STABLE, IR_DEBOUNCE, LCD1602_CHANNEL, LCD2004_CHANNEL,
    LIVE_FILE, LOOP_SLEEP, OE595, RFID_COOLDOWN, SERVO_CLOSED_DUTY,
    SERVO_ENTRY_PIN, SERVO_EXIT_CLOSED_DUTY, SERVO_EXIT_PIN, SERVO_FREQUENCY,
    SLOT_NAMES, UID_SCAN_REQUEST_FILE, UID_SCAN_RESULT_FILE, YELLOW_BEFORE_OPEN,
)
from .lcd_display import I2CLCD
from .hardware import (
    Debouncer, decode_ir, initialize_gpio, move_servo, read_74hc165,
    set_traffic_light, update_slot_leds, write_595,
)
from .rfid import create_reader, read_uid
from .data_store import (
    calculate_fee, elapsed_text, load_active, load_reservation_records,
    log_history, read_uid_scan_json, save_active, write_uid_scan_result,
)
from .parking_lot import ParkingLot
from .reservation_manager import find_reservation_for_uid, set_reservation_status
from .telemetry import publish_live_status, record_rfid_open

def main():

    initialize_gpio()

    bus = None
    servo_entry = None
    servo_exit = None

    try:
        bus = SMBus(1)

        lcd_in = I2CLCD(
            bus, LCD1602_CHANNEL, 16, 2
        )
        lcd_out = I2CLCD(
            bus, LCD2004_CHANNEL, 20, 4
        )

        servo_entry = GPIO.PWM(
            SERVO_ENTRY_PIN, SERVO_FREQUENCY
        )
        servo_exit = GPIO.PWM(
            SERVO_EXIT_PIN, SERVO_FREQUENCY
        )

        servo_entry.start(SERVO_CLOSED_DUTY)
        servo_exit.start(SERVO_EXIT_CLOSED_DUTY)
        time.sleep(0.4)
        servo_entry.ChangeDutyCycle(0)
        servo_exit.ChangeDutyCycle(0)

        reader_entry = create_reader(
            ENTRY_RFID_DEVICE, ENTRY_RFID_RST
        )

        try:
            reader_exit = create_reader(
                EXIT_RFID_DEVICE, EXIT_RFID_RST
            )
        except Exception as exc:
            reader_exit = None
            print("CANH BAO RC522 cong ra:", exc, flush=True)

        active = load_active()
        lot = ParkingLot(active)

        # Khoi tao dau ra; den giao thong bat dau do.
        write_595(0)
        set_traffic_light("entry", "red")
        set_traffic_light("exit", "red")
        GPIO.output(OE595, GPIO.LOW)

        standby = [
            "BAI DO XE THONG MINH",
            "San sang - quet the",
            "",
            "Phi: 5000 VND/gio",
        ]

        event_lines = standby[:]
        event_until = 0.0
        last_lcd_lines = None

        def show_event(lines, seconds=DISPLAY_EVENT_SECONDS):
            nonlocal event_lines, event_until
            event_lines = lines[:]
            event_until = time.monotonic() + seconds

        def open_entry_for_uid(uid, reservation=None, reserved_physical=None):
            nonlocal entry_state
            nonlocal entry_seen_after_open
            nonlocal entry_clear_since

            entry_ts = time.time()

            active[uid] = {
                "entry_ts": entry_ts,
                "parked_ts": None,
                "start_ts": None,
                "slot": None,
                "last_slot": None,
                "awaiting_slot": True,
                "reservation_id": reservation.get("id") if reservation else None,
                "reserved_slot": reserved_physical,
                "reservation_display_slot": (
                    reservation.get("slot") if reservation else None
                ),
            }

            save_active(active)
            log_history("ENTRY", uid, "", None, entry_ts, 0)

            if reservation:
                display_slot = reservation.get("slot", "")
                show_event([
                    "DAT CHO XAC NHAN",
                    "Vi tri: " + str(display_slot),
                    "Cho xe den dung o",
                    "Quet the thanh cong",
                ])
            else:
                show_event([
                    "THE HOP LE - VAO",
                    "Cho IR o do",
                    "Chua tinh gio do",
                    "",
                ])

            set_traffic_light("entry", "yellow")
            time.sleep(YELLOW_BEFORE_OPEN)

            entry_seen_after_open = bool(entry_ir)
            move_servo(servo_entry, True)
            set_traffic_light("entry", "green")

            entry_state = "OPEN"
            record_rfid_open(uid, "CỔNG VÀO")
            entry_clear_since = None

        def render_lcd_out(now, entry_state, exit_state, pending_exit):
            nonlocal last_lcd_lines

            if entry_state == "WAIT_CARD":
                lines = [
                    "CONG VAO: CHO THE",
                    "Hay quet the RFID",
                    "",
                    "",
                ]
            elif exit_state == "WAIT_CARD":
                lines = [
                    "CONG RA: CHO THE",
                    "Hay quet the RFID",
                    "",
                    "",
                ]
            elif exit_state == "OPEN" and pending_exit:
                item = pending_exit
                lines = [
                    "UID: " + item["uid"][:14],
                    "Vi tri: " + str(item["slot"]),
                    "TG do: " + elapsed_text(item["parked_ts"]),
                    "Phi: {:,} VND".format(
                        item["fee"]
                    ).replace(",", "."),
                ]
            elif now < event_until:
                lines = event_lines[:]
            else:
                lines = standby[:]

            if lines != last_lcd_lines:
                lcd_out.display(lines)
                last_lcd_lines = lines[:]

        lcd_in.display(
            ["BAI DO XE SMART", "Dang khoi dong"],
            force=True,
        )
        lcd_out.display(standby, force=True)

        debouncer = Debouncer(7, IR_DEBOUNCE)
        previous_occupied = None
        previous_led_states = None
        startup_time = time.monotonic()
        last_reservation_housekeeping = 0.0

        entry_state = "IDLE"
        exit_state = "IDLE"

        entry_wait_since = 0.0
        exit_wait_since = 0.0

        entry_seen_after_open = False
        exit_seen_after_open = False

        entry_clear_since = None
        exit_clear_since = None

        entry_last_uid = None
        exit_last_uid = None

        entry_last_uid_time = 0.0
        exit_last_uid_time = 0.0

        pending_exit = None
        uid_scan_last_poll = 0.0
        uid_scan_last_request_id = None

        # Trang thai ban dau se duoc ghi sau khi debounce IR san sang.
        print("He thong bai do xe da khoi dong.", flush=True)
        print("IR LOW = co xe.", flush=True)
        print(
            "Dat cho: check-in trong 15 phut sau gio bat dau.",
            flush=True,
        )
        print(
            "Digital Twin: {}".format(LIVE_FILE),
            flush=True,
        )
        print("Nhan Ctrl+C de dung.", flush=True)

        while True:
            now = time.monotonic()

            raw_ir = read_74hc165()
            stable, debounce_ready = debouncer.update(
                decode_ir(raw_ir), now
            )

            occupied = stable[:5]
            entry_ir = stable[5]
            exit_ir = stable[6]

            # Doc UID theo yeu cau tu Digital Twin. Chi doc khi hai cong ranh;
            # thao tac nay chi tra UID, khong tao phien xe va khong mo barie.
            if now - uid_scan_last_poll >= 0.10:
                uid_scan_last_poll = now
                uid_request = read_uid_scan_json(UID_SCAN_REQUEST_FILE)
                request_id = uid_request.get("request_id")
                if request_id and request_id != uid_scan_last_request_id:
                    uid_result = read_uid_scan_json(UID_SCAN_RESULT_FILE)
                    result_is_final = (
                        uid_result.get("request_id") == request_id
                        and uid_result.get("status") in ("FOUND", "TIMEOUT", "BUSY", "ERROR")
                    )
                    if result_is_final:
                        uid_scan_last_request_id = request_id
                    else:
                        try:
                            expires_ts = float(uid_request.get("expires_ts", 0) or 0)
                        except (TypeError, ValueError):
                            expires_ts = 0.0

                        if time.time() >= expires_ts:
                            write_uid_scan_result(
                                request_id, "TIMEOUT",
                                message="Het thoi gian cho the. Hay bam Doc UID de thu lai."
                            )
                            uid_scan_last_request_id = request_id
                        elif entry_state != "IDLE" or exit_state != "IDLE" or entry_ir or exit_ir:
                            write_uid_scan_result(
                                request_id, "BUSY",
                                message="Cong dang ban hoac co xe tai cam bien cong. Hay thu lai khi cong ranh."
                            )
                            uid_scan_last_request_id = request_id
                        else:
                            scanned_uid = read_uid(reader_entry)
                            if scanned_uid:
                                write_uid_scan_result(
                                    request_id, "FOUND", uid=scanned_uid,
                                    message="Da doc UID. Khong tao phien vao bai va khong mo barie."
                                )
                                # Chan doc lap neu xe toi cong ngay lap tuc sau luc dang ky.
                                entry_last_uid = scanned_uid
                                entry_last_uid_time = now
                                uid_scan_last_request_id = request_id
                                print("Digital Twin doc UID (chi doc, khong mo barie):", scanned_uid, flush=True)


            # Chay housekeeping moi 5 giay de tu het han no-show
            # ke ca khi dashboard khong mo.
            if now - last_reservation_housekeeping >= 5.0:
                load_reservation_records()
                last_reservation_housekeeping = now

            # Dong bo trang thai IR khi khoi dong.
            if previous_occupied is None:
                if (
                    debounce_ready
                    and now - startup_time >= IR_DEBOUNCE + 0.1
                ):
                    previous_occupied = occupied[:]

                    print(
                        "IR ban dau:",
                        " ".join(
                            "{}={}".format(
                                SLOT_NAMES[i],
                                "CO XE" if occupied[i] else "TRONG",
                            )
                            for i in range(5)
                        ),
                        flush=True,
                    )

            else:
                for i, (before, current) in enumerate(
                    zip(previous_occupied, occupied)
                ):
                    if not before and current:
                        lot.on_slot_occupied(i)
                    elif before and not current:
                        lot.on_slot_empty(i)

                previous_occupied = occupied[:]

            # Cap nhat LED chi khi trang thai o thay doi.
            led_signature = tuple(occupied)
            if (
                previous_occupied is not None
                and led_signature != previous_led_states
            ):
                update_slot_leds(occupied)
                previous_led_states = led_signature

            free = lot.free_slots(occupied)

            lcd_in.display([
                "Trong: {}/5".format(len(free)),
                " ".join(free) if free else "KHONG CON CHO",
            ])

            # =================================================
            # CONG VAO
            # =================================================

            if entry_state == "IDLE" and entry_ir:
                entry_state = "WAIT_CARD"
                entry_wait_since = now
                set_traffic_light("entry", "red")

                show_event([
                    "CONG VAO: CHO THE",
                    "Hay quet the RFID",
                    "",
                    "",
                ])

            if entry_state == "WAIT_CARD":
                uid = read_uid(reader_entry)

                if uid and (
                    uid != entry_last_uid
                    or now - entry_last_uid_time >= RFID_COOLDOWN
                ):
                    entry_last_uid = uid
                    entry_last_uid_time = now

                    print(
                        "RFID cong vao doc UID: {}".format(uid),
                        flush=True,
                    )

                    # UID da co phien trong bai.
                    if uid in active:
                        record = active[uid]
                        slot = (
                            record.get("slot")
                            or record.get("last_slot")
                            or record.get("reservation_display_slot")
                            or "Chua ro"
                        )

                        show_event([
                            "THE DA CO PHIEN",
                            "Vi tri: " + str(slot),
                            "Khong mo barie",
                            "",
                        ])

                        entry_state = "WAIT_CLEAR"
                        set_traffic_light("entry", "red")

                    else:
                        kind, reservation = find_reservation_for_uid(uid)

                        if kind == "EARLY":
                            show_event([
                                "CHUA DEN GIO DAT",
                                "Vi tri: " + str(reservation.get("slot")),
                                "Quay lai dung gio",
                                "Barie van dong",
                            ])

                            entry_state = "WAIT_CLEAR"
                            set_traffic_light("entry", "red")

                        elif kind == "NO_SHOW":
                            show_event([
                                "DAT CHO HET HAN",
                                "Khach den muon",
                                "Tao dat cho moi",
                                "Barie van dong",
                            ])

                            entry_state = "WAIT_CLEAR"
                            set_traffic_light("entry", "red")

                        elif kind == "ALREADY_ARRIVED":
                            show_event([
                                "DAT CHO DA CHECK-IN",
                                "Khong tao phien moi",
                                "Lien he quan ly",
                                "",
                            ])

                            entry_state = "WAIT_CLEAR"
                            set_traffic_light("entry", "red")

                        elif kind == "VALID":
                            display_slot = str(
                                reservation.get("slot", "")
                            ).upper()

                            physical_slot = DISPLAY_TO_PHYSICAL.get(
                                display_slot
                            )

                            if physical_slot is None:
                                show_event([
                                    "DAT CHO KHONG HOP LE",
                                    "Khong xac dinh o",
                                    "",
                                    "",
                                ])
                                entry_state = "WAIT_CLEAR"
                                set_traffic_light("entry", "red")

                            elif not lot.can_admit_reserved(
                                occupied,
                                physical_slot,
                                uid,
                                reservation.get("id"),
                            ):
                                show_event([
                                    "O DAT CHO KHONG TRONG",
                                    "Vi tri: " + display_slot,
                                    "Lien he quan ly",
                                    "Barie van dong",
                                ])
                                entry_state = "WAIT_CLEAR"
                                set_traffic_light("entry", "red")

                            elif not set_reservation_status(
                                reservation.get("id"),
                                "ARRIVED",
                                {
                                    "arrival_ts": time.time(),
                                    "arrival_physical_slot": physical_slot,
                                },
                            ):
                                show_event([
                                    "LOI XAC NHAN DAT CHO",
                                    "Vui long thu lai",
                                    "",
                                    "",
                                ])
                                entry_state = "WAIT_CLEAR"
                                set_traffic_light("entry", "red")

                            else:
                                open_entry_for_uid(
                                    uid,
                                    reservation,
                                    physical_slot,
                                )

                        else:
                            # Khach khong dat truoc.
                            if not lot.can_admit(occupied):
                                show_event([
                                    "BAI DAY/DA GIU CHO",
                                    "Khong con o thuong",
                                    "Tu choi vao bai",
                                    "",
                                ])

                                entry_state = "WAIT_CLEAR"
                                set_traffic_light("entry", "red")

                            else:
                                open_entry_for_uid(uid)

                if (
                    entry_state == "WAIT_CARD"
                    and now - entry_wait_since >= CARD_WAIT_TIMEOUT
                ):
                    entry_state = "WAIT_CLEAR"
                    set_traffic_light("entry", "red")

                    show_event([
                        "CONG VAO HET GIO",
                        "Chua quet the",
                        "Thu lai khi xe den",
                        "",
                    ])

            elif entry_state == "WAIT_CLEAR":
                if not entry_ir:
                    entry_state = "IDLE"
                    set_traffic_light("entry", "red")

            elif entry_state == "OPEN":
                if entry_ir:
                    entry_seen_after_open = True
                    entry_clear_since = None

                elif entry_seen_after_open:
                    if entry_clear_since is None:
                        entry_clear_since = now
                    elif now - entry_clear_since >= GATE_CLEAR_STABLE:
                        move_servo(servo_entry, False)
                        set_traffic_light("entry", "red")

                        entry_state = "IDLE"
                        entry_seen_after_open = False
                        entry_clear_since = None

            # =================================================
            # CONG RA
            # =================================================

            if exit_state == "IDLE" and exit_ir:
                exit_state = "WAIT_CARD"
                exit_wait_since = now
                set_traffic_light("exit", "red")

                show_event([
                    "CONG RA: CHO THE",
                    "Hay quet the RFID",
                    "",
                    "",
                ])

            if exit_state == "WAIT_CARD" and reader_exit is not None:
                uid_out = read_uid(reader_exit)

                if uid_out and (
                    uid_out != exit_last_uid
                    or now - exit_last_uid_time >= RFID_COOLDOWN
                ):
                    exit_last_uid = uid_out
                    exit_last_uid_time = now

                    print(
                        "RFID cong ra doc UID: {}".format(uid_out),
                        flush=True,
                    )

                    record = active.get(uid_out)

                    if record is None:
                        show_event([
                            "THE CHUA CO PHIEN",
                            "Khong co phien vao",
                            "Barie van dong",
                            "",
                        ])

                        exit_state = "WAIT_CLEAR"
                        set_traffic_light("exit", "red")

                    else:
                        parked_ts = record.get("parked_ts")
                        end_ts = time.time()

                        fee = calculate_fee(parked_ts, end_ts)

                        slot = (
                            record.get("slot")
                            or record.get("last_slot")
                            or record.get("reservation_display_slot")
                            or "Chua do bai"
                        )

                        pending_exit = {
                            "uid": uid_out,
                            "slot": slot,
                            "parked_ts": parked_ts,
                            "end_ts": end_ts,
                            "fee": fee,
                        }

                        show_event([
                            "UID: " + uid_out[:14],
                            "Vi tri: " + str(slot),
                            "TG do: " + elapsed_text(parked_ts),
                            "Phi: {:,} VND".format(fee).replace(",", "."),
                        ])

                        set_traffic_light("exit", "yellow")
                        time.sleep(YELLOW_BEFORE_OPEN)

                        exit_seen_after_open = bool(exit_ir)

                        move_servo(servo_exit, True, exit_gate=True)
                        set_traffic_light("exit", "green")

                        exit_state = "OPEN"
                        record_rfid_open(uid_out, "CỔNG RA")
                        exit_clear_since = None

                if (
                    exit_state == "WAIT_CARD"
                    and now - exit_wait_since >= CARD_WAIT_TIMEOUT
                ):
                    exit_state = "WAIT_CLEAR"
                    set_traffic_light("exit", "red")

                    show_event([
                        "CONG RA HET GIO",
                        "Chua quet the",
                        "Thu lai khi xe den",
                        "",
                    ])

            elif exit_state == "WAIT_CLEAR":
                if not exit_ir:
                    exit_state = "IDLE"
                    set_traffic_light("exit", "red")

            elif exit_state == "OPEN":
                if exit_ir:
                    exit_seen_after_open = True
                    exit_clear_since = None

                elif exit_seen_after_open:
                    if exit_clear_since is None:
                        exit_clear_since = now

                    elif now - exit_clear_since >= GATE_CLEAR_STABLE:
                        item = pending_exit

                        if item is not None:
                            log_history(
                                "EXIT",
                                item["uid"],
                                item["slot"],
                                item["parked_ts"],
                                item["end_ts"],
                                item["fee"],
                            )

                            finished = lot.finish_exit(item["uid"])

                            if finished is not None:
                                print(
                                    "Da ket thuc phien xe.",
                                    flush=True,
                                )

                        pending_exit = None

                        move_servo(servo_exit, False, exit_gate=True)
                        set_traffic_light("exit", "red")

                        exit_state = "IDLE"
                        exit_seen_after_open = False
                        exit_clear_since = None

                        show_event([
                            "XE DA RA BAI",
                            "Giao dich hoan tat",
                            "San sang",
                            "",
                        ])

            # =================================================
            # LCD2004 + DIGITAL TWIN
            # =================================================

            render_lcd_out(
                time.monotonic(),
                entry_state,
                exit_state,
                pending_exit,
            )

            if previous_occupied is not None:
                publish_live_status(
                    occupied,
                    entry_state,
                    exit_state,
                )

            time.sleep(LOOP_SLEEP)

    except KeyboardInterrupt:
        print("\nDa dung bang Ctrl+C.", flush=True)

    finally:
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

        GPIO.cleanup()

        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass

