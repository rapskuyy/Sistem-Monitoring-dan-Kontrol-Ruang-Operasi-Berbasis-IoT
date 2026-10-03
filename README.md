# 🏥 Operating Room Pressure & Temperature Monitoring System (IoT)

Sistem pemantauan dan kontrol mikroklimat ruang operasi berbasis **ESP32** dan **Dual Barometric Sensor (BMP280)**. Sistem ini menjaga tekanan positif ruangan (*positive pressure*) dan kestabilan temperatur untuk mencegah kontaminasi udara luar, serta menyajikan telemetri *real-time* via **MQTT (WebSocket)** ke dashboard visual interaktif.

🔗 **Live Dashboard Demo:** [Buka Web Dashboard](https://rapskuyy.github.io/Sistem-Monitoring-dan-Kontrol-Ruang-Operasi-Berbasi-IoT/)

---

## 📌 Fitur Utama

- **Dual Sensor Differential Monitoring:** Mengukur tekanan atmosfer dan suhu di dua titik (dalam dan luar ruangan) secara simultan menggunakan bus I2C tunggal.
- **Automated Actuator Logic:**
  - **Blower Control:** Aktif otomatis saat diferensial tekanan ($\Delta P$) di bawah ambang aman ($< 2.0\ \text{hPa}$) untuk menjaga tekanan positif.
  - **Heater Control:** Aktif otomatis ketika selisih suhu luar terhadap dalam ($\Delta T$) melampaui batas toleransi ($> 5.0\ ^\circ\text{C}$).
- **Non-blocking Telemetry:** Koneksi Wi-Fi dan MQTT berjalan tanpa fungsi `delay()` yang memblokir eksekusi sensor dan relay.
- **Client-Side Real-Time Dashboard:** Menghubungkan browser langsung ke broker MQTT via Secure WebSocket (`WSS`) tanpa server/backend perantara.

---

## 🛠️ Skema Perangkat Keras & Pinout

ESP32 berkomunikasi dengan kedua modul BMP280 melalui protokol I2C menggunakan pembeda alamat *hardware* (pin `SDO/ADDR`):

| Komponen / Aktuator | Pin ESP32 | Keterangan / Konfigurasi |
|---|---|---|
| **I2C SDA** | `GPIO 21` | Pin data I2C bersama (pull-up internal/eksternal) |
| **I2C SCL** | `GPIO 22` | Pin clock I2C bersama |
| **BMP280 (Dalam Ruangan)** | I2C (`0x76`) | Pin `SDO` dihubungkan ke `GND` |
| **BMP280 (Luar Ruangan)** | I2C (`0x77`) | Pin `SDO` dihubungkan ke `VCC (3.3V)` |
| **Relay Blower** | `GPIO 25` | Output digital (`HIGH` = Aktif, `LOW` = Mati) |
| **Relay Heater** | `GPIO 26` | Output digital (`HIGH` = Aktif, `LOW` = Mati) |
| **Power Supply** | `3.3V` & `GND` | Sumber daya modul sensor BMP280 |

---

## 📡 Arsitektur & Spesifikasi MQTT

| Parameter | Nilai |
|---|---|
| **Broker** | `broker.hivemq.com` |
| **Port TCP (ESP32)** | `1883` |
| **Port WebSocket (Dashboard)** | `8884` (WSS / Secure) |
| **Topik Telemetri** | `ruangop/<nama_device-anda>/telemetry` |
| **Topik Status** | `ruangop/<nama_device_anda>/status` |
| **Interval Pengiriman** | 1000 ms (1 Hz) |

### Format Payload Telemetri (JSON)

```json
{
  "ts": 128450,
  "tempIn": 24.3,
  "tempOut": 30.1,
  "deltaTemp": 5.8,
  "presIn": 1013.25,
  "presOut": 1010.80,
  "deltaPres": 2.45,
  "blower": false,
  "heater": true,
  "pressureStatus": "NORMAL",
  "temperatureStatus": "HIGH"
}
```

---

## 📁 Struktur Direktori

```plaintext
.
├── firmware/OR_Control_MQTT
│   └── OR_Control_MQTT.ino      # Source code Arduino / ESP32
├── docs/
│   └── index.html             # Dashboard client-side (GitHub Pages)
└── README.md                  # Dokumentasi proyek
```

---

## 🚀 Panduan Instalasi & Penggunaan

### 1. Konfigurasi ESP32 (Firmware)

1. Buka `firmware/esp32_control.ino` di Arduino IDE atau VS Code (PlatformIO).
2. Pasang library berikut melalui Library Manager:
   - **Adafruit BMP280 Library** by Adafruit
   - **PubSubClient** by Nick O'Leary
   - **ArduinoJson** (kompatibel v6 / v7) by Benoit Blanchon
3. Sesuaikan konfigurasi jaringan di bagian atas file:

   ```cpp
   const char* WIFI_SSID     = "NAMA_WIFI_ANDA";
   const char* WIFI_PASSWORD = "PASSWORD_WIFI_ANDA";
   ```

4. Pilih board **ESP32 Dev Module**, tentukan port COM yang sesuai, lalu lakukan **Upload**.
5. Buka **Serial Monitor** pada baud rate `115200` untuk memverifikasi inisialisasi sensor dan koneksi broker.

### 2. Menjalankan Dashboard

- **Secara Online (GitHub Pages):** Akses melalui URL GitHub Pages repositori ini. Dashboard akan langsung mendengarkan aliran telemetri secara otomatis.
- **Secara Lokal:** Buka file `docs/index.html` menggunakan browser modern (Chrome, Edge, Firefox) tanpa memerlukan web server lokal.

---

## ⚙️ Logika Kontrol Ruangan

```plaintext
[BMP280 Inside]  \
                  --> [Kalkulasi Delta] --> [Evaluasi Batas Ambang] --> [Trigger Relay]
[BMP280 Outside] /
```

### Tekanan ($\Delta P = P_{in} - P_{out}$)

| Kondisi | Status | Blower |
|---|---|---|
| $\Delta P < 0.0\ \text{hPa}$ | Tekanan negatif (**KRITIS**) | ON |
| $0.0 \le \Delta P < 2.0\ \text{hPa}$ | Tekanan di bawah standar | ON |
| $2.0 \le \Delta P \le 3.0\ \text{hPa}$ | Target ideal ruang isolasi/operasi | OFF |
| $\Delta P > 3.0\ \text{hPa}$ | Tekanan tinggi | OFF |

### Suhu ($\Delta T = T_{out} - T_{in}$)

| Kondisi | Status | Heater |
|---|---|---|
| $\Delta T > 5.0\ ^\circ\text{C}$ | Suhu dalam terlalu dingin dibanding lingkungan | ON |
| $\Delta T \le 5.0\ ^\circ\text{C}$ | Suhu dalam dalam toleransi normal | OFF |
