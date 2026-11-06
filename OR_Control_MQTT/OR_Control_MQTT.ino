
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <time.h>
#include <math.h>



Adafruit_BMP280 bmpInside;
Adafruit_BMP280 bmpOutside;

const char* WIFI_SSID     = "";
const char* WIFI_PASSWORD = "";


// =====================================================
// NTP
// =====================================================

const char* NTP_SERVER = "pool.ntp.org";

// Jakarta / WIB = UTC +7 (timestamp diberi akhiran +07:00, sama dengan dataset)
const long GMT_OFFSET_SEC = 7 * 3600;
const int DAYLIGHT_OFFSET_SEC = 0;
bool ntpStarted = false;

// =====================================================
// MQTT
// =====================================================

// Broker publik: hanya untuk prototipe.
const char* MQTT_BROKER = "broker.hivemq.com";
const int   MQTT_PORT   = 1883;

// Harus unik supaya tidak bentrok dengan pengguna lain di broker publik.
// Topic di dashboard HARUS sama: ruangop/<DEVICE_ID>/telemetry
const char* DEVICE_ID = "operation_room_simulation";

String TOPIC_TELEMETRY;
String TOPIC_STATUS;
String TOPIC_EVENTS;
String MQTT_CLIENT_ID;

WiFiClient espClient;
PubSubClient mqttClient(espClient);

unsigned long lastMqttAttempt = 0;
const unsigned long mqttRetryInterval = 5000;

unsigned long lastWifiAttempt = 0;
const unsigned long wifiRetryInterval = 10000;

// =====================================================
// PIN
// =====================================================

const int blowerPin = 25;
const int heaterPin = 26;
const int mq135Pin  = 34;   // ADC1 (aman dipakai bersama WiFi)

// =====================================================
// PRESSURE CONTROL (hPa)
// =====================================================

// Blower ON jika deltaP < BLOWER_ON_DP
// Blower OFF jika deltaP > BLOWER_OFF_DP
// Di antara keduanya: state dipertahankan (hysteresis, titik tengah = 2.0 hPa)
const float BLOWER_ON_DP  = 1.8;
const float BLOWER_OFF_DP = 2.2;

// Untuk klasifikasi status tekanan (sama seperti Assignment 1)
const float MIN_DP = 2.0;
const float MAX_DP = 3.0;

// =====================================================
// TEMPERATURE CONTROL (deg C)
// =====================================================

// deltaTemp = Outside - Inside
// Heater ON jika deltaTemp > MAX_DELTA_TEMP
// Heater OFF jika deltaTemp < HEATER_OFF_DT
const float MAX_DELTA_TEMP = 5.0;
const float HEATER_OFF_DT  = 4.5;

// Proteksi: heater dipaksa OFF jika suhu dalam melebihi batas ini
const float MAX_INSIDE_TEMP = 40.0;

// =====================================================
// MQ135 (air quality, nilai relatif / belum dikalibrasi ke ppm)
// =====================================================

const int MQ135_SAMPLES = 16;                       // rata-rata untuk meredam noise
const unsigned long MQ135_WARMUP_MS = 180000UL;     // 3 menit setelah boot

// SESUAIKAN dengan baseline udara bersih dari sensormu
// (ukur raw saat udara bersih, lalu atur ambang di atas baseline).
const int AIR_MODERATE_RAW = 120;
const int AIR_POOR_RAW     = 200;

// =====================================================
// STATE
// =====================================================

bool blowerState = false;
bool heaterState = false;

// -1 = belum pernah dicatat (untuk event "initial_state")
int8_t lastLoggedBlower = -1;
int8_t lastLoggedHeater = -1;

unsigned long lastControl = 0;
const unsigned long controlInterval = 1000;

// =====================================================
// EVENT QUEUE (agar event tidak hilang saat MQTT putus)
// =====================================================

const int EVENT_QUEUE_SIZE = 10;
String eventQueue[EVENT_QUEUE_SIZE];
int eventHead = 0;
int eventCount = 0;

void queueEvent(const String& payload) {

  if (eventCount == EVENT_QUEUE_SIZE) {
    // Penuh: buang yang terlama
    eventHead = (eventHead + 1) % EVENT_QUEUE_SIZE;
    eventCount--;
  }

  eventQueue[(eventHead + eventCount) % EVENT_QUEUE_SIZE] = payload;
  eventCount++;
}

void flushEvents() {

  while (eventCount > 0 && mqttClient.connected()) {

    if (!mqttClient.publish(TOPIC_EVENTS.c_str(),
                            eventQueue[eventHead].c_str())) {
      break;   // coba lagi di loop berikutnya
    }

    eventQueue[eventHead] = "";
    eventHead = (eventHead + 1) % EVENT_QUEUE_SIZE;
    eventCount--;
  }
}

