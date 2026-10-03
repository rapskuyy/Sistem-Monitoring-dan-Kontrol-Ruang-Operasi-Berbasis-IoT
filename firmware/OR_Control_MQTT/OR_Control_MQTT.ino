#include <WiFi.h>
#include <WiFiClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>

Adafruit_BMP280 bmpInside;
Adafruit_BMP280 bmpOutside;

// =====================================================
// WIFI
// =====================================================

const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// =====================================================
// MQTT
// =====================================================

// Public broker for prototyping only. For a real deployment,
// run your own broker (e.g. Mosquitto) with auth + TLS.
const char* MQTT_BROKER = "broker.hivemq.com";
const int   MQTT_PORT   = 1883;

// Give this device a unique id so it doesn't collide with
// other people using the same public broker.
const char* DEVICE_ID = "YOUR_DEVICE_ID";

String TOPIC_TELEMETRY;
String TOPIC_STATUS;
String MQTT_CLIENT_ID;

WiFiClient espClient;
PubSubClient mqttClient(espClient);

unsigned long lastMqttAttempt = 0;
const unsigned long mqttRetryInterval = 5000;

// =====================================================
// PIN
// =====================================================

const int blowerPin = 25;
const int heaterPin = 26;

// =====================================================
// PRESSURE CONTROL
// =====================================================

// Target positive pressure
const float TARGET_DP = 2.0;

// Minimum acceptable positive pressure
const float MIN_DP = 2.0;

// Maximum desired pressure
const float MAX_DP = 3.0;

// =====================================================
// TEMPERATURE CONTROL
// =====================================================

// Maximum allowed delta temperature
const float MAX_DELTA_TEMP = 5.0;

bool blowerState = false;
bool heaterState = false;

// =====================================================
// CONTROL INTERVAL
// =====================================================

unsigned long lastControl = 0;

const unsigned long controlInterval = 1000;


// =====================================================
// WIFI CONNECT
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

    // Keep actuators OFF while we don't have network yet,
    // but don't block the board forever on a bad WiFi config.
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

  if (mqttClient.connect(MQTT_CLIENT_ID.c_str())) {

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

  MQTT_CLIENT_ID    = String("esp32-") + DEVICE_ID;
  TOPIC_TELEMETRY   = String("ruangop/") + DEVICE_ID + "/telemetry";
  TOPIC_STATUS      = String("ruangop/") + DEVICE_ID + "/status";

  // ---------------------------------------------------
  // Blower
  // ---------------------------------------------------

  pinMode(
    blowerPin,
    OUTPUT
  );

  // Blower OFF saat startup
  digitalWrite(
    blowerPin,
    LOW
  );

  // ---------------------------------------------------
  // Heater
  // ---------------------------------------------------

  pinMode(
    heaterPin,
    OUTPUT
  );

  // Heater OFF saat startup
  digitalWrite(
    heaterPin,
    LOW
  );

  // ---------------------------------------------------
  // BMP280 Inside
  // ---------------------------------------------------

  if (!bmpInside.begin(0x76)) {

    Serial.println(
      "ERROR: BMP280 INSIDE tidak ditemukan!"
    );

    while (1) {

      digitalWrite(
        blowerPin,
        LOW
      );

      digitalWrite(
        heaterPin,
        LOW
      );

      delay(1000);
    }
  }

  // ---------------------------------------------------
  // BMP280 Outside
  // ---------------------------------------------------

  if (!bmpOutside.begin(0x77)) {

    Serial.println(
      "ERROR: BMP280 OUTSIDE tidak ditemukan!"
    );

    while (1) {

      digitalWrite(
        blowerPin,
        LOW
      );

      digitalWrite(
        heaterPin,
        LOW
      );

      delay(1000);
    }
  }

  // ---------------------------------------------------
  // WiFi + MQTT
  // ---------------------------------------------------

  connectWiFi();

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);

  // ---------------------------------------------------
  // Startup message
  // ---------------------------------------------------

  Serial.println();
  Serial.println(
    "======================================"
  );

  Serial.println(
    " OPERATING ROOM CONTROL SYSTEM"
  );

  Serial.println(
    "======================================"
  );

  Serial.println(
    "BMP280 Inside  : OK"
  );

  Serial.println(
    "BMP280 Outside : OK"
  );

  Serial.print(
    "Target Pressure : +"
  );

  Serial.print(
    TARGET_DP
  );

  Serial.println(
    " hPa"
  );

  Serial.print(
    "Minimum Pressure : +"
  );

  Serial.print(
    MIN_DP
  );

  Serial.println(
    " hPa"
  );

  Serial.print(
    "Maximum Delta Temperature : "
  );

  Serial.print(
    MAX_DELTA_TEMP
  );

  Serial.println(
    " C"
  );

  Serial.print(
    "MQTT Telemetry Topic : "
  );
  Serial.println(TOPIC_TELEMETRY);

  Serial.println();
}


