import paho.mqtt.client as mqtt
import json
import time
import threading
from collections import deque
from flask import Flask, jsonify

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
NODE_DISTANCE_M = 500
CORRELATION_WINDOW_S = 60

DANGER_DISTANCE_CM = 10.0
WARN_DISTANCE_CM = 20.0
HISTORY_LEN = 60

lock = threading.RLock()
node_status = {}
event_times = {}
correlation_log = []
structural_flags = {}
event_feed = deque(maxlen=40)
history = {}

app = Flask(__name__)


def log_feed(text, kind="info"):
    event_feed.appendleft({
        "time": time.strftime("%H:%M:%S"),
        "text": text,
        "kind": kind
    })


def on_connect(client, userdata, flags, rc, properties=None):
    print(f"[Gateway] Connected to broker (rc={rc})")
    result = client.subscribe("flood/#")
    print(f"[Gateway] Subscribe call result: {result}")
    log_feed("Gateway connected to MQTT broker", "info")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception as e:
        print(f"[ERROR] Failed to parse payload: {e}")
        return

    topic_parts = msg.topic.split("/")
    if len(topic_parts) != 3:
        return
    node_id = topic_parts[1]
    msg_type = topic_parts[2]

    now = time.time()

    with lock:
        if msg_type == "telemetry":
            dist = payload.get("distance")
            vib = payload.get("vib_mag")

            known = node_id in node_status
            node_status[node_id] = {
                "distance": dist,
                "vib_mag": vib,
                "last_seen": now
            }

            if node_id not in history:
                history[node_id] = {
                    "dist": deque(maxlen=HISTORY_LEN),
                    "vib": deque(maxlen=HISTORY_LEN)
                }
            if dist is not None:
                history[node_id]["dist"].append(round(dist, 2))
            if vib is not None:
                history[node_id]["vib"].append(round(vib / 16384.0, 3))

            if not known:
                log_feed(f"{node_id} came online and started reporting", "info")

        elif msg_type == "event":
            flood = payload.get("flood", False)
            vibration = payload.get("vibration", False)
            print(f"\n[EVENT] {node_id} -> flood={flood}, vibration={vibration}")

            if flood:
                d = node_status.get(node_id, {}).get("distance")
                dtxt = f" (water at {d:.1f} cm)" if d is not None else ""
                log_feed(f"{node_id} detected rapid water rise{dtxt}", "flood")
                event_times[node_id] = now
                check_correlation()

            if vibration:
                log_feed(f"{node_id} detected significant vibration", "vib")
                broadcast_structural_check(node_id)


def check_correlation():
    if "dam_node" not in event_times or "bridge_node" not in event_times:
        return

    dam_time = event_times["dam_node"]
    bridge_time = event_times["bridge_node"]
    dt = bridge_time - dam_time

    if dt < 0:
        print(f"\n[WARNING] Bridge triggered before dam ({-dt:.2f}s early) — ignoring\n")
        log_feed("Bridge triggered before dam — trigger dam first for a valid reading", "warn")
        return

    if dt > CORRELATION_WINDOW_S or dt == 0:
        return

    velocity = NODE_DISTANCE_M / dt

    result = {
        "time": time.strftime("%H:%M:%S"),
        "dam_time": time.strftime("%H:%M:%S", time.localtime(dam_time)),
        "bridge_time": time.strftime("%H:%M:%S", time.localtime(bridge_time)),
        "dt_seconds": round(dt, 2),
        "velocity_mps": round(velocity, 2),
        "distance_m": NODE_DISTANCE_M
    }

    print(f"\n>>> CORRELATED FLOOD EVENT <<<")
    print(f"    Dam triggered at:    {result['dam_time']}")
    print(f"    Bridge triggered at: {result['bridge_time']}")
    print(f"    Time gap:            {result['dt_seconds']}s")
    print(f"    Measured velocity:   {result['velocity_mps']} m/s\n")

    log_feed(
        f"Correlated flood event confirmed — wave travelled {NODE_DISTANCE_M} m "
        f"in {result['dt_seconds']}s ({result['velocity_mps']} m/s)",
        "corr"
    )

    correlation_log.insert(0, result)
    event_times.pop("dam_node", None)
    event_times.pop("bridge_node", None)


def broadcast_structural_check(source_node):
    print(f"\n>>> VIBRATION DETECTED at {source_node} — flagging structural check <<<\n")
    with lock:
        structural_flags["dam_node"] = time.time()
        structural_flags["bridge_node"] = time.time()


