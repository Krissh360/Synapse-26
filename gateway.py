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
SCALE_MAX_CM = 120.0
HISTORY_LEN = 120

lock = threading.RLock()
node_status = {}
event_times = {}
correlation_log = []
structural_flags = {}
event_feed = deque(maxlen=60)
history = {}
stats = {}
session_start = time.time()

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
                    "vib": deque(maxlen=HISTORY_LEN),
                    "t": deque(maxlen=HISTORY_LEN)
                }
            if node_id not in stats:
                stats[node_id] = {
                    "messages": 0, "events": 0,
                    "min_dist": None, "max_dist": None, "peak_g": None
                }

            s = stats[node_id]
            s["messages"] += 1

            if dist is not None:
                history[node_id]["dist"].append(round(dist, 2))
                history[node_id]["t"].append(now)
                s["min_dist"] = dist if s["min_dist"] is None else min(s["min_dist"], dist)
                s["max_dist"] = dist if s["max_dist"] is None else max(s["max_dist"], dist)
            if vib is not None:
                g = vib / 16384.0
                history[node_id]["vib"].append(round(g, 4))
                s["peak_g"] = g if s["peak_g"] is None else max(s["peak_g"], g)

            if not known:
                log_feed(f"{node_id} came online and started reporting", "info")

        elif msg_type == "event":
            flood = payload.get("flood", False)
            vibration = payload.get("vibration", False)
            print(f"\n[EVENT] {node_id} -> flood={flood}, vibration={vibration}")

            if node_id in stats:
                stats[node_id]["events"] += 1

            if flood:
                d = node_status.get(node_id, {}).get("distance")
                dtxt = f", water at {d:.1f} cm" if d is not None else ""
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
        log_feed("Bridge fired before dam — water flows downstream, so trigger dam first", "warn")
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
        f"Flood wave confirmed across both nodes — {NODE_DISTANCE_M} m "
        f"in {result['dt_seconds']} s, {result['velocity_mps']} m/s",
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
            s = stats.get(node, {})
            nodes[node] = {
                "distance": dist,
                "vib_mag": vib_mag,
                "g_force": g_force,
                "online": age < 5,
                "structural_flag": node in structural_flags,
                "state": node_state(dist),
                "dist_history": list(history.get(node, {}).get("dist", [])),
                "vib_history": list(history.get(node, {}).get("vib", [])),
                "recent_trigger": (time.time() - event_times[node] < 8) if node in event_times else False,
                "messages": s.get("messages", 0),
                "events": s.get("events", 0),
                "min_dist": round(s["min_dist"], 1) if s.get("min_dist") is not None else None,
                "max_dist": round(s["max_dist"], 1) if s.get("max_dist") is not None else None,
                "peak_g": round(s["peak_g"], 3) if s.get("peak_g") is not None else None,
            }
        correlations = correlation_log[:10]
        feed = list(event_feed)[:30]
        latest = correlation_log[0] if correlation_log else None
        peak_v = max((c["velocity_mps"] for c in correlation_log), default=None)

    return jsonify({
        "nodes": nodes,
        "correlations": correlations,
        "feed": feed,
        "latest_correlation": latest,
        "session": {
            "uptime_s": int(time.time() - session_start),
            "total_correlations": len(correlation_log),
            "peak_velocity": peak_v
        },
        "config": {
            "danger_cm": DANGER_DISTANCE_CM,
            "warn_cm": WARN_DISTANCE_CM,
            "scale_max_cm": SCALE_MAX_CM,
            "node_distance_m": NODE_DISTANCE_M
        }
    })


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Environmental Sensor Network</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --abyss:#f3f2ed; --panel:#fcfbf7; --panel-2:#eeeee7; --rule:#dedfd6;
    --ink:#272e2b; --ink-dim:#535e57; --ink-faint:#68736b;
    --water:#326c80; --safe:#357052; --watch:#91621e; --crit:#b64439;
    --schematic-bg:#f6f6f0; --schematic-land:#e9ece2; --schematic-water:#bed5d8;
    --schematic-structure:#bcc6b6; --schematic-structure-2:#879985; --schematic-deck:#edf0e7;
    --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
  }
  :root[data-theme="dark"]{
    --abyss:#181b19; --panel:#20241f; --panel-2:#272c26; --rule:#343a35;
    --ink:#e4e6e0; --ink-dim:#aab1a7; --ink-faint:#7c8780;
    --water:#5fa8c2; --safe:#5fb583; --watch:#d1943f; --crit:#e17767;
    --schematic-bg:#20241f; --schematic-land:#272c26; --schematic-water:#2f4a52;
    --schematic-structure:#3a4238; --schematic-structure-2:#4a5346; --schematic-deck:#2a2f28;
  }
  *{box-sizing:border-box}
  html,body{margin:0;overflow-x:hidden}
  body{background:var(--abyss);color:var(--ink);font-family:var(--sans);
    font-size:14px;line-height:1.5;padding:24px clamp(16px,2.5vw,40px) 32px;
    -webkit-font-smoothing:antialiased}
  .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
  .top{max-width:1600px;margin:0 auto 18px;display:flex;align-items:center;
    gap:8px 20px;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid var(--rule)}
  .title{font-size:20px;font-weight:600;letter-spacing:-.025em}
  .byline{color:var(--ink-faint);font-size:12px}
  .live{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--ink-dim);font-size:12px}
  .theme-toggle{border:1px solid var(--rule);background:var(--panel);color:var(--ink-dim);
    font-family:var(--sans);font-size:11px;padding:5px 10px;border-radius:6px;cursor:pointer}
  .theme-toggle:hover{color:var(--ink)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--safe);flex-shrink:0}
  .dot.off{background:var(--ink-faint)}
  .bento{max-width:1600px;margin:auto;display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr)) minmax(280px,.72fr);
    grid-template-areas:"hero hero rail" "nodes nodes rail" "water vibration rail" "totals totals rail" "waves waves waves";
    gap:12px;align-items:stretch}
  .panel{min-width:0;background:var(--panel);border:1px solid var(--rule);border-radius:12px;padding:18px 20px}
  .hero{grid-area:hero}
  .nodes{grid-area:nodes}.water-chart{grid-area:water}.vibration-chart{grid-area:vibration}
  .totals{grid-area:totals}.waves{grid-area:waves;overflow-x:auto}
  .rail{grid-area:rail;display:flex;flex-direction:column;gap:12px;min-height:0;contain:size}
  .rail .grow{display:flex;flex-direction:column;min-height:0;height:0;flex:1;overflow:hidden}
  h2{font-size:12px;font-weight:500;color:var(--ink-dim);margin:0 0 14px;letter-spacing:.025em}
  #schematic{background:var(--schematic-bg);border-radius:6px}
  .readout{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px;margin-top:14px;padding-top:14px;border-top:1px solid var(--rule)}
  .readout .big{font-family:var(--mono);font-size:clamp(22px,2.2vw,32px);font-weight:500;color:var(--water);line-height:1.2}
  .readout .cap{color:var(--ink-faint);font-size:12px}
  .readout .note{margin-left:auto;color:var(--ink-faint);font-size:11px;text-align:right}
  .strip{display:flex;flex-direction:column}
  .nrow{display:grid;grid-template-columns:minmax(150px,1fr) repeat(2,minmax(70px,.45fr)) 74px;
    gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--rule)}
  .nrow:first-child{padding-top:0}.nrow:last-child{border-bottom:none;padding-bottom:0}
  .nname{font-weight:500;display:flex;align-items:center;flex-wrap:wrap;gap:7px;font-size:13px}
  .swatch{width:7px;height:7px;border-radius:2px;flex-shrink:0}
  .metric{text-align:right;min-width:0}
  .metric .v{font-family:var(--mono);font-size:18px;font-variant-numeric:tabular-nums}
  .metric .l{color:var(--ink-faint);font-size:10px;margin-top:2px}
  .chip{display:inline-block;font-size:10px;padding:3px 7px;border-radius:4px;font-weight:500}
  .c-normal{background:#e6eee5;color:var(--safe)}
  .c-warning{background:#f2ead8;color:var(--watch)}
  .c-critical{background:#f5e5df;color:var(--crit)}
  .c-stale,.c-unknown{background:#ebece5;color:var(--ink-faint)}
  .tag{font-size:10px;color:var(--watch)}
  .feed{flex:1;overflow-y:auto;padding-right:5px;min-height:0;height:0;scrollbar-width:thin;scrollbar-color:var(--rule) transparent}
  .frow{display:grid;grid-template-columns:58px minmax(0,1fr);gap:10px;padding:11px 0;border-bottom:1px solid var(--rule);font-size:12px;line-height:1.55;overflow-wrap:anywhere}
  .frow:first-child{padding-top:0}.frow:last-child{border-bottom:none}
  .ftime{font-family:var(--mono);color:var(--ink-faint);font-size:10px;padding-top:2px}
  .k-flood{color:var(--water)}.k-vib{color:var(--watch)}
  .k-corr{color:var(--safe);font-weight:500}.k-warn{color:var(--crit)}.k-info{color:var(--ink-dim)}
  details{border-bottom:1px solid var(--rule)}details:last-child{border-bottom:none}
  summary{cursor:pointer;font-size:12px;padding:9px 0;list-style:none;display:flex;align-items:center;gap:8px}
  summary::-webkit-details-marker{display:none}
  summary::before{content:"+";color:var(--ink-faint);font-family:var(--mono);font-size:13px;width:10px;flex-shrink:0}
  details[open] summary::before{content:"−"}
  details p{font-size:12px;color:var(--ink-dim);line-height:1.65;margin:0 0 12px 18px;max-width:64ch}
  summary:focus-visible{outline:2px solid var(--water);outline-offset:3px}
  table{border-collapse:collapse;width:100%;font-size:12px;min-width:540px}
  th{text-align:left;font-weight:500;color:var(--ink-faint);font-size:11px;padding:0 12px 10px 0;border-bottom:1px solid var(--rule)}
  td{padding:12px 12px 12px 0;border-bottom:1px solid var(--rule)}tr:last-child td{border-bottom:none}
  .empty{color:var(--ink-faint);font-size:12px;padding:14px 0}
  .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
  .stat{padding:0 0 0 16px;border-left:1px solid var(--rule)}.stat:first-child{padding-left:0;border:0}
  .stat .v{font-family:var(--mono);font-size:22px;font-variant-numeric:tabular-nums}
  .stat .l{color:var(--ink-faint);font-size:10px;margin-top:3px}
  @media(max-width:1100px){
    .bento{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-areas:"hero hero" "nodes nodes" "water vibration" "totals totals" "rail rail" "waves waves"}
    .rail{display:grid;contain:none;grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:auto;align-items:start}
    .rail .grow{height:360px}.byline{order:3;width:100%}
  }
  @media(max-width:640px){
    body{padding:16px 12px 24px}.top{gap:8px;padding-bottom:14px;margin-bottom:12px}
    .title{font-size:18px}.live{margin-left:0;width:100%;order:4}
    .bento{grid-template-columns:minmax(0,1fr);grid-template-areas:"hero" "nodes" "water" "vibration" "totals" "rail" "waves"}
    .panel{padding:15px}.rail{grid-template-columns:minmax(0,1fr)}
    .readout .note{width:100%;text-align:left;margin:0}
    .nrow{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;padding:14px 0}
    .nname{grid-column:1 / 3}.nrow>div:last-child{grid-column:3;grid-row:1}
    .metric{text-align:left;grid-row:2}
    .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
    .stat:nth-child(3){padding-left:0;border:0}
  }
  @media(prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head>
<body>

<div class="top">
  <span class="title">Environmental Sensor Network</span>
  <span class="byline">Flood &amp; structural early warning · Team Synapse</span>
  <span class="live"><span class="dot" id="livedot"></span><span id="livetxt">connecting</span></span>
  <button class="theme-toggle" id="themeToggle" type="button">dark mode</button>
</div>

<div class="bento">
  <div class="panel hero">
    <h2>River cross-section, live</h2>
    <svg id="schematic" viewBox="0 64 900 266" style="width:100%;height:auto;display:block"></svg>
    <div class="readout">
      <span class="big" id="vbig">—</span>
      <span class="cap" id="vcap">no flood wave measured yet</span>
      <span class="note" id="vnote"></span>
    </div>
  </div>

  <div class="rail">
    <div class="panel grow">
      <h2>Event log</h2>
      <div class="feed" id="feed"></div>
    </div>
    <div class="panel">
      <h2>Reference</h2>
      <div id="ref"></div>
    </div>
  </div>

  <div class="panel nodes">
    <h2>Node readings</h2>
    <div class="strip" id="strip"></div>
  </div>

  <div class="panel water-chart">
    <h2>Water level, last 2 minutes</h2>
    <svg id="chart-dist" viewBox="0 0 440 150" style="width:100%;height:auto;display:block"></svg>
  </div>

  <div class="panel vibration-chart">
    <h2>Vibration, last 2 minutes</h2>
    <svg id="chart-vib" viewBox="0 0 440 150" style="width:100%;height:auto;display:block"></svg>
  </div>

  <div class="panel totals">
    <h2>Session totals</h2>
    <div class="stats" id="stats"></div>
  </div>

  <div class="panel waves">
    <h2>Confirmed flood waves</h2>
    <table id="corr-table"></table>
  </div>
</div>

<script>
function stateColors(){
  const cs=getComputedStyle(document.documentElement);
  const v=n=>cs.getPropertyValue(n).trim();
  return {normal:v('--safe'),warning:v('--watch'),critical:v('--crit'),unknown:v('--ink-faint'),stale:v('--ink-faint')};
}
const REF = [
  ["How the two nodes work",
   "Each node points an ultrasonic sensor down at the water and carries a vibration sensor on its mount. The dam node sits upstream, the bridge node downstream. Detection runs on the microcontroller itself, so a node still sounds its local alarm with the network down."],
  ["Reading the distance",
   "Distance is measured from the sensor down to the water surface, so a smaller number means the water has risen closer to the sensor. The marked line on the channel is the danger depth."],
  ["STA/LTA detection",
   "Two rolling averages of the same signal, one fast and one slow. When the fast average climbs well above the slow baseline, something is changing suddenly. The method comes from seismology, where it picks the arrival of an earthquake wave."],
  ["Why two triggers run together",
   "STA/LTA catches water rising fast. A separate fixed threshold catches water that is already too high, even if it crept up slowly. Running both means neither case slips through."],
  ["The g-force column",
   "Raw accelerometer magnitude converted to g. At rest it reads close to 1.000 g because gravity alone acts on the sensor, so departures from 1 g mean the node is genuinely being shaken."],
  ["How flow velocity is measured",
   "When the dam node fires and the bridge node fires shortly after, the gap between those timestamps is the wave's travel time. Velocity is the distance between nodes divided by that gap. The dam has to fire first, because water flows downstream."]
];

function fmtUp(s){
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+"h ":"")+(h||m?m+"m ":"")+x+"s";
}

/* ---------- hero schematic ---------- */
function waterY(dist, cfg, top, bottom){
  if(dist==null) return bottom-6;
  const f=Math.max(0,Math.min(1,dist/cfg.scale_max_cm));
  return top+f*(bottom-top);
}

function drawSchematic(nodes,latest,cfg){
  const C=stateColors();
  const dam=nodes.dam_node, br=nodes.bridge_node;
  const dc=C[dam?(dam.online?dam.state:'stale'):'unknown'];
  const bc=C[br?(br.online?br.state:'stale'):'unknown'];

  // zones: scale 118 | channel 135-770 | right bank 770-900
  const TOP=165, BOT=290, X0=116, X1=765;
  const dY=waterY(dam?dam.distance:null,cfg,TOP,BOT);
  const bY=waterY(br?br.distance:null,cfg,TOP,BOT);
  const dangerY=waterY(cfg.danger_cm,cfg,TOP,BOT);

  const surface=`M ${X0} ${dY.toFixed(1)} C 330 ${(dY-2).toFixed(1)}, 520 ${(bY-2).toFixed(1)}, ${X1} ${bY.toFixed(1)}`;

  const dur=latest?Math.max(.9,Math.min(latest.dt_seconds,6)):0;
  const wave = latest ? `
    <g opacity=".9">
      <circle r="6" fill="#326c80">
        <animateMotion dur="${dur}s" repeatCount="indefinite"
          path="M 195 ${(dY-9).toFixed(0)} L 683 ${(bY-9).toFixed(0)}"/></circle>
      <circle r="14" fill="none" stroke="#326c80" stroke-width="1.4" opacity=".4">
        <animateMotion dur="${dur}s" repeatCount="indefinite"
          path="M 195 ${(dY-9).toFixed(0)} L 683 ${(bY-9).toFixed(0)}"/></circle>
    </g>` : '';

  const blink = on => on ? `<animate attributeName="opacity" values="1;.25;1" dur=".9s" repeatCount="indefinite"/>` : '';

  let ticks='';
  for(let cm=0; cm<=cfg.scale_max_cm; cm+=30){
    const y=waterY(cm,cfg,TOP,BOT);
    ticks+=`<line x1="${X1}" y1="${y.toFixed(1)}" x2="${X1+9}" y2="${y.toFixed(1)}" stroke="#9da99d" stroke-width="1"/>
            <text x="${X1+14}" y="${(y+3.5).toFixed(1)}" fill="var(--ink-faint)" font-size="9.5"
              font-family="IBM Plex Mono, monospace">${cm}</text>`;
  }

  document.getElementById('schematic').innerHTML = `

  <rect x="0" y="${BOT}" width="900" height="${340-BOT}" fill="var(--schematic-land)"/>
  <path d="M0 ${BOT} L0 236 Q 30 228 62 234 L 62 ${BOT} Z" fill="var(--schematic-land)"/>
  <rect x="${X1}" y="${TOP-18}" width="${900-X1}" height="${BOT-TOP+18}" fill="var(--schematic-land)"/>

  <rect x="0" y="196" width="62" height="${BOT-196}" fill="var(--schematic-water)"/>
  <line x1="0" y1="196" x2="62" y2="196" stroke="#568c9b" stroke-width="1.6" opacity=".7"/>
  <text x="31" y="188" fill="var(--ink-faint)" font-size="9.5" text-anchor="middle">reservoir</text>

  <path d="M62 152 L 104 152 L 112 ${BOT} L 62 ${BOT} Z" fill="var(--schematic-structure)"/>
  <rect x="62" y="152" width="42" height="6" fill="var(--schematic-structure-2)"/>
  <rect x="74" y="158" width="20" height="40" fill="var(--schematic-deck)"/>
  <path d="M74 198 Q 86 236 110 276" stroke="#568c9b" stroke-width="2.2" fill="none" opacity=".45"/>
  <text x="83" y="144" fill="var(--ink-dim)" font-size="12" text-anchor="middle">Dam</text>

  <rect x="${X0}" y="${TOP}" width="${X1-X0}" height="${BOT-TOP}" fill="var(--schematic-deck)"/>
  ${ticks}
  <text x="${X1+14}" y="${TOP-16}" fill="var(--ink-faint)" font-size="9.5">depth cm</text>
  <path d="${surface} L ${X1} ${BOT} L ${X0} ${BOT} Z" fill="var(--schematic-water)"/>
  <path d="${surface}" stroke="#568c9b" stroke-width="2" fill="none"/>
  <line x1="${X0}" y1="${dangerY.toFixed(1)}" x2="${X1}" y2="${dangerY.toFixed(1)}"
        stroke="#b64439" stroke-width="1" stroke-dasharray="5 5" opacity=".6"/>
  <text x="738" y="${(dangerY-5).toFixed(1)}" fill="#b64439" font-size="10" text-anchor="end">danger depth ${cfg.danger_cm} cm</text>
  ${wave}

  <rect x="560" y="112" width="310" height="4" rx="1" fill="var(--schematic-structure)"/>
  <rect x="560" y="116" width="310" height="10" rx="1.5" fill="var(--schematic-structure-2)"/>
  <rect x="600" y="126" width="9" height="${BOT-126}" fill="var(--schematic-structure)"/>
  <rect x="745" y="126" width="9" height="${BOT-126}" fill="var(--schematic-structure)"/>
  <rect x="845" y="126" width="9" height="${BOT-126}" fill="var(--schematic-structure)" opacity=".7"/>
  <text x="715" y="104" fill="var(--ink-dim)" font-size="12" text-anchor="middle">Bridge</text>

  <g>
    <rect x="186" y="108" width="7" height="34" fill="#bcc6b6"/>
    <rect x="171" y="86" width="37" height="23" rx="3.5" fill="#fcfbf7" stroke="${dc}" stroke-width="1.6"/>
    <rect x="180" y="95" width="19" height="5" rx="1" fill="${dc}">${blink(dam&&dam.recent_trigger)}</rect>
    <text x="189" y="78" fill="${dc}" font-size="11.5" text-anchor="middle">dam_node</text>
    <line x1="189" y1="142" x2="189" y2="${(dY-5).toFixed(1)}" stroke="${dc}"
      stroke-width="1" stroke-dasharray="3 4" opacity=".8"/>
    <line x1="180" y1="${(dY-3).toFixed(1)}" x2="198" y2="${(dY-3).toFixed(1)}" stroke="${dc}" stroke-width="1.6"/>
    <text x="213" y="100" fill="${dc}" font-size="11"
      font-family="IBM Plex Mono, monospace">${dam&&dam.distance!=null?dam.distance.toFixed(1)+' cm':'—'}</text>
  </g>

  <g>
    <rect x="665" y="126" width="37" height="23" rx="3.5" fill="#fcfbf7" stroke="${bc}" stroke-width="1.6"/>
    <rect x="674" y="135" width="19" height="5" rx="1" fill="${bc}">${blink(br&&br.recent_trigger)}</rect>
    <text x="657" y="134" fill="${bc}" font-size="11.5" text-anchor="end">bridge_node</text>
    <text x="657" y="148" fill="${bc}" font-size="11" text-anchor="end"
      font-family="IBM Plex Mono, monospace">${br&&br.distance!=null?br.distance.toFixed(1)+' cm':'—'}</text>
    <line x1="683" y1="149" x2="683" y2="${(bY-5).toFixed(1)}" stroke="${bc}"
      stroke-width="1" stroke-dasharray="3 4" opacity=".8"/>
    <line x1="674" y1="${(bY-3).toFixed(1)}" x2="692" y2="${(bY-3).toFixed(1)}" stroke="${bc}" stroke-width="1.6"/>
  </g>

  <line x1="189" y1="316" x2="683" y2="316" stroke="#9da99d" stroke-width="1"/>
  <line x1="189" y1="311" x2="189" y2="321" stroke="#9da99d" stroke-width="1"/>
  <line x1="683" y1="311" x2="683" y2="321" stroke="#9da99d" stroke-width="1"/>
  <rect x="343" y="308" width="186" height="16" fill="var(--schematic-land)"/>
  <text x="436" y="320" fill="var(--ink-faint)" font-size="10.5" text-anchor="middle">
    ${cfg.node_distance_m} m apart · flow downstream →</text>`;
}

/* ---------- line chart ---------- */
function lineChart(el,series,opts){
  const W=440,H=150,L=38,R=10,T=12,B=22;
  const all=series.flatMap(s=>s.data);
  if(all.length<2){
    el.innerHTML=`<text x="${W/2}" y="${H/2}" fill="#68736b" font-size="12"
      text-anchor="middle">waiting for readings…</text>`;return;
  }
  let min=Math.min(...all),max=Math.max(...all);
  if(opts.pad){const p=(max-min)*.15||opts.pad;min-=p;max+=p;}
  if(opts.floor0) min=Math.min(min,0);
  const rng=(max-min)||1;
  const x=(i,n)=>L+(i/Math.max(1,n-1))*(W-L-R);
  const y=v=>T+(1-(v-min)/rng)*(H-T-B);

  let grid='',labels='';
  for(let k=0;k<=3;k++){
    const v=min+(rng*k/3), yy=y(v);
    grid+=`<line x1="${L}" y1="${yy.toFixed(1)}" x2="${W-R}" y2="${yy.toFixed(1)}"
      stroke="#dedfd6" stroke-width="1"/>`;
    labels+=`<text x="${L-6}" y="${(yy+3.5).toFixed(1)}" fill="#68736b" font-size="9.5"
      text-anchor="end" font-family="IBM Plex Mono, monospace">${v.toFixed(opts.dp)}</text>`;
  }

  let paths='',legend='';
  series.forEach((s,si)=>{
    if(s.data.length<2) return;
    const pts=s.data.map((v,i)=>`${x(i,s.data.length).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    paths+=`<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="1.8"
              stroke-linejoin="round" stroke-linecap="round"/>`;
    const last=s.data[s.data.length-1];
    paths+=`<circle cx="${x(s.data.length-1,s.data.length).toFixed(1)}"
              cy="${y(last).toFixed(1)}" r="2.8" fill="${s.color}"/>`;
    legend+=`<g transform="translate(${L+si*104},${H-6})">
      <rect width="8" height="2.5" y="-3" rx="1" fill="${s.color}"/>
      <text x="13" y="0" fill="#535e57" font-size="10.5">${s.name}</text></g>`;
  });

  let marker='';
  if(opts.markAt!=null){
    const yy=y(opts.markAt);
    if(yy>T&&yy<H-B) marker=`<line x1="${L}" y1="${yy.toFixed(1)}" x2="${W-R}"
      y2="${yy.toFixed(1)}" stroke="#b64439" stroke-width="1" stroke-dasharray="4 4" opacity=".6"/>`;
  }
  el.innerHTML=grid+marker+paths+labels+legend;
}

/* ---------- main refresh ---------- */
async function refresh(){
  try{
    const res=await fetch('/data');
    const d=await res.json();
    const cfg=d.config, nodes=d.nodes;
    const order=['dam_node','bridge_node'];
    const keys=Object.keys(nodes).sort((a,b)=>order.indexOf(a)-order.indexOf(b));
    const onlineCount=keys.filter(k=>nodes[k].online).length;

    document.getElementById('livedot').className='dot'+(onlineCount?'':' off');
    document.getElementById('livetxt').textContent=
      `${onlineCount} of ${keys.length||2} nodes reporting · ${fmtUp(d.session.uptime_s)}`;

    drawSchematic(nodes,d.latest_correlation,cfg);

    const c=d.latest_correlation;
    document.getElementById('vbig').textContent = c ? c.velocity_mps+' m/s' : '—';
    document.getElementById('vcap').textContent = c
      ? `across ${c.distance_m} m in ${c.dt_seconds} s`
      : 'no flood wave measured yet';
    document.getElementById('vnote').textContent = c
      ? `dam ${c.dam_time} → bridge ${c.bridge_time}`
      : 'trigger the dam node first, then the bridge node';

    /* node strip */
    const C=stateColors();
    let strip='';
    for(const n of keys){
      const v=nodes[n], st=v.online?v.state:'stale';
      strip+=`<div class="nrow">
        <div class="nname"><span class="swatch" style="background:${C[st]}"></span>${n}
          ${v.structural_flag?'<span class="tag">inspect structure</span>':''}</div>
        <div class="metric"><div class="v">${v.distance!=null?v.distance.toFixed(1):'—'}</div>
          <div class="l">cm to water</div></div>
        <div class="metric"><div class="v">${v.g_force!=null?v.g_force.toFixed(3):'—'}</div>
          <div class="l">g-force</div></div>
        <div style="text-align:right"><span class="chip c-${st}">${st}</span></div>
      </div>`;
    }
    document.getElementById('strip').innerHTML = strip ||
      '<div class="empty">No nodes reporting. Check the broker and both boards.</div>';

    /* charts */
    lineChart(document.getElementById('chart-dist'),[
      {name:'dam_node',color:'#326c80',data:nodes.dam_node?nodes.dam_node.dist_history:[]},
      {name:'bridge_node',color:'#357052',data:nodes.bridge_node?nodes.bridge_node.dist_history:[]}
    ],{dp:0,pad:2,markAt:cfg.danger_cm});

    lineChart(document.getElementById('chart-vib'),[
      {name:'dam_node',color:'#91621e',data:nodes.dam_node?nodes.dam_node.vib_history:[]},
      {name:'bridge_node',color:'#80638c',data:nodes.bridge_node?nodes.bridge_node.vib_history:[]}
    ],{dp:3,pad:.01});

    /* session totals */
    const totalMsg=keys.reduce((a,k)=>a+nodes[k].messages,0);
    const totalEv=keys.reduce((a,k)=>a+nodes[k].events,0);
    const minD=keys.map(k=>nodes[k].min_dist).filter(v=>v!=null);
    const pg=keys.map(k=>nodes[k].peak_g).filter(v=>v!=null);
    document.getElementById('stats').innerHTML=`
      <div class="stat"><div class="v">${totalMsg.toLocaleString()}</div>
        <div class="l">readings received</div></div>
      <div class="stat"><div class="v">${totalEv}</div>
        <div class="l">node alerts fired</div></div>
      <div class="stat"><div class="v">${d.session.peak_velocity!=null?d.session.peak_velocity:'—'}</div>
        <div class="l">fastest wave, m/s</div></div>
      <div class="stat"><div class="v">${minD.length?Math.min(...minD).toFixed(1):'—'}</div>
        <div class="l">closest water, cm</div></div>`;

    /* correlations */
    let ct=`<tr><th>Confirmed</th><th>Dam fired</th><th>Bridge fired</th>
      <th>Travel time</th><th>Flow velocity</th></tr>`;
    for(const r of d.correlations){
      ct+=`<tr><td class="num">${r.time}</td><td class="num">${r.dam_time}</td>
        <td class="num">${r.bridge_time}</td><td class="num">${r.dt_seconds} s</td>
        <td class="num" style="color:#357052">${r.velocity_mps} m/s</td></tr>`;
    }
    document.getElementById('corr-table').innerHTML = d.correlations.length ? ct :
      ct+`<tr><td colspan="5" class="empty">Nothing confirmed yet. Trigger the dam node, then the bridge node within a minute.</td></tr>`;

    /* feed */
    let fd='';
    for(const e of d.feed){
      fd+=`<div class="frow"><span class="ftime">${e.time}</span>
        <span class="k-${e.kind}">${e.text}</span></div>`;
    }
    document.getElementById('feed').innerHTML = fd ||
      '<div class="empty">Nothing logged yet.</div>';
  }catch(e){console.error(e);}
}

/* ---------- theme toggle ---------- */
(function(){
  const btn=document.getElementById('themeToggle');
  function apply(theme){
    document.documentElement.setAttribute('data-theme',theme);
    btn.textContent = theme==='dark' ? 'light mode' : 'dark mode';
  }
  let saved='light';
  try{ saved=localStorage.getItem('theme')||'light'; }catch(e){}
  apply(saved);
  btn.addEventListener('click',()=>{
    const next=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
    apply(next);
    try{ localStorage.setItem('theme',next); }catch(e){}
  });
})();

document.getElementById('ref').innerHTML = REF.map((r,i)=>
  `<details${i===0?' open':''}><summary>${r[0]}</summary><p>${r[1]}</p></details>`).join('');
refresh();
setInterval(refresh,1000);
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