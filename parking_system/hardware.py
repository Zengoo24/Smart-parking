"""I/O phan cung: 74HC595, 74HC165, LED, IR va servo."""

import time

import RPi.GPIO as GPIO

from .config import (
    DATA595, CLOCK595, LATCH595, OE595, PL165, CLOCK165, DATA165,
    IR_ACTIVE_LEVEL, SLOT_IR_BITS, ENTRY_IR_BIT, EXIT_IR_BIT,
    LED3_RED, LED3_GREEN, LED5_RED, LED5_GREEN,
    ENTRY_LIGHT_BITS, EXIT_LIGHT_BITS,
    SERVO_ENTRY_PIN, SERVO_EXIT_PIN,
    SERVO_OPEN_DUTY, SERVO_CLOSED_DUTY,
    SERVO_EXIT_OPEN_DUTY, SERVO_EXIT_CLOSED_DUTY,
)

output_595 = 0

def write_595(value):
    global output_595

    output_595 = value & 0xFFFF
    GPIO.output(LATCH595, GPIO.LOW)

    for bit in range(15, -1, -1):
        GPIO.output(CLOCK595, GPIO.LOW)
        GPIO.output(DATA595, (output_595 >> bit) & 1)
        GPIO.output(CLOCK595, GPIO.HIGH)

    GPIO.output(CLOCK595, GPIO.LOW)
    GPIO.output(LATCH595, GPIO.HIGH)
    GPIO.output(LATCH595, GPIO.LOW)

def set_traffic_light(gate, color):
    global output_595

    maps = {"entry": ENTRY_LIGHT_BITS, "exit": EXIT_LIGHT_BITS}

    if gate not in maps or color not in maps[gate]:
        raise ValueError("Sai ten cong hoac mau den")

    for name, bit in maps[gate].items():
        if name == color:
            output_595 |= 1 << bit
        else:
            output_595 &= ~(1 << bit)

    write_595(output_595)

def update_slot_leds(occupied):
    """Dieu khien LED theo mapping vat ly da kiem tra.

    P1 -> cap GPIO LED5_RED/GREEN (thuc te LED1)
    P2 -> IC2 Q0/Q7 (thuc te LED2)
    P3 -> GPIO LED3, giu nguyen
    P4 -> IC1 Q7/Q0 (thuc te LED4)
    P5 -> IC2 Q1/Q2 (thuc te LED5)
    """
    global output_595

    # Tat cac kenh LED 1, 2, 4, 5 truoc khi cap nhat
    for bit in (9, 10, 7, 0, 8, 15):
        output_595 &= ~(1 << bit)

    # LED5 vat ly: Q1 = xanh/co xe; Q2 = do/o trong
    output_595 |= 1 << (9 if occupied[4] else 10)

    # LED4 vat ly: Q7 = xanh/co xe; Q0 = do/o trong
    output_595 |= 1 << (7 if occupied[3] else 0)

    # LED2 vat ly: Q0 = xanh/co xe; Q7 = do/o trong
    output_595 |= 1 << (8 if occupied[1] else 15)

    write_595(output_595)

    # LED3 giu nguyen mapping da hoat dong dung
    GPIO.output(
        LED3_RED, GPIO.HIGH if occupied[2] else GPIO.LOW
    )
    GPIO.output(
        LED3_GREEN, GPIO.LOW if occupied[2] else GPIO.HIGH
    )

    # LED1 / P1: dao logic mau theo test thuc te
    # P1 co xe -> xanh; P1 trong -> do.
    GPIO.output(
        LED5_RED, GPIO.LOW if occupied[0] else GPIO.HIGH
    )
    GPIO.output(
        LED5_GREEN, GPIO.LOW if not occupied[0] else GPIO.HIGH
    )

def read_74hc165():
    GPIO.output(CLOCK165, GPIO.LOW)
    GPIO.output(PL165, GPIO.LOW)
    time.sleep(0.00001)
    GPIO.output(PL165, GPIO.HIGH)
    time.sleep(0.00001)

    value = 0

    for _ in range(8):
        value = (value << 1) | int(GPIO.input(DATA165))
        GPIO.output(CLOCK165, GPIO.HIGH)
        time.sleep(0.000005)
        GPIO.output(CLOCK165, GPIO.LOW)
        time.sleep(0.000005)

    return value

def decode_ir(raw_value):
    states = [
        ((raw_value >> bit) & 1) == IR_ACTIVE_LEVEL
        for bit in SLOT_IR_BITS
    ]

    states.append(
        ((raw_value >> ENTRY_IR_BIT) & 1) == IR_ACTIVE_LEVEL
    )
    states.append(
        ((raw_value >> EXIT_IR_BIT) & 1) == IR_ACTIVE_LEVEL
    )

    return states

class Debouncer:
    def __init__(self, count, delay):
        self.raw = [None] * count
        self.stable = [False] * count
        self.changed = [time.monotonic()] * count
        self.delay = delay

    def update(self, values, now):
        for i, value in enumerate(values):
            if self.raw[i] is None or value != self.raw[i]:
                self.raw[i] = value
                self.changed[i] = now
            elif (
                value != self.stable[i]
                and now - self.changed[i] >= self.delay
            ):
                self.stable[i] = value

        ready = (
            all(value is not None for value in self.raw)
            and all(
                now - changed >= self.delay
                for changed in self.changed
            )
        )

        return self.stable[:], ready

def initialize_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    for pin in (
        DATA595,
        CLOCK595,
        LATCH595,
        PL165,
        CLOCK165,
    ):
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(OE595, GPIO.OUT, initial=GPIO.HIGH)

    GPIO.setup(
        DATA165,
        GPIO.IN,
        pull_up_down=GPIO.PUD_DOWN,
    )

    for pin in (
        LED3_RED,
        LED3_GREEN,
        LED5_RED,
        LED5_GREEN,
    ):
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    GPIO.setup(SERVO_ENTRY_PIN, GPIO.OUT)
    GPIO.setup(SERVO_EXIT_PIN, GPIO.OUT)

def move_servo(pwm, opened, exit_gate=False):
    if opened:
        duty = SERVO_EXIT_OPEN_DUTY if exit_gate else SERVO_OPEN_DUTY
    else:
        duty = SERVO_EXIT_CLOSED_DUTY if exit_gate else SERVO_CLOSED_DUTY

    pwm.ChangeDutyCycle(duty)
    time.sleep(0.35)
    pwm.ChangeDutyCycle(0)
