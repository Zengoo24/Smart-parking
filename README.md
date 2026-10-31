# Smart Parking Edge IoT - ma nguon da tach module

Du an duoc tach tu file dieu khien tong hop thanh cac module theo chuc nang. File `bai_do_xe_tong_hop.py` o thu muc goc chi la launcher de giu nguyen duong dan `ExecStart` cua systemd.

## Cau truc

```text
smart_parking_edge_iot/
├── bai_do_xe_tong_hop.py       # Launcher tuong thich systemd
├── parking_system/
│   ├── __init__.py
│   ├── config.py               # GPIO, mapping IC, servo, LCD, duong dan du lieu
│   ├── lcd_display.py          # LCD1602/LCD2004 qua TCA9548A
│   ├── hardware.py             # 74HC595, 74HC165, IR, LED, servo, debounce
│   ├── rfid.py                 # RC522, doc va chuan hoa UID
│   ├── data_store.py           # JSON/CSV, phien xe, lich su, UID request
│   ├── reservation_manager.py  # Dat cho, check-in, no-show, trang thai dat cho
│   ├── parking_lot.py          # Quan ly 5 vi tri va gan xe vao vi tri
│   ├── telemetry.py            # Ghi parking_live.json va trang thai he thong
│   └── controller.py           # Dieu phoi luong xe vao/ra va vong lap chinh
└── README.md
```

## Thong so phan cung giu theo file goc

- GPIO BCM; DATA/CLOCK/LATCH/OE 74HC595: 17/22/23/5.
- PL/CLOCK/DATA 74HC165: 24/20/21.
- RC522: SPI0 CE0/RST GPIO25 (vao), CE1/RST GPIO16 (ra).
- Servo: GPIO13 (vao), GPIO18 (ra), 50 Hz.
- Duty: cong vao mo/dong 2.5/5.0; cong ra mo/dong 3.0/5.1.
- IR active LOW; mapping slot bits va mapping P1/P5, P2/P4 giu theo file goc.
- I2C TCA9548A 0x70, LCD 0x27; LCD1602 channel 0, LCD2004 channel 1.

## Cai dat tren Raspberry Pi

Can cac thu vien da co trong Python he thong: `RPi.GPIO`, `smbus` hoac `smbus2`, va `mfrc522`.

Sao chep ca file `bai_do_xe_tong_hop.py` va thu muc `parking_system/` vao `/home/pi/`. Ket qua can co:

```text
/home/pi/bai_do_xe_tong_hop.py
/home/pi/parking_system/__init__.py
/home/pi/parking_system/config.py
... cac module con lai ...
```

Giu nguyen service hien tai: `ExecStart=/usr/bin/python3 -u /home/pi/bai_do_xe_tong_hop.py`. Khong can sua unit file systemd va khong can `daemon-reload`.

## Kiem tra va chay

```bash
python3 -m compileall -q /home/pi/parking_system
python3 -m py_compile /home/pi/bai_do_xe_tong_hop.py
sudo systemctl restart parking-controller.service
sudo systemctl status parking-controller.service --no-pager
```

**Quan trong:** Phai upload ca thu muc `parking_system/` cung launcher. Neu chi upload file launcher, se gap `ModuleNotFoundError`. Sao luu file cu va du lieu JSON/CSV truoc khi thay the.


## GitHub va bao mat

- Khong commit file `.env`, mat khau Wi-Fi/MQTT, token, UID the RFID hoac cac file du lieu van hanh.
- `.gitignore` da loai tru cac file JSON/CSV du lieu runtime, cache Python va file cau hinh rieng.
- Repository nay chua bo dieu khien Raspberry Pi da tach module. Cac thanh phan Flask/Digital Twin, MQTT Bridge va giao dien web can duoc bo sung tu may Raspberry Pi neu muon cong bo toan bo he thong.