// =====================================================
// HELPERS
// =====================================================

float r2(float v) {
  return roundf(v * 100.0f) / 100.0f;
}

String getTimestamp() {

  struct tm timeinfo;

  // Timeout pendek agar loop tidak terblokir jika NTP belum sinkron
  if (!getLocalTime(&timeinfo, 50)) {
    return "TIME_NOT_SYNCED";
  }

  char timestamp[32];

  strftime(
    timestamp,
    sizeof(timestamp),
    "%Y-%m-%dT%H:%M:%S+07:00",
    &timeinfo
  );

  return String(timestamp);
}

int readMq135Raw() {

  long sum = 0;

  for (int i = 0; i < MQ135_SAMPLES; i++) {
    sum += analogRead(mq135Pin);
    delay(2);
  }

  return (int)(sum / MQ135_SAMPLES);
}

void logActuatorEvent(
  const char* actuator,
  bool state,
  const char* reason,
  float value,
  float threshold
) {

  StaticJsonDocument<320> doc;

  doc["timestamp"] = getTimestamp();
  doc["elapsedMs"] = millis();
  doc["actuator"]  = actuator;
  doc["state"]     = state ? "ON" : "OFF";
  doc["reason"]    = reason;
  doc["value"]     = r2(value);
  doc["threshold"] = r2(threshold);

  String payload;
  serializeJson(doc, payload);

  Serial.print("EVENT: ");
  Serial.println(payload);

  queueEvent(payload);
}

// =====================================================
// WIFI
// =====================================================

void connectWiFi() {

  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();

  while (WiFi.status() != WL_CONNECTED) {

    delay(300);
    Serial.print(".");

    if (millis() - startAttempt > 20000) {

      Serial.println();
      Serial.println("WARNING: WiFi belum terhubung, lanjut tanpa MQTT.");
      return;
    }
  }

  Serial.println();
  Serial.print("WiFi connected, IP: ");
  Serial.println(WiFi.localIP());
}

// Dipanggil setiap loop: reconnect WiFi (non-blocking) dan mulai NTP
// begitu WiFi tersedia (termasuk jika WiFi baru tersambung setelah boot).
void maintainWiFi() {

  if (WiFi.status() == WL_CONNECTED) {

    if (!ntpStarted) {
      configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);
      ntpStarted = true;
      Serial.println("NTP started.");
    }

    return;
  }

  if (millis() - lastWifiAttempt < wifiRetryInterval) {
    return;
  }

  lastWifiAttempt = millis();

  Serial.println("WiFi terputus, mencoba menyambung ulang...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// =====================================================
// MQTT RECONNECT (non-blocking)
// =====================================================

void reconnectMqttIfNeeded() {

  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  if (mqttClient.connected()) {
    return;
  }

  if (millis() - lastMqttAttempt < mqttRetryInterval) {
    return;
  }

  lastMqttAttempt = millis();

  Serial.print("Connecting to MQTT broker...");

  // Last Will: broker mem-publish "offline" (retained) jika ESP32 putus mendadak
  if (mqttClient.connect(
        MQTT_CLIENT_ID.c_str(),
        nullptr,
        nullptr,
        TOPIC_STATUS.c_str(),
        1,
        true,
        "offline"
      )) {

    Serial.println(" OK");
    mqttClient.publish(TOPIC_STATUS.c_str(), "online", true);

  } else {

    Serial.print(" FAILED, rc=");
    Serial.println(mqttClient.state());
  }
}

// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(115200);

  delay(1000);

  Wire.begin(21, 22);

  MQTT_CLIENT_ID  = String("esp32-") + DEVICE_ID;
  TOPIC_TELEMETRY = String("ruangop/") + DEVICE_ID + "/telemetry";
  TOPIC_STATUS    = String("ruangop/") + DEVICE_ID + "/status";
  TOPIC_EVENTS    = String("ruangop/") + DEVICE_ID + "/events";

  analogReadResolution(12);
  analogSetPinAttenuation(mq135Pin, ADC_11db);

  // Blower & heater OFF saat startup
  pinMode(blowerPin, OUTPUT);
  digitalWrite(blowerPin, LOW);

  pinMode(heaterPin, OUTPUT);
  digitalWrite(heaterPin, LOW);

  // BMP280 Inside
  if (!bmpInside.begin(0x76)) {

    Serial.println("ERROR: BMP280 INSIDE tidak ditemukan!");

    while (1) {
      digitalWrite(blowerPin, LOW);
      digitalWrite(heaterPin, LOW);
      delay(1000);
    }
  }

  // BMP280 Outside
  if (!bmpOutside.begin(0x77)) {

    Serial.println("ERROR: BMP280 OUTSIDE tidak ditemukan!");

    while (1) {
      digitalWrite(blowerPin, LOW);
      digitalWrite(heaterPin, LOW);
      delay(1000);
    }
  }

  // WiFi + NTP + MQTT
  connectWiFi();

  if (WiFi.status() == WL_CONNECTED) {

    configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);
    ntpStarted = true;

    Serial.println("Synchronizing time with NTP...");

    struct tm timeinfo;

    if (getLocalTime(&timeinfo, 10000)) {
      Serial.println("NTP time synchronized!");
      Serial.println(getTimestamp());
    } else {
      Serial.println("WARNING: NTP synchronization failed!");
    }
  }

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);

  // PENTING: default PubSubClient hanya 256 byte, payload telemetry lebih besar
  mqttClient.setBufferSize(1024);

  Serial.println();
  Serial.println("======================================");
  Serial.println(" OPERATING ROOM CONTROL SYSTEM v2");
  Serial.println("======================================");
  Serial.println("BMP280 Inside  : OK");
  Serial.println("BMP280 Outside : OK");
  Serial.print("MQ135 warm-up  : ");
  Serial.print(MQ135_WARMUP_MS / 1000);
  Serial.println(" s");
  Serial.print("Blower ON < ");
  Serial.print(BLOWER_ON_DP);
  Serial.print(" hPa, OFF > ");
  Serial.print(BLOWER_OFF_DP);
  Serial.println(" hPa");
  Serial.print("Heater ON > ");
  Serial.print(MAX_DELTA_TEMP);
  Serial.print(" C, OFF < ");
  Serial.print(HEATER_OFF_DT);
  Serial.println(" C");
  Serial.print("Telemetry topic : ");
  Serial.println(TOPIC_TELEMETRY);
  Serial.print("Events topic    : ");
  Serial.println(TOPIC_EVENTS);
  Serial.println();
}

