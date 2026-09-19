#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>

// ---- WiFi + MQTT config ----
const char* ssid = "POCO M4 Pro";
const char* password = "realmadrid";
const char* mqtt_server = "10.174.15.226";
const char* node_id = "dam_node";

WiFiClient espClient;
PubSubClient client(espClient);

// ---- HC-SR04 pins ----
#define TRIG_PIN 5
#define ECHO_PIN 18

// ---- MPU6050 ----
const int MPU_ADDR = 0x68;

// ---- Buzzer (direct GPIO, no relay) ----
#define BUZZER_PIN 4

// ---- STA/LTA config (used for MQTT event detection, NOT the buzzer) ----
#define STA_WINDOW 5
#define LTA_WINDOW 30
#define DIST_TRIGGER_RATIO 2.5
#define VIB_TRIGGER_RATIO 3.0

// ---- Buzzer distance thresholds, with hysteresis to stop flicker ----
#define BUZZ_ON_DISTANCE_CM 10.0
#define BUZZ_OFF_DISTANCE_CM 14.0

// ---- Absolute danger threshold (for MQTT event, separate from buzzer) ----
#define DANGER_DISTANCE_CM 10.0

float distRateBuffer[LTA_WINDOW] = {0};
float vibRateBuffer[LTA_WINDOW] = {0};
int bufferIndex = 0;
bool bufferFilled = false;

float lastDistance = 0;
float lastVibMag = 0;
bool firstReading = true;

bool eventActive = false;
unsigned long lastEventTime = 0;
const unsigned long EVENT_COOLDOWN = 5000;

bool buzzerOn = false;

void setup_wifi() {
  delay(10);
  Serial.print("Connecting to WiFi");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected. IP: " + WiFi.localIP().toString());
}

void reconnect_mqtt() {
  while (!client.connected()) {
    Serial.print("Connecting to MQTT...");
    if (client.connect(node_id)) {
      Serial.println("connected");
    } else {
      Serial.print("failed, rc=");
      Serial.print(client.state());
      Serial.println(" retrying in 2s");
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);

  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  Wire.begin(21, 22);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0);
  Wire.endTransmission(true);

  setup_wifi();
  client.setServer(mqtt_server, 1883);

  Serial.println("Node initialized.");
}

float readDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  if (duration == 0) return lastDistance;
  return duration * 0.0343 / 2;
}

float readVibrationMagnitude() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 6, true);

  int16_t ax = Wire.read() << 8 | Wire.read();
  int16_t ay = Wire.read() << 8 | Wire.read();
  int16_t az = Wire.read() << 8 | Wire.read();

  float mag = sqrt((float)ax * ax + (float)ay * ay + (float)az * az);
  return mag;
}

float computeAverage(float* buffer, int count) {
  float sum = 0;
  for (int i = 0; i < count; i++) sum += buffer[i];
  return sum / count;
}

void loop() {
  if (!client.connected()) {
    reconnect_mqtt();
  }
  client.loop();

  float distance = readDistance();
  float vibMag = readVibrationMagnitude();

  if (firstReading) {
    lastDistance = distance;
    lastVibMag = vibMag;
    firstReading = false;
    delay(300);
    return;
  }

  float distRate = abs(lastDistance - distance);
  float vibRate = abs(lastVibMag - vibMag);

  lastDistance = distance;
  lastVibMag = vibMag;

  distRateBuffer[bufferIndex] = distRate;
  vibRateBuffer[bufferIndex] = vibRate;
  bufferIndex = (bufferIndex + 1) % LTA_WINDOW;
  if (bufferIndex == 0) bufferFilled = true;

  int count = bufferFilled ? LTA_WINDOW : bufferIndex;

  if (!buzzerOn && distance <= BUZZ_ON_DISTANCE_CM) {
    buzzerOn = true;
  } else if (buzzerOn && distance > BUZZ_OFF_DISTANCE_CM) {
    buzzerOn = false;
  }
  digitalWrite(BUZZER_PIN, buzzerOn ? HIGH : LOW);

  if (count < STA_WINDOW) {
    delay(300);
    return;
  }

  float distSTA = 0, vibSTA = 0;
  for (int i = 0; i < STA_WINDOW; i++) {
    int idx = (bufferIndex - 1 - i + LTA_WINDOW) % LTA_WINDOW;
    distSTA += distRateBuffer[idx];
    vibSTA += vibRateBuffer[idx];
  }
  distSTA /= STA_WINDOW;
  vibSTA /= STA_WINDOW;

  float distLTA = computeAverage(distRateBuffer, count);
  float vibLTA = computeAverage(vibRateBuffer, count);

  float distRatio = (distLTA > 0.01) ? distSTA / distLTA : 0;
  float vibRatio = (vibLTA > 0.01) ? vibSTA / vibLTA : 0;

  bool rateTrigger = distRatio > DIST_TRIGGER_RATIO;
  bool absoluteThresholdTrigger = distance <= DANGER_DISTANCE_CM;
  bool floodTrigger = rateTrigger || absoluteThresholdTrigger;
  bool vibTrigger = vibRatio > VIB_TRIGGER_RATIO;

  Serial.print("Dist: "); Serial.print(distance);
  Serial.print(" cm | distRatio: "); Serial.print(distRatio);
  Serial.print(" | vibRatio: "); Serial.print(vibRatio);
  Serial.print(" | FLOOD: "); Serial.print(floodTrigger ? "YES" : "no");
  Serial.print(floodTrigger ? (absoluteThresholdTrigger ? " (threshold)" : " (rate)") : "");
  Serial.print(" | VIB: "); Serial.print(vibTrigger ? "YES" : "no");
  Serial.print(" | BUZZER: "); Serial.println(buzzerOn ? "ON" : "off");

  String telemetryPayload = String("{\"node\":\"") + node_id +
                             "\",\"distance\":" + distance +
                             ",\"vib_mag\":" + vibMag + "}";
  client.publish((String("flood/") + node_id + "/telemetry").c_str(), telemetryPayload.c_str());

  if ((floodTrigger || vibTrigger) && !eventActive) {
    String eventPayload = String("{\"node\":\"") + node_id +
                           "\",\"flood\":" + (floodTrigger ? "true" : "false") +
                           ",\"vibration\":" + (vibTrigger ? "true" : "false") +
                           ",\"timestamp\":" + millis() + "}";
    client.publish((String("flood/") + node_id + "/event").c_str(), eventPayload.c_str());
    Serial.println(">>> EVENT PUBLISHED <<<");
    eventActive = true;
    lastEventTime = millis();
  }

  if (!floodTrigger && !vibTrigger && (millis() - lastEventTime > EVENT_COOLDOWN)) {
    eventActive = false;
  }

  delay(300);
}
