"""Doc UID tu hai dau doc RFID RC522."""

import RPi.GPIO as GPIO
from mfrc522 import MFRC522

from .config import ENTRY_RFID_DEVICE, ENTRY_RFID_RST, EXIT_RFID_DEVICE, EXIT_RFID_RST

def normalize_uid(value):
    uid = str(value or "").strip().upper()

    for separator in (" ", ":", "-"):
        uid = uid.replace(separator, "")

    if (
        len(uid) not in (8, 14, 20)
        or any(ch not in "0123456789ABCDEF" for ch in uid)
    ):
        return None

    return uid

def create_reader(device, rst_pin):
    constructors = [
        lambda: MFRC522(
            bus=0,
            device=device,
            spd=1000000,
            pin_mode=GPIO.BCM,
            pin_rst=rst_pin,
        ),
        lambda: MFRC522(
            bus=0,
            device=device,
            pin_mode=GPIO.BCM,
            pin_rst=rst_pin,
        ),
    ]

    if device == 0:
        constructors.append(lambda: MFRC522())

    errors = []

    for create in constructors:
        try:
            return create()
        except (TypeError, ValueError, AttributeError) as exc:
            errors.append(str(exc))

    raise RuntimeError(
        "Khong khoi tao duoc RC522 device={} RST GPIO{}: {}".format(
            device, rst_pin, "; ".join(errors)
        )
    )

def read_uid(reader):
    if reader is None:
        return None

    if hasattr(reader, "read_id_no_block"):
        try:
            result = reader.read_id_no_block()

            if result:
                if isinstance(result, (list, tuple)):
                    uid = "".join(
                        "{:02X}".format(int(x) & 0xFF)
                        for x in result[:4]
                    )
                    return normalize_uid(uid)

                if isinstance(result, int):
                    return normalize_uid("{:08X}".format(result))

                return normalize_uid(str(result))

        except Exception:
            pass

    method_pairs = [
        ("MFRC522_Request", "MFRC522_Anticoll"),
        ("Request", "Anticoll"),
        ("request", "anticoll"),
    ]

    for request_name, anticoll_name in method_pairs:
        if not (
            hasattr(reader, request_name)
            and hasattr(reader, anticoll_name)
        ):
            continue

        try:
            request_method = getattr(reader, request_name)
            anticoll_method = getattr(reader, anticoll_name)

            request_code = getattr(
                reader,
                "PICC_REQIDL",
                getattr(reader, "REQIDL", 0x26),
            )

            response = request_method(request_code)
            ok = getattr(reader, "MI_OK", 0)

            if (
                not isinstance(response, (list, tuple))
                or not response
                or response[0] != ok
            ):
                continue

            answer = anticoll_method()

            if not isinstance(answer, (list, tuple)) or len(answer) < 2:
                continue

            status, uid_data = answer[0], answer[1]

            if status != ok or not uid_data:
                continue

            uid = "".join(
                "{:02X}".format(int(x) & 0xFF)
                for x in list(uid_data)[:4]
            )

            return normalize_uid(uid)

        except Exception:
            continue

    return None
