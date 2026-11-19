# config.py
# -*- coding: utf-8 -*-

import os

# ============================================================
# DUONG DAN DU LIEU
# ============================================================
BASE_DIR = os.path.expanduser("~")
DATA_FILE = os.path.join(BASE_DIR, "parking_data.json")
HISTORY_FILE = os.path.join(BASE_DIR, "parking_history.csv")
PLATE_HISTORY_FILE = os.path.join(BASE_DIR, "parking_plate_history.csv")

# ============================================================
# 74HC595
# ============================================================
DATA595 = 17
CLOCK595 = 22
LATCH595 = 23
OE595 = 5
LED_ACTIVE_LOW = False
SLOT_LED_BITS = {
    "P1": {"red": 6, "green": 7},
    "P2": {"red": 5, "green": 4},
    "P3": {"red": 2, "green": 3},
    "P4": {"red": 1, "green": 0},
}

# ============================================================
# 74HC165
# ============================================================
PL165 = 24
CLOCK165 = 20
DATA165 = 21
IR_ACTIVE_LEVEL = 0
IR_DEBOUNCE = 0.20
SLOT_NAMES = ["P1", "P2", "P3", "P4"]
SLOT_IR_BITS = [2, 1, 3, 0]
ENTRY_IR_BIT = 5
EXIT_IR_BIT = 6

# ============================================================
# RC522
# ============================================================
ENTRY_RFID_DEVICE = 0
ENTRY_RFID_RST = 25
EXIT_RFID_DEVICE = 1
EXIT_RFID_RST = 16

# ============================================================
# SERVO SG90 - GIU NGUYEN THONG SO DA TEST
# ============================================================
SERVO_ENTRY_PIN = 13
SERVO_EXIT_PIN = 18
SERVO_FREQUENCY = 50
SERVO_ENTRY_CLOSED_DUTY = 6.0
SERVO_ENTRY_OPEN_DUTY = 2.5
SERVO_EXIT_CLOSED_DUTY = 5.1
SERVO_EXIT_OPEN_DUTY = 3.0

# ============================================================
# PARKING
# ============================================================
FEE_PER_HOUR = 10000
CARD_WAIT_TIMEOUT = 15.0
GATE_CLEAR_STABLE = 0.35
RFID_COOLDOWN = 2.0
GATE_PRE_OPEN_DELAY = 0.5
DISPLAY_EVENT_SECONDS = 5.0
LOOP_SLEEP = 0.03

# ============================================================
# LCD / TCA9548A
# ============================================================
TCA_ADDR = 0x70
LCD_ADDR = 0x27
LCD1602_CHANNEL = 0
LCD2004_CHANNEL = 1

# ============================================================
# CAMERA USB OV9726
# ============================================================
ENTRY_CAMERA_DEVICE = "/dev/video2"
EXIT_CAMERA_DEVICE = "/dev/video0"
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 800
# Camera modes observed: 30/25/15/10 FPS at MJPG 1280x800.
CAMERA_FPS = 25
CAMERA_FOURCC = "MJPG"
ROI_REFERENCE_WIDTH = 640
ROI_REFERENCE_HEIGHT = 480

# ROI cong VAO duoc can chinh theo anh thu nghiem hai dong.
# ROI cong RA tam giu gia tri da cau hinh; can kiem tra rieng bang test_two_line_alpr.py.
# Mo rong va dich ROI cong vao sang trai/len tren de bao tron ca hai dong bien so.
# ROI nay duoc tinh theo kich thuoc tham chieu 640x480.
ENTRY_PLATE_ROI = (180, 235, 440, 425)
# ROI cong ra giu nguyen vi tri da cau hinh.
EXIT_PLATE_ROI = (200, 250, 420, 410)
PLATE_ROW_SPLIT = 0.48
PLATE_COMPONENT_SCALE = 2
PLATE_ROW_OCR_SCALE = 3

# ============================================================
# ============================================================
# CAMERA PREVIEW
# ============================================================
SHOW_CAMERA_WINDOWS = False
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 400
ENTRY_WINDOW_X = 10
ENTRY_WINDOW_Y = 40
EXIT_WINDOW_X = 660
EXIT_WINDOW_Y = 40

# ============================================================
# OCR - TESSERACT DOC RIENG 2 DONG
# ============================================================
OCR_LANGUAGE = "eng"
OCR_PSM = 7
OCR_SCALE = 3
OCR_WHITELIST = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
OCR_TIMEOUT = 1.8

# ============================================================
# TOI UU THOI GIAN NHAN DIEN & GIU PHIEN IR
# ============================================================
PLATE_CAPTURE_WINDOW = 0.08
PLATE_RETRY_INTERVAL = 0.08
PLATE_IR_HOLD_SECONDS = 2.5
PLATE_MAX_ATTEMPTS = 8
PLATE_RETRY_PAUSE = 0.25
PLATE_VOTE_WINDOW = 3
PLATE_MIN_MATCHES = 2
PLATE_MIN_AGREEMENT = 0.67
PLATE_MIN_SHARPNESS = 15.0
OCR_QUEUE_SIZE = 4
