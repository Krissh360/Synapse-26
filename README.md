# EEMN — Edge-AI Environmental Monitoring Network

Distributed, offline-capable sensor network for early flood detection, built for **TEKATHON-5.0 (SIH26178)** by **Team Synapse**.

EEMN uses low-cost ESP32/ESP8266 nodes placed along a waterway (dam, bridge, and other sites) to measure water level and ground vibration in real time. Each node makes a local, rule-based safety decision — triggering a siren instantly, with no dependency on internet or a central server — while also publishing telemetry and event data over MQTT for network-wide monitoring and flood-wavefront tracking.

## Why EEMN

- **One network, multiple hazards** — a common ESP32-class hardware core with swappable sensors, extendable beyond flood to fire, air quality, and structural monitoring.
- **Measured, not assumed** — flood arrival time is estimated from real, measured signal changes rather than static assumptions.
- **Safety before AI** — a deterministic rule-based trigger is the first line of defense, independent of any cloud or ML layer.
- **Works without internet** — local alerts fire immediately at the node; data syncs to the network once connectivity is available.

## How it works

Each node continuously reads:

- **Water level** — HC-SR04 ultrasonic distance sensor
- **Ground/structural vibration** — MPU6050 accelerometer

Two independent signals are combined into a trigger decision:

1. **Absolute threshold** — an immediate trigger if the measured distance drops to a critical level.
2. **Short-term vs. long-term average (STA/LTA) ratio** — detects sudden rate-of-change spikes in distance and vibration, catching fast-developing events even before the absolute threshold is crossed.

A local buzzer is driven directly off the absolute distance threshold with hysteresis (separate ON/OFF bands) to avoid flicker at the boundary. In parallel, the node publishes:

- Rolling **telemetry** (distance, vibration magnitude) over MQTT
- A one-shot **event** message when a flood or vibration trigger fires, debounced with a cooldown window to prevent duplicate alerts

## Repository contents

| File | Node | Board | Notes |
|---|---|---|---|
| `dam_node.ino` | Dam site | ESP32 | Wi-Fi via built-in `WiFi.h` |
| `bridge_node.ino` | Bridge site | ESP8266 (NodeMCU) | Wi-Fi via `ESP8266WiFi.h`, different GPIO mapping |

Both nodes share the same sensing and trigger logic; only the board-specific pin definitions and Wi-Fi library differ.

## Hardware

- ESP32 or ESP8266 (NodeMCU) microcontroller
- HC-SR04 ultrasonic distance sensor
- MPU6050 accelerometer/gyroscope (I²C)
- Piezo buzzer (direct GPIO drive)
- MQTT broker reachable on the local network (e.g., Mosquitto on a laptop/hotspot)

## Configuration

Before flashing, update the following in the sketch:

```cpp
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";
const char* mqtt_server = "YOUR_BROKER_IP";
```

Key tunables:

| Parameter | Purpose |
|---|---|
| `BUZZ_ON_DISTANCE_CM` / `BUZZ_OFF_DISTANCE_CM` | Hysteresis band for the local buzzer |
| `DANGER_DISTANCE_CM` | Absolute distance that triggers an MQTT flood event |
| `DIST_TRIGGER_RATIO` / `VIB_TRIGGER_RATIO` | STA/LTA sensitivity for distance and vibration |
| `STA_WINDOW` / `LTA_WINDOW` | Short-term / long-term averaging window sizes |
| `EVENT_COOLDOWN` | Minimum time between repeated event publishes |

## MQTT topics

| Topic | Payload | Description |
|---|---|---|
| `flood/<node_id>/telemetry` | `{"node", "distance", "vib_mag"}` | Continuous sensor readings |
| `flood/<node_id>/event` | `{"node", "flood", "vibration", "timestamp"}` | Published once when a trigger condition fires |

## Status

Current stage: working firmware for two sensor nodes (dam, bridge), publishing telemetry and events over MQTT with local buzzer alerting. Next steps include flood-wavefront ETA calculation from multi-node data and a central dashboard/alerting layer.

## Team

**Team Synapse** — TEKATHON-5.0, 2026 — Problem Statement SIH26178