// =====================================================
// LOOP
// =====================================================

void loop() {

  maintainWiFi();
  reconnectMqttIfNeeded();
  mqttClient.loop();
  flushEvents();

  if (millis() - lastControl < controlInterval) {
    return;
  }

  lastControl = millis();

  // ===================================================
  // READ SENSORS
  // ===================================================

  int mq135Raw = readMq135Raw();
  float mq135VoltageMV = mq135Raw * (3.3 / 4095.0) * 1000.0;

  float temperatureInside  = bmpInside.readTemperature();
  float temperatureOutside = bmpOutside.readTemperature();
  float pressureInside     = bmpInside.readPressure() / 100.0;
  float pressureOutside    = bmpOutside.readPressure() / 100.0;

  bool sensorsOk =
    !(isnan(temperatureInside)  || isnan(temperatureOutside) ||
      isnan(pressureInside)     || isnan(pressureOutside));

  // ===================================================
  // SENSOR FAULT -> aktuator OFF, jangan kirim data palsu
  // ===================================================

  if (!sensorsOk) {

    Serial.println("ERROR: pembacaan BMP280 tidak valid, aktuator OFF.");

    blowerState = false;
    heaterState = false;

    digitalWrite(blowerPin, LOW);
    digitalWrite(heaterPin, LOW);

    if ((int8_t)blowerState != lastLoggedBlower) {
      logActuatorEvent("blower", false, "sensor_fault", 0, 0);
      lastLoggedBlower = 0;
    }

    if ((int8_t)heaterState != lastLoggedHeater) {
      logActuatorEvent("heater", false, "sensor_fault", 0, 0);
      lastLoggedHeater = 0;
    }

    return;
  }

  // ===================================================
  // CALCULATE DELTA
  // ===================================================

  float deltaPressure    = pressureInside - pressureOutside;
  float deltaTemperature = temperatureOutside - temperatureInside;

  // ===================================================
  // BLOWER (rule-based + hysteresis)
  // ===================================================

  const char* blowerReason = "hysteresis_hold";
  float blowerThreshold = BLOWER_ON_DP;

  if (deltaPressure < BLOWER_ON_DP) {

    blowerState = true;
    blowerReason = "deltaP_below_on_threshold";
    blowerThreshold = BLOWER_ON_DP;

  } else if (deltaPressure > BLOWER_OFF_DP) {

    blowerState = false;
    blowerReason = "deltaP_above_off_threshold";
    blowerThreshold = BLOWER_OFF_DP;
  }

  digitalWrite(blowerPin, blowerState ? HIGH : LOW);

  // ===================================================
  // HEATER (rule-based + hysteresis + proteksi)
  // ===================================================

  const char* heaterReason = "hysteresis_hold";
  float heaterThreshold = MAX_DELTA_TEMP;
  float heaterValue = deltaTemperature;

  if (temperatureInside > MAX_INSIDE_TEMP) {

    heaterState = false;
    heaterReason = "over_temp_cutoff";
    heaterThreshold = MAX_INSIDE_TEMP;
    heaterValue = temperatureInside;

  } else if (deltaTemperature > MAX_DELTA_TEMP) {

    heaterState = true;
    heaterReason = "deltaT_above_on_threshold";
    heaterThreshold = MAX_DELTA_TEMP;

  } else if (deltaTemperature < HEATER_OFF_DT) {

    heaterState = false;
    heaterReason = "deltaT_below_off_threshold";
    heaterThreshold = HEATER_OFF_DT;
  }

  digitalWrite(heaterPin, heaterState ? HIGH : LOW);

  // ===================================================
  // LOG ACTUATOR EVENTS (hanya saat state berubah)
  // ===================================================

  if ((int8_t)blowerState != lastLoggedBlower) {

    logActuatorEvent(
      "blower",
      blowerState,
      (lastLoggedBlower == -1) ? "initial_state" : blowerReason,
      deltaPressure,
      blowerThreshold
    );

    lastLoggedBlower = (int8_t)blowerState;
  }

  if ((int8_t)heaterState != lastLoggedHeater) {

    logActuatorEvent(
      "heater",
      heaterState,
      (lastLoggedHeater == -1) ? "initial_state" : heaterReason,
      heaterValue,
      heaterThreshold
    );

    lastLoggedHeater = (int8_t)heaterState;
  }

  // ===================================================
  // STATUS STRINGS
  // ===================================================

  String pressureStatus;

  if (deltaPressure < 0) {
    pressureStatus = "NEGATIVE";
  } else if (deltaPressure < MIN_DP) {
    pressureStatus = "LOW";
  } else if (deltaPressure <= MAX_DP) {
    pressureStatus = "NORMAL";
  } else {
    pressureStatus = "HIGH";
  }

  String temperatureStatus =
    (deltaTemperature > MAX_DELTA_TEMP) ? "HIGH" : "NORMAL";

  String airStatus;

  if (millis() < MQ135_WARMUP_MS) {
    airStatus = "WARMUP";
  } else if (mq135Raw >= AIR_POOR_RAW) {
    airStatus = "POOR";
  } else if (mq135Raw >= AIR_MODERATE_RAW) {
    airStatus = "MODERATE";
  } else {
    airStatus = "GOOD";
  }

  // ===================================================
  // SERIAL MONITOR
  // ===================================================

  Serial.println("======================================");

  Serial.print("Delta Pressure      : ");
  Serial.print(deltaPressure, 2);
  Serial.print(" hPa  [");
  Serial.print(pressureStatus);
  Serial.println("]");

  Serial.print("Delta Temperature   : ");
  Serial.print(deltaTemperature, 2);
  Serial.print(" C    [");
  Serial.print(temperatureStatus);
  Serial.println("]");

  Serial.print("MQ135 raw / mV      : ");
  Serial.print(mq135Raw);
  Serial.print(" / ");
  Serial.print(mq135VoltageMV, 1);
  Serial.print("  [");
  Serial.print(airStatus);
  Serial.println("]");

  Serial.print("Blower              : ");
  Serial.println(blowerState ? "ON" : "OFF");

  Serial.print("Heater              : ");
  Serial.println(heaterState ? "ON" : "OFF");

  // ===================================================
  // PUBLISH TELEMETRY OVER MQTT
  // ===================================================

  if (mqttClient.connected()) {

    StaticJsonDocument<768> doc;

    doc["timestamp"]         = getTimestamp();
    doc["elapsedMs"]         = millis();
    doc["tempIn"]            = r2(temperatureInside);
    doc["tempOut"]           = r2(temperatureOutside);
    doc["deltaTemp"]         = r2(deltaTemperature);
    doc["presIn"]            = r2(pressureInside);
    doc["presOut"]           = r2(pressureOutside);
    doc["deltaPres"]         = r2(deltaPressure);
    doc["mq135Raw"]          = mq135Raw;
    doc["mq135mV"]           = r2(mq135VoltageMV);
    doc["blower"]            = blowerState;
    doc["heater"]            = heaterState;
    doc["pressureStatus"]    = pressureStatus;
    doc["temperatureStatus"] = temperatureStatus;
    doc["airStatus"]         = airStatus;

    char payload[768];
    size_t len = serializeJson(doc, payload, sizeof(payload));

    mqttClient.publish(TOPIC_TELEMETRY.c_str(), payload, len);

  } else {

    Serial.println("MQTT not connected, skipping publish.");
  }
}
