# Smart Parking Edge IoT

Hệ thống quản lý bãi đỗ xe thông minh sử dụng Raspberry Pi 4B, Edge Computing và IoT. Mô hình gồm 5 vị trí đỗ, 2 cổng vào/ra, RFID, cảm biến IR, LCD, giao diện web Digital Twin và MQTT.

## Cấu trúc dự án

```text
smart_parking_edge_iot/
├── bai_do_xe_tong_hop.py       # Launcher cho systemd
├── parking_system/             # Các module điều khiển phần cứng và nghiệp vụ
├── digital_twin.py             # Flask REST API và máy chủ giao diện web
├── parking_mqtt_bridge.py      # Gửi trạng thái/sự kiện lên MQTT
├── templates/
│   └── digital_twin.html       # Giao diện web giám sát và quản lý
└── static/                     # CSS, JavaScript, hình ảnh (nếu có)
```

## Phần cứng chính

- Raspberry Pi 4B; 5 cảm biến IR; 2 đầu đọc RFID RC522.
- 2 IC 74HC595 và 1 IC 74HC165.
- 2 servo barie, đèn giao thông và 2 LCD (1602, 2004) qua TCA9548A.

## Cài đặt

Sao chép `bai_do_xe_tong_hop.py`, thư mục `parking_system/`, `digital_twin.py`, `parking_mqtt_bridge.py`, `templates/` và `static/` (nếu có) vào `/home/pi/` theo đúng cấu trúc.

Kiểm tra mã nguồn và khởi động bộ điều khiển:

```bash
python3 -m compileall -q /home/pi/parking_system
python3 -m py_compile /home/pi/bai_do_xe_tong_hop.py
sudo systemctl restart parking-controller.service
```

Các dịch vụ giao diện web và MQTT được quản lý riêng bằng systemd theo cấu hình trên Raspberry Pi.

## Chạy hệ thống trên PuTTY

Đăng nhập Raspberry Pi bằng PuTTY, sau đó chạy các lệnh dưới đây.

### 1. Khởi động các dịch vụ

Khởi động Mosquitto Broker trước, sau đó khởi động bộ điều khiển bãi đỗ, giao diện Digital Twin và MQTT Bridge:

```bash
sudo systemctl start mosquitto
sudo systemctl start parking-controller
sudo systemctl start digital-twin
sudo systemctl start parking-mqtt-bridge
```

### 2. Kiểm tra trạng thái dịch vụ

```bash
sudo systemctl status parking-controller digital-twin parking-mqtt-bridge mosquitto --no-pager
```

Các dịch vụ hoạt động bình thường sẽ hiển thị trạng thái `active (running)`.

### 3. Mở giao diện Digital Twin

Lấy địa chỉ IP của Raspberry Pi:

```bash
hostname -I
```

Mở trình duyệt trên máy tính cùng mạng và truy cập địa chỉ sau, thay `IP_RASPBERRY_PI` bằng địa chỉ IP vừa nhận được:

```text
http://IP_RASPBERRY_PI:5000
```

Ví dụ:

```text
http://192.168.0.110:5000
```

### 4. Theo dõi dữ liệu MQTT

Lệnh dưới đây yêu cầu nhập mật khẩu MQTT mà không hiển thị ký tự trên màn hình, sau đó theo dõi tất cả topic thuộc `parking/pi4/`:

```bash
read -rsp "Nhap mat khau MQTT: " MQTT_PASS
echo
mosquitto_sub -h 127.0.0.1 -p 1883 -u parkingpi -P "$MQTT_PASS" -t 'parking/pi4/#' -v
```

Nhấn `Ctrl + C` để dừng chương trình theo dõi MQTT. Thao tác này chỉ dừng lệnh `mosquitto_sub`, không tắt Mosquitto Broker, MQTT Bridge hoặc bộ điều khiển bãi đỗ.

### 5. Xem nhật ký hoạt động khi cần kiểm tra lỗi

Chạy lệnh tương ứng với dịch vụ cần theo dõi:

```bash
# Bộ điều khiển bãi đỗ
sudo journalctl -u parking-controller -f

# Giao diện Digital Twin / Flask API
sudo journalctl -u digital-twin -f

# MQTT Bridge
sudo journalctl -u parking-mqtt-bridge -f

# Mosquitto Broker
sudo journalctl -u mosquitto -f
```

Nhấn `Ctrl + C` để thoát xem nhật ký.

**Lưu ý:** Các lệnh trên sử dụng các service systemd đã được cấu hình trên Raspberry Pi. Nếu một service chưa được cài đặt hoặc cấu hình, lệnh `systemctl start` sẽ không thể khởi động thành phần tương ứng.

## Bảo mật

Không đưa mật khẩu Wi-Fi/MQTT, token, UID thẻ RFID hoặc dữ liệu vận hành (JSON/CSV) lên repository. Không commit file `.env` hay các cấu hình chứa thông tin xác thực.
