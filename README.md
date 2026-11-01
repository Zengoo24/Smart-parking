# Smart Parking Edge IoT

## Cấu trúc

```text
bai_do_xe_tong_hop.py
parking_system/
  config.py
  hardware.py
  rfid.py
  lcd_display.py
  data_store.py
  reservation_manager.py
  parking_lot.py
  telemetry.py
  controller.py
```

## Phần cứng

- Raspberry Pi 4B; 5 vị trí đỗ, cảm biến IR active LOW.
- 74HC595: GPIO 17/22/23/5; 74HC165: GPIO 24/20/21.
- RC522: cổng vào CE0/RST GPIO25; cổng ra CE1/RST GPIO16.
- Servo: GPIO13 (vào), GPIO18 (ra), 50 Hz.
- TCA9548A 0x70; LCD 0x27, LCD1602 kênh 0 và LCD2004 kênh 1.

## Cài đặt và chạy

Copy `bai_do_xe_tong_hop.py` và thư mục `parking_system/` vào `/home/pi/`, sau đó kiểm tra và khởi động:

```bash
python3 -m compileall -q /home/pi/parking_system
python3 -m py_compile /home/pi/bai_do_xe_tong_hop.py
sudo systemctl restart parking-controller.service
sudo systemctl status parking-controller.service --no-pager
```

Service hiện tại dùng `/usr/bin/python3 -u /home/pi/bai_do_xe_tong_hop.py`, không cần sửa unit file nếu giữ nguyên đường dẫn này.