def node_state(dist):
    if dist is None:
        return "unknown"
    if dist <= DANGER_DISTANCE_CM:
        return "critical"
    if dist <= WARN_DISTANCE_CM:
        return "warning"
    return "normal"


@app.route("/data")
def data():
    with lock:
        nodes = {}
        for node, d in node_status.items():
            age = time.time() - d["last_seen"]
            vib_mag = d["vib_mag"]
            dist = d["distance"]
            g_force = round(vib_mag / 16384.0, 3) if vib_mag is not None else None
            nodes[node] = {
                "distance": dist,
                "vib_mag": vib_mag,
                "g_force": g_force,
                "online": age < 5,
                "structural_flag": node in structural_flags,
                "state": node_state(dist),
                "dist_history": list(history.get(node, {}).get("dist", [])),
                "vib_history": list(history.get(node, {}).get("vib", [])),
                "recent_trigger": (time.time() - event_times[node] < 8) if node in event_times else False
            }
        correlations = correlation_log[:10]
        feed = list(event_feed)[:25]
        latest = correlation_log[0] if correlation_log else None

    return jsonify({
        "nodes": nodes,
        "correlations": correlations,
        "feed": feed,
        "latest_correlation": latest,
        "config": {
            "danger_cm": DANGER_DISTANCE_CM,
            "warn_cm": WARN_DISTANCE_CM,
            "node_distance_m": NODE_DISTANCE_M
        }
    })


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""
<html><head><meta charset="utf-8"><title>Environmental Sensor Network</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d1117; color: #e6edf3; padding: 24px; margin: 0; }
  h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 24px; }
  h2 { font-size: 15px; font-weight: 600; color: #58d4c4; margin: 0 0 12px;
       text-transform: uppercase; letter-spacing: 0.5px; }
  .grid { display: grid; grid-template-columns: 1fr 340px; gap: 20px; align-items: start; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
          padding: 18px; margin-bottom: 20px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { border-bottom: 1px solid #30363d; padding: 9px 10px; text-align: left; }
  th { color: #8b949e; font-weight: 500; font-size: 12px; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  .bar-wrap { position: relative; height: 16px; background: #21262d;
              border-radius: 4px; overflow: hidden; width: 130px; }
  .bar { height: 100%; transition: width .4s, background .4s; }
  .danger-line { position: absolute; top: 0; bottom: 0; width: 2px; background: #f85149; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px;
          font-size: 11px; font-weight: 600; }
  .p-normal { background: #12331f; color: #5ddc8a; }
  .p-warning { background: #3d2f0c; color: #e3b341; }
  .p-critical { background: #40151a; color: #ff7b72; }
  .p-stale { background: #292929; color: #8b949e; }
  .flag { color: #e3b341; font-size: 11px; margin-left: 6px; }
  .feed { max-height: 300px; overflow-y: auto; font-size: 13px; }
  .feed-row { padding: 7px 0; border-bottom: 1px solid #21262d; display: flex; gap: 10px; }
  .feed-row:last-child { border-bottom: none; }
  .feed-time { color: #6e7681; font-variant-numeric: tabular-nums; flex-shrink: 0; }
  .k-flood { color: #79c0ff; } .k-vib { color: #e3b341; }
  .k-corr { color: #56d364; font-weight: 500; } .k-warn { color: #ff7b72; }
  .k-info { color: #8b949e; }
  details { border-top: 1px solid #30363d; padding-top: 12px; margin-top: 4px; }
  details:first-of-type { border-top: none; padding-top: 0; }
  summary { cursor: pointer; font-size: 13px; font-weight: 500; padding: 6px 0; color: #e6edf3; }
  details p { font-size: 12.5px; color: #8b949e; line-height: 1.6; margin: 6px 0 10px; }
  .hero { font-size: 13px; color: #8b949e; margin-top: 10px; }
  .hero b { color: #56d364; font-size: 17px; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  .pulsing { animation: pulse 1s ease-in-out infinite; }
</style></head>
<body>
  <h1>Environmental Sensor Network</h1>
  <div class="sub">Edge-AI flood &amp; structural monitoring &mdash; Team Synapse</div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>River schematic</h2>
        <svg id="schematic" viewBox="0 0 700 220" style="width:100%;height:auto"></svg>
        <div class="hero" id="hero">Awaiting sensor data&hellip;</div>
      </div>

      <div class="card">
        <h2>Live node status</h2>
        <table id="status-table"></table>
      </div>

      <div class="card">
        <h2>Correlated flood events</h2>
        <table id="corr-table"></table>
      </div>
    </div>

    <div>
      <div class="card">
        <h2>Event log</h2>
        <div class="feed" id="feed"></div>
      </div>

      <div class="card">
        <h2>What am I looking at?</h2>
        <details open><summary>The two sensor nodes</summary>
          <p>Each node has an ultrasonic sensor pointed down at the water and a vibration
          sensor on its mounting. The dam node sits upstream, the bridge node downstream.
          All detection runs on the microcontroller itself &mdash; no internet needed to
          sound a local alarm.</p></details>
        <details><summary>Distance reading</summary>
          <p>Distance from the sensor down to the water surface. Smaller distance means the
          water has risen closer to the sensor, so <b>lower is more dangerous</b>. The red
          line on each bar marks the danger threshold.</p></details>
        <details><summary>STA/LTA detection</summary>
          <p>Short-Term Average over Long-Term Average. Two rolling averages of the same
          signal &mdash; one fast, one slow. When the fast one spikes well above the slow
          baseline, something is changing suddenly. Borrowed from seismology, where it
          detects the arrival of an earthquake wave.</p></details>
        <details><summary>Why two triggers?</summary>
          <p>STA/LTA catches water <i>rising fast</i>. A separate absolute threshold catches
          water that is <i>already too high</i>, even if it crept up slowly. Running both
          means neither failure mode is missed.</p></details>
        <details><summary>G-force reading</summary>
          <p>The accelerometer's raw magnitude converted to g. At rest it reads about 1.000 g
          because gravity alone acts on the sensor. Deviations from 1 g mean the node is
          actually being shaken.</p></details>
        <details><summary>Flow velocity correlation</summary>
          <p>When the dam node triggers and the bridge node triggers shortly after, the gap
          between those two timestamps is how long the wave took to travel between them.
          Velocity = distance between nodes &divide; that time gap. Dam must trigger first
          &mdash; water flows downstream.</p></details>
      </div>
    </div>
  </div>

<script>
const STATE_COLOR = { normal:'#3fb950', warning:'#d29922', critical:'#f85149', unknown:'#6e7681' };

function spark(vals, color, invert) {
  if (!vals || vals.length < 2) return '<span style="color:#6e7681">&mdash;</span>';
  const w = 90, h = 22, min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const pts = vals.map((v,i) => {
    const x = (i / (vals.length - 1)) * w;
    let norm = (v - min) / range;
    if (invert) norm = 1 - norm;
    return `${x.toFixed(1)},${(h - norm * h).toFixed(1)}`;
  }).join(' ');
  return `<svg width="${w}" height="${h}" style="display:block">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"
      stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function levelBar(dist, cfg) {
  if (dist == null) return '<span style="color:#6e7681">&mdash;</span>';
  const maxScale = 60;
  const clamped = Math.min(dist, maxScale);
  const fill = (1 - clamped / maxScale) * 100;
  const st = dist <= cfg.danger_cm ? 'critical' : dist <= cfg.warn_cm ? 'warning' : 'normal';
  const dangerPos = (1 - cfg.danger_cm / maxScale) * 100;
  return `<div class="bar-wrap">
      <div class="bar" style="width:${fill.toFixed(0)}%;background:${STATE_COLOR[st]}"></div>
      <div class="danger-line" style="left:${dangerPos.toFixed(0)}%"></div>
    </div>`;
}

function drawSchematic(nodes, latest) {
  const svg = document.getElementById('schematic');
  const dam = nodes.dam_node, bridge = nodes.bridge_node;
  const dc = STATE_COLOR[dam ? dam.state : 'unknown'];
  const bc = STATE_COLOR[bridge ? bridge.state : 'unknown'];
  const damPulse = dam && dam.recent_trigger ? 'class="pulsing"' : '';
  const brPulse = bridge && bridge.recent_trigger ? 'class="pulsing"' : '';

  let wave = '';
  if (latest) {
    const dur = Math.max(0.6, Math.min(latest.dt_seconds, 6));
    wave = `<circle r="7" fill="#58a6ff" opacity="0.9">
      <animateMotion dur="${dur}s" repeatCount="indefinite"
        path="M 150 150 L 560 150"/></circle>`;
  }

  svg.innerHTML = `
    <rect x="0" y="130" width="700" height="60" fill="#0f2233"/>
    <path d="M0 132 Q 175 126 350 132 T 700 132" stroke="#1f6feb" stroke-width="2" fill="none" opacity=".7"/>
    <rect x="90" y="60" width="60" height="110" fill="#30363d" rx="3"/>
    <text x="120" y="52" fill="#8b949e" font-size="12" text-anchor="middle">Dam</text>
    <rect x="500" y="112" width="160" height="10" fill="#30363d" rx="2"/>
    <rect x="516" y="122" width="10" height="26" fill="#30363d"/>
    <rect x="634" y="122" width="10" height="26" fill="#30363d"/>
    <text x="580" y="104" fill="#8b949e" font-size="12" text-anchor="middle">Bridge</text>
    ${wave}
    <circle cx="150" cy="150" r="9" fill="${dc}" ${damPulse}/>
    <text x="150" y="182" fill="${dc}" font-size="11.5" text-anchor="middle">dam_node</text>
    <text x="150" y="196" fill="#6e7681" font-size="10.5" text-anchor="middle">${dam && dam.distance != null ? dam.distance.toFixed(1)+' cm' : '—'}</text>
    <circle cx="560" cy="150" r="9" fill="${bc}" ${brPulse}/>
    <text x="560" y="182" fill="${bc}" font-size="11.5" text-anchor="middle">bridge_node</text>
    <text x="560" y="196" fill="#6e7681" font-size="10.5" text-anchor="middle">${bridge && bridge.distance != null ? bridge.distance.toFixed(1)+' cm' : '—'}</text>
    <line x1="150" y1="212" x2="560" y2="212" stroke="#30363d" stroke-width="1"/>
    <text x="355" y="208" fill="#6e7681" font-size="10.5" text-anchor="middle">flow direction &rarr;</text>`;
}

async function refresh() {
  try {
    const res = await fetch('/data');
    const d = await res.json();
    const cfg = d.config;

    drawSchematic(d.nodes, d.latest_correlation);

    const hero = document.getElementById('hero');
    if (d.latest_correlation) {
      const c = d.latest_correlation;
      hero.innerHTML = `Last confirmed flood wave travelled ${cfg.node_distance_m} m in
        ${c.dt_seconds}s &mdash; measured flow velocity <b>${c.velocity_mps} m/s</b>`;
    } else {
      hero.textContent = 'Monitoring. No correlated flood event yet — trigger dam_node first, then bridge_node.';
    }

    let rows = `<tr><th>Node</th><th>Water level</th><th>Distance</th><th>Trend</th>
      <th>G-force</th><th>Vibration</th><th>State</th></tr>`;
    const order = ['dam_node','bridge_node'];
    const keys = Object.keys(d.nodes).sort((a,b) => order.indexOf(a) - order.indexOf(b));
    for (const node of keys) {
      const v = d.nodes[node];
      const st = v.online ? v.state : 'stale';
      const flag = v.structural_flag ? '<span class="flag">STRUCTURAL CHECK</span>' : '';
      rows += `<tr>
        <td>${node}${flag}</td>
        <td>${levelBar(v.distance, cfg)}</td>
        <td>${v.distance != null ? v.distance.toFixed(1)+' cm' : '—'}</td>
        <td>${spark(v.dist_history, '#58a6ff', true)}</td>
        <td>${v.g_force != null ? v.g_force.toFixed(3)+' g' : '—'}</td>
        <td>${spark(v.vib_history, '#e3b341', false)}</td>
        <td><span class="pill p-${st}">${st}</span></td>
      </tr>`;
    }
    if (keys.length === 0) rows += `<tr><td colspan="7" style="color:#6e7681">No nodes reporting yet…</td></tr>`;
    document.getElementById('status-table').innerHTML = rows;

    let cr = `<tr><th>Detected</th><th>Dam trigger</th><th>Bridge trigger</th>
      <th>Time gap</th><th>Flow velocity</th></tr>`;
    for (const r of d.correlations) {
      cr += `<tr><td>${r.time}</td><td>${r.dam_time}</td><td>${r.bridge_time}</td>
        <td>${r.dt_seconds}s</td><td style="color:#56d364">${r.velocity_mps} m/s</td></tr>`;
    }
    if (d.correlations.length === 0)
      cr += `<tr><td colspan="5" style="color:#6e7681">No correlated events yet…</td></tr>`;
    document.getElementById('corr-table').innerHTML = cr;

    let fd = '';
    for (const e of d.feed) {
      fd += `<div class="feed-row"><span class="feed-time">${e.time}</span>
        <span class="k-${e.kind}">${e.text}</span></div>`;
    }
    document.getElementById('feed').innerHTML = fd ||
      '<div style="color:#6e7681;font-size:13px">Nothing logged yet…</div>';
  } catch(e) { console.error(e); }
}
refresh();
setInterval(refresh, 1000);
</script>
</body></html>
"""


def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[Gateway] Attempting to connect to {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)

    print("[Gateway] Starting loop_forever()...")
    client.loop_forever()