// =====================================================
// LOOP
// =====================================================

void loop() {

  reconnectMqttIfNeeded();
  mqttClient.loop();

  if (
    millis() - lastControl
    < controlInterval
  ) {

    return;
  }

  lastControl = millis();


  // ===================================================
  // READ SENSORS
  // ===================================================

  float temperatureInside =
    bmpInside.readTemperature();

  float temperatureOutside =
    bmpOutside.readTemperature();

  float pressureInside =
    bmpInside.readPressure() / 100.0;

  float pressureOutside =
    bmpOutside.readPressure() / 100.0;


  // ===================================================
  // CALCULATE DELTA
  // ===================================================

  float deltaPressure =
    pressureInside -
    pressureOutside;

  // Delta temperature:
  // Outside - Inside

  float deltaTemperature =
    temperatureOutside -
    temperatureInside;


  // ===================================================
  // PRESSURE CONTROL - BLOWER ON/OFF
  // ===================================================

  /*
     LOGIC BLOWER:

     Jika delta pressure < 1.5 hPa
     → BLOWER ON

     Jika delta pressure >= 1.5 hPa
     → BLOWER OFF

     Karena blower hanya bisa ON/OFF,
     tidak ada pengaturan kecepatan.
  */

  if (
    deltaPressure < MIN_DP
  ) {

    blowerState = true;

  }

  else {

    blowerState = false;
  }


  // Apply blower state

  if (blowerState) {

    digitalWrite(
      blowerPin,
      HIGH
    );

  }

  else {

    digitalWrite(
      blowerPin,
      LOW
    );
  }


  // ===================================================
  // TEMPERATURE CONTROL - HEATER
  // ===================================================

  /*
     LOGIC HEATER:

     Delta Temperature = Outside - Inside

     Jika delta > 5°C
     → HEATER ON

     Jika delta <= 5°C
     → HEATER OFF
  */

  if (
    deltaTemperature > MAX_DELTA_TEMP
  ) {

    heaterState = true;

  }

  else {

    heaterState = false;
  }


  // Apply heater state

  if (heaterState) {

    digitalWrite(
      heaterPin,
      HIGH
    );

  }

  else {

    digitalWrite(
      heaterPin,
      LOW
    );
  }


  // ===================================================
  // PRESSURE STATUS STRING
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


  // ===================================================
  // SERIAL MONITOR
  // ===================================================

  Serial.println(
    "======================================"
  );

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

  Serial.print("Blower              : ");
  Serial.println(blowerState ? "ON" : "OFF");

  Serial.print("Heater              : ");
  Serial.println(heaterState ? "ON" : "OFF");


  // ===================================================
  // PUBLISH TELEMETRY OVER MQTT
  // ===================================================

  if (mqttClient.connected()) {

    StaticJsonDocument<384> doc;

    doc["ts"]            = millis();
    doc["tempIn"]        = temperatureInside;
    doc["tempOut"]       = temperatureOutside;
    doc["deltaTemp"]     = deltaTemperature;
    doc["presIn"]        = pressureInside;
    doc["presOut"]       = pressureOutside;
    doc["deltaPres"]     = deltaPressure;
    doc["blower"]        = blowerState;
    doc["heater"]        = heaterState;
    doc["pressureStatus"]    = pressureStatus;
    doc["temperatureStatus"] = temperatureStatus;

    char payload[384];
    size_t len = serializeJson(doc, payload);

    mqttClient.publish(TOPIC_TELEMETRY.c_str(), payload, len);

  } else {

    Serial.println("MQTT not connected, skipping publish.");
  }
}
