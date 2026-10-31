"""Dieu khien LCD1602/LCD2004 qua TCA9548A."""

import time

from .config import TCA_ADDR, LCD_ADDR

class I2CLCD:
    LCD_CHR, LCD_CMD = 1, 0
    LINE_ADDRS = [0x00, 0x40, 0x14, 0x54]
    BACKLIGHT, ENABLE = 0x08, 0x04

    def __init__(self, bus, channel, columns, rows):
        self.bus = bus
        self.channel = channel
        self.columns = columns
        self.rows = rows
        self.last_lines = None

        self.select_channel()
        time.sleep(0.05)

        self._nibble(0x30)
        time.sleep(0.005)
        self._nibble(0x30)
        time.sleep(0.001)
        self._nibble(0x30)
        self._nibble(0x20)

        self.command(0x28)
        self.command(0x0C)
        self.command(0x06)
        self.command(0x01)
        time.sleep(0.003)

    def select_channel(self):
        self.bus.write_byte(TCA_ADDR, 1 << self.channel)
        time.sleep(0.001)

    def _expander_write(self, value):
        self.bus.write_byte(LCD_ADDR, value | self.BACKLIGHT)

    def _nibble(self, value, mode=LCD_CMD):
        data = (value & 0xF0) | mode | self.BACKLIGHT
        self._expander_write(data | self.ENABLE)
        time.sleep(0.0005)
        self._expander_write(data & ~self.ENABLE)
        time.sleep(0.0001)

    def _byte(self, value, mode=LCD_CMD):
        self._nibble(value & 0xF0, mode)
        self._nibble((value << 4) & 0xF0, mode)

    def command(self, value):
        self._byte(value, self.LCD_CMD)

    def write_char(self, value):
        self._byte(value, self.LCD_CHR)

    def display(self, lines, force=False):
        result = []

        for row in range(self.rows):
            line = lines[row] if row < len(lines) else ""
            line = str(line).encode("ascii", "replace").decode("ascii")
            result.append(line[:self.columns].ljust(self.columns))

        key = tuple(result)

        if not force and key == self.last_lines:
            return

        self.select_channel()

        for row, line in enumerate(result):
            self.command(0x80 | self.LINE_ADDRS[row])
            for char in line:
                self.write_char(ord(char))

        self.last_lines = key

