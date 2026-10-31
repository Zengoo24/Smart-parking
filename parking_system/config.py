"""Cau hinh phan cung, GPIO, I2C va duong dan du lieu."""

import os
import RPi.GPIO as GPIO

DATA595, CLOCK595, LATCH595, OE595 = 17, 22, 23, 5
PL165, CLOCK165, DATA165 = 24, 20, 21

# LOW = co xe, theo cau hinh IR ban dang dung
IR_ACTIVE_LEVEL = GPIO.LOW
IR_DEBOUNCE = 0.20

SLOT_NAMES = ["P1", "P2", "P3", "P4", "P5"]
SLOT_IR_BITS = [2, 1, 3, 0, 4]
ENTRY_IR_BIT, EXIT_IR_BIT = 5, 6

LED3_RED, LED3_GREEN = 27, 12
LED5_RED, LED5_GREEN = 19, 6

ENTRY_LIGHT_BITS = {"red": 4, "yellow": 5, "green": 6}
EXIT_LIGHT_BITS = {"red": 1, "yellow": 2, "green": 3}

ENTRY_RFID_DEVICE, ENTRY_RFID_RST = 0, 25
EXIT_RFID_DEVICE, EXIT_RFID_RST = 1, 16

SERVO_ENTRY_PIN, SERVO_EXIT_PIN = 13, 18
SERVO_FREQUENCY = 50
SERVO_CLOSED_DUTY, SERVO_OPEN_DUTY = 5.0, 2.5  # Cong vao: dong/mo
SERVO_EXIT_OPEN_DUTY = 3.0   # Cong ra: mo
SERVO_EXIT_CLOSED_DUTY = 5.1  # Cong ra: dong va vi tri khi khoi dong
FEE_PER_HOUR = 5000
CARD_WAIT_TIMEOUT = 15.0
GATE_CLEAR_STABLE = 0.35
RFID_COOLDOWN = 2.0
YELLOW_BEFORE_OPEN = 0.5
DISPLAY_EVENT_SECONDS = 5.0
LOOP_SLEEP = 0.03

TCA_ADDR, LCD_ADDR = 0x70, 0x27
LCD1602_CHANNEL, LCD2004_CHANNEL = 0, 1

HOME = os.path.expanduser("~")
DATA_FILE = os.path.join(HOME, "parking_data.json")
HISTORY_FILE = os.path.join(HOME, "parking_history.csv")
LIVE_FILE = os.path.join(HOME, "parking_live.json")
RESERVATIONS_FILE = os.path.join(HOME, "parking_reservations.json")
UID_SCAN_REQUEST_FILE = os.path.join(HOME, "parking_uid_request.json")
UID_SCAN_RESULT_FILE = os.path.join(HOME, "parking_uid_result.json")

NO_SHOW_GRACE_SECONDS = 15 * 60

# Ten o tren Digital Twin -> ten o cam bien vat ly.
# Giu dung quy uoc dao P1/P5 va P2/P4 cua he thong hien tai.
DISPLAY_TO_PHYSICAL = {
    "P1": "P5",
    "P2": "P4",
    "P3": "P3",
    "P4": "P2",
    "P5": "P1",
}

PHYSICAL_TO_DISPLAY = {
    physical: display
    for display, physical in DISPLAY_TO_PHYSICAL.items()
}
