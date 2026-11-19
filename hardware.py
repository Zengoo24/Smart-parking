#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""Phan cung cho bai do xe thong minh 4 o.

Giu nguyen:

- GPIO BCM va cac chan trong config.py.

- Thu tu bit 74HC165 va mapping LED 74HC595.

- Cach khoi tao RC522 va API doc UID.

- Thong so servo da duoc nguoi dung test.

Toi uu chinh:

- LCD dung khoa I2C chung va ghi lai dong khi noi dung thay doi.

- Kiem tra dau vao de loi ro rang hon.

- Khoa thao tac shift-register de tranh chen lenh neu sau nay co them thread.

- Debouncer xu ly du lieu theo dung so kenh va co reset.

"""

import threading

import time

import RPi.GPIO as GPIO

from mfrc522 import MFRC522

from config import *

# ============================================================

# KHOA CHO SHIFT REGISTER

# ============================================================

_595_LOCK = threading.Lock()
_165_LOCK = threading.Lock()
_I2C_LOCK = threading.RLock()

# ============================================================

# LCD I2C QUA TCA9548A

# ============================================================

class I2CLCD:
    """Dieu khien LCD HD44780 qua PCF8574 va TCA9548A bang giao tiep 4-bit.

    So do PCF8574 mac dinh:
        P0 = RS, P1 = RW, P2 = EN, P3 = Backlight,
        P4..P7 = D4..D7.

    Moi giao dich LCD duoc bao boi khoa I2C chung de tranh hai LCD
    chon kenh TCA9548A chen len nhau.
    """

    LCD_CHR = 1
    LCD_CMD = 0

    LINE_ADDRS = (0x00, 0x40, 0x14, 0x54)

    RS = 0x01
    ENABLE = 0x04
    BACKLIGHT = 0x08

    def __init__(self, bus, channel, columns, rows):
        if not 0 <= int(channel) <= 7:
            raise ValueError("TCA9548A channel phai nam trong khoang 0..7")
        if int(columns) <= 0:
            raise ValueError("So cot LCD phai lon hon 0")
        if not 1 <= int(rows) <= len(self.LINE_ADDRS):
            raise ValueError("LCD ho tro tu 1 den 4 dong")

        self.bus = bus
        self.addr = LCD_ADDR
        self.channel = int(channel)
        self.columns = int(columns)
        self.rows = int(rows)
        self.last_lines = None
        self._lock = threading.RLock()

        # Khoi tao bo dieu khien HD44780 trong khoa I2C chung.
        with _I2C_LOCK:
            with self._lock:
                self._initialize_controller()

    def select_channel(self):
        """Chon kenh TCA9548A cua LCD nay. Phai goi khi dang giu _I2C_LOCK."""
        self.bus.write_byte(TCA_ADDR, 1 << self.channel)
        time.sleep(0.001)

    def _write_expander(self, value):
        """Ghi mot byte ra PCF8574, giu den nen bat."""
        self.bus.write_byte(self.addr, (int(value) | self.BACKLIGHT) & 0xFF)

    def _write_single_nibble(self, nibble_high):
        """Gui mot nibble len D7..D4 kem mot xung EN (dung cho lenh RS=0)."""
        # RS=0, RW=0, EN=0, Backlight=1.
        data = (nibble_high & 0xF0) | self.BACKLIGHT
        self.bus.write_byte(self.addr, data)
        time.sleep(0.0002)
        self.bus.write_byte(self.addr, data | self.ENABLE)
        time.sleep(0.0002)
        self.bus.write_byte(self.addr, data)
        time.sleep(0.0002)

    def _write_nibble(self, nibble_high, mode):
        """Gui mot nibble voi RS=0 (lenh) hoac RS=1 (du lieu)."""
        data = (nibble_high & 0xF0) | (mode & self.RS) | self.BACKLIGHT
        self._write_expander(data)
        time.sleep(0.0002)
        self._write_expander(data | self.ENABLE)
        time.sleep(0.0002)
        self._write_expander(data)
        time.sleep(0.0002)

    def _write_byte(self, value, mode):
        value = int(value) & 0xFF
        self._write_nibble(value & 0xF0, mode)
        self._write_nibble((value << 4) & 0xF0, mode)

    def write_cmd(self, value):
        """Gui mot lenh 8-bit theo hai nibble; chon lai kenh truoc khi ghi."""
        with _I2C_LOCK:
            with self._lock:
                self.select_channel()
                self._write_byte(value, self.LCD_CMD)
                if (int(value) & 0xFF) in (0x01, 0x02):
                    time.sleep(0.002)

    def command(self, value):
        """Ten ham tuong thich voi cac ban hardware.py truoc."""
        self.write_cmd(value)

    def write_data(self, value):
        """Gui mot byte ky tu len LCD."""
        with _I2C_LOCK:
            with self._lock:
                self.select_channel()
                self._write_byte(value, self.LCD_CHR)

    def write_char(self, value):
        """Ten ham tuong thich voi cac ban hardware.py truoc."""
        self.write_data(value)

    def resync_4bit(self):
        """Dong bo lai HD44780 ve 4-bit ma khong xoa DDRAM/man hinh.

        Chuoi nibble don 0x30, 0x30, 0x30, 0x20 dua bo dieu khien ve
        che do 4-bit. Sau do cau hinh lai function/display/entry mode.
        """
        with _I2C_LOCK:
            with self._lock:
                self.select_channel()

                self._write_single_nibble(0x30)
                time.sleep(0.005)
                self._write_single_nibble(0x30)
                time.sleep(0.005)
                self._write_single_nibble(0x30)
                time.sleep(0.001)

                self._write_single_nibble(0x20)
                time.sleep(0.001)

                # Khong gui 0x01 o day de tranh xoa/trang man hinh khi phuc hoi.
                self.write_cmd(0x28)
                self.write_cmd(0x0C)
                self.write_cmd(0x06)

                # Buoc ghi tiep theo nen ve lai noi dung day du.
                self.last_lines = None

    def recover(self):
        """API phuc hoi duoc main.py goi khi giao dich I2C bi loi."""
        self.resync_4bit()

    def _initialize_controller(self):
        """Khoi tao LCD sau khi cap nguon lan dau."""
        self.select_channel()
        time.sleep(0.05)

        # Dung cung chuoi dong bo nibble nhu ham resync_4bit.
        self._write_single_nibble(0x30)
        time.sleep(0.005)
        self._write_single_nibble(0x30)
        time.sleep(0.005)
        self._write_single_nibble(0x30)
        time.sleep(0.001)
        self._write_single_nibble(0x20)
        time.sleep(0.001)

        self._write_byte(0x28, self.LCD_CMD)  # 4-bit, 2-line mode
        self._write_byte(0x0C, self.LCD_CMD)  # Display ON, cursor OFF
        self._write_byte(0x06, self.LCD_CMD)  # Entry mode
        self._write_byte(0x01, self.LCD_CMD)  # Clear chi khi khoi tao
        time.sleep(0.003)
        self.last_lines = None

    @staticmethod
    def _normalize_line(value, columns):
        # HD44780 hien tai dung bo ky tu ASCII.
        text = str(value).encode("ascii", "replace").decode("ascii")
        return text[:columns].ljust(columns)

    def _write_changed_runs(self, row, old_line, new_line):
        """Chi ghi cac cum ky tu thay doi cua mot dong."""
        changed = [
            index
            for index, (old, new) in enumerate(zip(old_line, new_line))
            if old != new
        ]
        if not changed:
            return

        start = previous = changed[0]
        runs = []
        for index in changed[1:]:
            if index != previous + 1:
                runs.append((start, previous + 1))
                start = index
            previous = index
        runs.append((start, previous + 1))

        for run_start, run_end in runs:
            self._write_byte(
                0x80 | (self.LINE_ADDRS[row] + run_start),
                self.LCD_CMD,
            )
            for char in new_line[run_start:run_end]:
                self._write_byte(ord(char), self.LCD_CHR)

    def display(self, lines, force=False):
        """Hien thi cac dong; chi ghi phan thay doi de giam giao dich I2C."""
        if lines is None:
            lines = []

        text_lines = tuple(
            self._normalize_line(
                lines[row] if row < len(lines) else "",
                self.columns,
            )
            for row in range(self.rows)
        )

        with _I2C_LOCK:
            with self._lock:
                old_lines = self.last_lines
                if not force and text_lines == old_lines:
                    return

                self.select_channel()

                if force or old_lines is None:
                    for row, line in enumerate(text_lines):
                        self._write_byte(
                            0x80 | self.LINE_ADDRS[row],
                            self.LCD_CMD,
                        )
                        for char in line:
                            self._write_byte(ord(char), self.LCD_CHR)
                else:
                    for row, (old_line, new_line) in enumerate(
                        zip(old_lines, text_lines)
                    ):
                        self._write_changed_runs(row, old_line, new_line)

                # Chi cap nhat cache sau khi tat ca lenh I2C thanh cong.
                self.last_lines = text_lines

# ============================================================

def initialize_gpio():

    """Khoi tao GPIO BCM, giu nguyen mapping phan cung da chot."""

    GPIO.setmode(GPIO.BCM)

    GPIO.setwarnings(False)

    # 74HC165: PL idle HIGH, CLOCK idle LOW.

    GPIO.setup(PL165, GPIO.OUT, initial=GPIO.HIGH)

    GPIO.setup(CLOCK165, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(DATA165, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    # 74HC595: OE HIGH de tam tat dau ra trong luc khoi tao.

    GPIO.setup(DATA595, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(CLOCK595, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(LATCH595, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(OE595, GPIO.OUT, initial=GPIO.HIGH)

    # Servo PWM do main.py tao va quan ly.

    GPIO.setup(SERVO_ENTRY_PIN, GPIO.OUT)

    GPIO.setup(SERVO_EXIT_PIN, GPIO.OUT)

# ============================================================

# GHI DU LIEU 74HC595

# ============================================================

def write_595(value):

    """Ghi 8 bit vao 74HC595, bit 7 truoc bit 0 sau."""

    value = int(value) & 0xFF

    with _595_LOCK:

        GPIO.output(LATCH595, GPIO.LOW)

        for bit in range(7, -1, -1):

            GPIO.output(DATA595, (value >> bit) & 1)

            GPIO.output(CLOCK595, GPIO.HIGH)

            time.sleep(0.000005)

            GPIO.output(CLOCK595, GPIO.LOW)

            time.sleep(0.000005)

        GPIO.output(LATCH595, GPIO.HIGH)

        time.sleep(0.000005)

        GPIO.output(LATCH595, GPIO.LOW)

def update_slot_leds(occupied):

    """Bat den do neu co xe, den xanh neu o trong."""

    if len(occupied) < len(SLOT_NAMES):

        raise ValueError(

            "Can {} trang thai o do, nhan duoc {}".format(

                len(SLOT_NAMES), len(occupied)

            )

        )

    on_mask = 0

    for index, slot in enumerate(SLOT_NAMES):

        bits = SLOT_LED_BITS[slot]

        selected_bit = bits["red"] if bool(occupied[index]) else bits["green"]

        on_mask |= 1 << selected_bit

    output_value = ((~on_mask) & 0xFF) if LED_ACTIVE_LOW else on_mask

    write_595(output_value)

# ============================================================

# DOC DU LIEU 74HC165

# ============================================================

def read_74hc165():

    """Doc 8 bit theo dung thu tu bit da su dung va kiem thu truoc day."""

    with _165_LOCK:

        GPIO.output(CLOCK165, GPIO.LOW)

        GPIO.output(PL165, GPIO.LOW)

        time.sleep(0.00001)

        GPIO.output(PL165, GPIO.HIGH)

        time.sleep(0.00001)

        value = 0

        for bit in range(8):

            data = int(GPIO.input(DATA165))

            value |= data << bit

            GPIO.output(CLOCK165, GPIO.HIGH)

            time.sleep(0.000005)

            GPIO.output(CLOCK165, GPIO.LOW)

            time.sleep(0.000005)

        return value

def decode_ir(raw_value):

    """Tra ve [P1, P2, P3, P4, IR vao, IR ra]; True nghia la co xe."""

    raw_value = int(raw_value) & 0xFF

    states = [

        ((raw_value >> bit) & 1) == IR_ACTIVE_LEVEL

        for bit in SLOT_IR_BITS

    ]

    states.append(((raw_value >> ENTRY_IR_BIT) & 1) == IR_ACTIVE_LEVEL)

    states.append(((raw_value >> EXIT_IR_BIT) & 1) == IR_ACTIVE_LEVEL)

    return states

# ============================================================

# LOC NHIEU IR

# ============================================================

class Debouncer:

    """Bo loc theo thoi gian: dau vao phai on dinh du delay moi cap nhat."""

    def __init__(self, count, delay):

        self.count = int(count)

        self.delay = float(delay)

        if self.count <= 0:

            raise ValueError("Debouncer count phai lon hon 0")

        if self.delay < 0:

            raise ValueError("Debouncer delay khong duoc am")

        self.reset()

    def reset(self, now=None):

        current = time.monotonic() if now is None else float(now)

        self.raw = [None] * self.count

        self.stable = [False] * self.count

        self.changed = [current] * self.count

    def update(self, values, now=None):

        if len(values) != self.count:

            raise ValueError(

                "Debouncer can {} kenh, nhan duoc {}".format(

                    self.count, len(values)

                )

            )

        current = time.monotonic() if now is None else float(now)

        for index, value in enumerate(values):

            value = bool(value)

            if self.raw[index] is None or value != self.raw[index]:

                self.raw[index] = value

                self.changed[index] = current

            elif value != self.stable[index] and current - self.changed[index] >= self.delay:

                self.stable[index] = value

        ready = all(value is not None for value in self.raw) and all(

            current - changed >= self.delay for changed in self.changed

        )

        return self.stable[:], ready

# ============================================================

# RC522

# ============================================================

def create_reader(device, rst_pin):

    """Tao MFRC522 theo cac dang constructor pho bien cua thu vien."""

    device = int(device)

    rst_pin = int(rst_pin)

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

    # Chi thu constructor mac dinh khi dung SPI0 CE0, khong ap dung CE1.

    if device == 0:

        constructors.append(lambda: MFRC522())

    errors = []

    for make_reader in constructors:

        try:

            return make_reader()

        except (TypeError, ValueError) as exc:

            errors.append(str(exc))

    raise RuntimeError(

        "MFRC522 khong tao duoc SPI device {} GPIO{}: {}".format(

            device, rst_pin, " | ".join(errors) or "khong ro loi"

        )

    )

def _format_uid_bytes(values):

    """Chuan hoa UID dang byte/list/tuple thanh chuoi HEX."""

    try:

        byte_values = [int(value) & 0xFF for value in list(values)]

    except (TypeError, ValueError):

        return None

    if not byte_values:

        return None

    # Uu tien 4 byte UID nhu logic he thong dang su dung.

    return "".join("{:02X}".format(value) for value in byte_values[:4])

def read_uid(reader):

    """Doc UID khong block neu co; fallback cac API MFRC522 pho bien."""

    if reader is None:

        return None

    if hasattr(reader, "read_id_no_block"):

        try:

            result = reader.read_id_no_block()

            if result:

                if isinstance(result, (list, tuple)):

                    return _format_uid_bytes(result[:4])

                # Giu cach chuan hoa dang chuoi de tuong thich du lieu hien tai.

                return str(result).replace(" ", "").upper()

        except Exception:

            pass

    method_pairs = (

        ("MFRC522_Request", "MFRC522_Anticoll"),

        ("Request", "Anticoll"),

        ("request", "anticoll"),

    )

    for request_name, anticoll_name in method_pairs:

        if not (hasattr(reader, request_name) and hasattr(reader, anticoll_name)):

            continue

        try:

            request = getattr(reader, request_name)

            anticoll = getattr(reader, anticoll_name)

            request_code = getattr(

                reader,

                "PICC_REQIDL",

                getattr(reader, "REQIDL", 0x26),

            )

            result = request(request_code)

            if not isinstance(result, (tuple, list)) or not result:

                continue

            ok = getattr(reader, "MI_OK", 0)

            if result[0] != ok:

                continue

            result2 = anticoll()

            if not isinstance(result2, (tuple, list)) or len(result2) < 2:

                continue

            status, uid = result2[0], result2[1]

            if status == ok and uid:

                return _format_uid_bytes(uid)

        except Exception:

            continue

    return None

# ============================================================

# DIEU KHIEN SERVO

# ============================================================

def move_servo(pwm, opened, gate="entry"):

    """Di chuyen servo theo duty da test; giu thoi gian on dinh 0.35 giay."""

    if gate == "exit":

        duty = SERVO_EXIT_OPEN_DUTY if opened else SERVO_EXIT_CLOSED_DUTY

    else:

        duty = SERVO_ENTRY_OPEN_DUTY if opened else SERVO_ENTRY_CLOSED_DUTY

    pwm.ChangeDutyCycle(duty)

    # Khong bo sleep nay khi chua doi state machine trong main.py:

    # main chi bat dau dem IR qua cong sau khi servo da di chuyen xong.

    time.sleep(0.35)

    pwm.ChangeDutyCycle(0)
