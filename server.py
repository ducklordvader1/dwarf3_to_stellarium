#!/usr/bin/env python3
"""
dwarf32stellarium — standalone Stellarium telescope protocol bridge for the Dwarf3

Runs two services in one process:
  • Flask HTTP server (default port 5002) — status dashboard + REST API
  • TCP server (default port 10001) — speaks the Stellarium telescope protocol

Connects directly to the Dwarf3 WebSocket (port 9900) using the DwarfLab SDK
from the parent directory.  No separate dwarf_web server is required.

Usage
-----
  python3 server.py [192.168.88.1] [--tcp-port 10001] [--port 5002]

  Positional argument (optional): Dwarf3 IP address (default: 192.168.88.1)

Stellarium setup
----------------
  1. Plugins → Telescope Control → enable → restart Stellarium
  2. F2 → Plugins → Telescope Control → Configure
  3. Add a new telescope
  4. "Telescope controlled by" → External software or remote computer
  5. TCP port → 10001 (or whatever --tcp-port you chose), host → localhost
  6. Connect
  7. Right-click any sky object → Slew telescope to

Stellarium telescope protocol (all little-endian)
--------------------------------------------------
  GoTo packet (Stellarium → this server, 20 bytes):
    uint16  length   = 20
    uint16  type     = 0
    int64   client_usec
    uint32  ra_raw               (J2000 RA,  full circle = 2^32)
    int32   dec_raw              (J2000 Dec, ±90° = ±2^30)

  Current-position packet (this server → Stellarium, 24 bytes):
    uint16  length   = 24
    uint16  type     = 0
    int64   server_usec
    uint32  ra_raw
    int32   dec_raw
    int32   status               (0 = OK)

  Coordinate mapping:
    ra_raw  = int( ra_radians  * 2^31 / π ) & 0xFFFFFFFF   (unsigned)
    dec_raw = int( dec_radians * 2^31 / π )                (signed)
"""

import argparse
import logging
import math
import os
import secrets
import socket
import struct
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

from dwarflab_controller import DwarfLab

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("dwarf32stellarium")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# ── runtime config (populated in main()) ──────────────────────────────────────
cfg = {
    "dwarf_ip":   "192.168.88.1",
    "tcp_port":   10001,
    "tcp_host":   "127.0.0.1",  # Stellarium runs on the same host
    "http_port":  5002,
    "http_host":  "127.0.0.1",
    "lat":        None,   # observer latitude  (degrees, None = don't send)
    "lon":        None,   # observer longitude (degrees, None = don't send)
    "alt":        0.0,    # observer altitude  (metres)
}

# ── DwarfLab instance (set in _connect_telescope()) ───────────────────────────
_dwarf: DwarfLab = None
_dwarf_lock = threading.Lock()

# ── bridge state: only fields not already in dwarf.state ──────────────────────
bridge = {
    # Last commanded pointing position (J2000 degrees).
    # Reported back to Stellarium at 10 Hz.
    "ra_deg":         0.0,
    "dec_deg":        0.0,
    # Active Stellarium TCP client count
    "clients":        0,
    # Last target name from a goto command
    "last_goto_name": "",
    # Unix timestamp of last goto (None if never)
    "last_goto_time": None,
}
_bridge_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Telescope connection
# ─────────────────────────────────────────────────────────────────────────────

def _on_telescope_notify(pkt):
    """Called by DwarfLab on every incoming WebSocket notification.
    DwarfLab already updates self.state; nothing extra needed here.
    """
    pass


def _connect_telescope():
    """Background thread: create a DwarfLab instance and connect."""
    global _dwarf
    ip = cfg["dwarf_ip"]
    log.info("Connecting to Dwarf3 at %s …", ip)
    dwarf = DwarfLab(host=ip, on_notify=_on_telescope_notify)
    ok = dwarf.connect(timeout=10.0)
    with _dwarf_lock:
        _dwarf = dwarf
    if ok:
        log.info("Dwarf3 connected")
        dwarf.sync_time()
        dwarf.get_device_state()
        if cfg["lat"] is not None and cfg["lon"] is not None:
            dwarf.set_location(cfg["lat"], cfg["lon"], cfg["alt"])
            log.info("Location sent: lat=%.5f lon=%.5f alt=%.0fm", cfg["lat"], cfg["lon"], cfg["alt"])
        else:
            log.warning("No GPS location configured — goto accuracy may be reduced (use --lat/--lon)")
    else:
        log.warning("Dwarf3 not reachable — will keep retrying in the background")


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate conversion helpers
# ─────────────────────────────────────────────────────────────────────────────

_SCALE = 0x80000000 / math.pi   # 2^31 / π


def _ra_deg_to_raw(ra: float) -> int:
    """RA degrees [0, 360) → unsigned int32 Stellarium wire value."""
    return int(math.radians(ra % 360.0) * _SCALE) & 0xFFFFFFFF


def _dec_deg_to_raw(dec: float) -> int:
    """Dec degrees [-90, 90] → signed int32 Stellarium wire value."""
    return int(math.radians(max(-90.0, min(90.0, dec))) * _SCALE)


def _raw_to_ra_deg(raw: int) -> float:
    """Unsigned int32 Stellarium wire value → RA degrees [0, 360)."""
    return math.degrees(raw / _SCALE) % 360.0


def _raw_to_dec_deg(raw_signed: int) -> float:
    """Signed int32 Stellarium wire value → Dec degrees [-90, 90]."""
    return math.degrees(raw_signed / _SCALE)


# ─────────────────────────────────────────────────────────────────────────────
# Stellarium packet builders / parsers
# ─────────────────────────────────────────────────────────────────────────────

def _build_position_packet(ra: float, dec: float, status: int = 0) -> bytes:
    """Build a 24-byte Stellarium current-position packet."""
    ts = int(time.time() * 1_000_000)
    return struct.pack(
        "<HHqIii",   # length, type, timestamp, RA (uint32), Dec (int32), status (int32)
        24, 0,
        ts,
        _ra_deg_to_raw(ra),
        _dec_deg_to_raw(dec),
        status,
    )


def _parse_goto_body(body: bytes):
    """Parse the 16-byte body of a Stellarium GoTo packet (after the 4-byte header).
    Returns (ra_deg, dec_deg) J2000.
    """
    ra_raw  = struct.unpack_from("<I", body, 8)[0]   # uint32 at offset 8
    dec_raw = struct.unpack_from("<i", body, 12)[0]  # int32  at offset 12
    return _raw_to_ra_deg(ra_raw), _raw_to_dec_deg(dec_raw)


# ─────────────────────────────────────────────────────────────────────────────
# GoTo command handler
# ─────────────────────────────────────────────────────────────────────────────

def _do_goto(ra_deg: float, dec_deg: float, name: str = "Stellarium target"):
    """Send a GoTo command directly to the Dwarf3 via the DwarfLab SDK."""
    with _dwarf_lock:
        dwarf = _dwarf
    if dwarf is None or not dwarf.state.get("connected"):
        log.warning("GOTO ignored — telescope not connected (RA=%.4f Dec=%.4f)", ra_deg, dec_deg)
        return
    log.info("GOTO → RA %.4f°  Dec %.4f°  (%s)", ra_deg, dec_deg, name)
    dwarf.one_click_goto_dso(ra_deg, dec_deg, name)
    with _bridge_lock:
        bridge["ra_deg"]         = ra_deg
        bridge["dec_deg"]        = dec_deg
        bridge["last_goto_name"] = name
        bridge["last_goto_time"] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Stellarium TCP server
# ─────────────────────────────────────────────────────────────────────────────

def _send_position_loop(conn: socket.socket, stop: threading.Event):
    """Broadcast the current telescope position at 10 Hz to one Stellarium client."""
    while not stop.is_set():
        with _bridge_lock:
            ra  = bridge["ra_deg"]
            dec = bridge["dec_deg"]
        try:
            conn.sendall(_build_position_packet(ra, dec))
        except OSError:
            stop.set()
            return
        time.sleep(0.1)


def _recv_loop(conn: socket.socket, stop: threading.Event):
    """Receive and dispatch GoTo commands from one Stellarium client."""
    conn.settimeout(1.0)
    buf = b""
    while not stop.is_set():
        try:
            chunk = conn.recv(256)
            if not chunk:
                stop.set()
                return
            buf += chunk
        except socket.timeout:
            continue
        except OSError:
            stop.set()
            return

        # Process all complete packets in the buffer
        while len(buf) >= 4:
            pkt_len, pkt_type = struct.unpack_from("<HH", buf, 0)
            if pkt_len < 4:
                # Malformed length — would cause infinite loop; close connection
                log.warning("Malformed Stellarium packet (len=%d), closing connection", pkt_len)
                stop.set()
                return
            if len(buf) < pkt_len:
                break  # wait for more data
            body = buf[4:pkt_len]
            buf  = buf[pkt_len:]

            if pkt_type == 0:
                if len(body) >= 16:
                    ra_deg, dec_deg = _parse_goto_body(body)
                    log.info("Stellarium GOTO: RA=%.4f°  Dec=%.4f°", ra_deg, dec_deg)
                    threading.Thread(
                        target=_do_goto,
                        args=(ra_deg, dec_deg),
                        daemon=True,
                    ).start()
                else:
                    log.debug("Stellarium type-0 packet too short (body=%d bytes)", len(body))
            else:
                log.debug("Unknown Stellarium packet type=%d len=%d", pkt_type, pkt_len)


def _handle_stellarium_client(conn: socket.socket, addr):
    log.info("Stellarium client connected: %s", addr)
    with _bridge_lock:
        bridge["clients"] += 1

    stop = threading.Event()
    sender = threading.Thread(
        target=_send_position_loop, args=(conn, stop), daemon=True, name="stell-send"
    )
    sender.start()
    try:
        _recv_loop(conn, stop)
    finally:
        stop.set()
        sender.join(timeout=2)
        conn.close()
        with _bridge_lock:
            bridge["clients"] -= 1
        log.info("Stellarium client disconnected: %s", addr)


def _run_tcp_server():
    host = cfg["tcp_host"]
    port = cfg["tcp_port"]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    log.info("Stellarium TCP server listening on %s:%d", host, port)
    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(
                target=_handle_stellarium_client,
                args=(conn, addr),
                daemon=True,
                name=f"stell-client-{addr[1]}",
            ).start()
        except Exception as exc:
            log.error("TCP accept error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(_HTML_UI, mimetype="text/html")


@app.route("/api/status")
def api_status():
    with _dwarf_lock:
        dwarf = _dwarf
    telescope_state = dict(dwarf.state) if dwarf else {}
    with _bridge_lock:
        b = dict(bridge)
    return jsonify({
        "telescope": telescope_state,
        "bridge":    b,
        "config":    dict(cfg),
    })


@app.route("/api/goto", methods=["POST"])
def api_goto():
    """Manual GoTo endpoint — useful for testing without Stellarium.
    Body: {"ra": <degrees>, "dec": <degrees>, "name": "<optional label>"}
    """
    body = request.get_json(force=True, silent=True) or {}
    ra  = body.get("ra")
    dec = body.get("dec")
    if ra is None or dec is None:
        return jsonify({"status": "error", "message": "ra and dec (J2000 degrees) are required"}), 400
    name = body.get("name", "Manual target")
    threading.Thread(
        target=_do_goto, args=(float(ra), float(dec), name), daemon=True
    ).start()
    return jsonify({"status": "ok", "message": f"GOTO RA={ra} Dec={dec} commanded"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Abort any active GoTo."""
    with _dwarf_lock:
        dwarf = _dwarf
    if dwarf is None or not dwarf.state.get("connected"):
        return jsonify({"status": "error", "message": "Telescope not connected"}), 400
    dwarf.stop_goto()
    return jsonify({"status": "ok", "message": "GoTo aborted"})


@app.route("/api/track/start", methods=["POST"])
def api_track_start():
    with _dwarf_lock:
        dwarf = _dwarf
    if not dwarf or not dwarf.state.get("connected"):
        return jsonify({"status": "error", "message": "Telescope not connected"}), 400
    dwarf.start_tracking()
    return jsonify({"status": "ok", "message": "Tracking started"})


@app.route("/api/location", methods=["POST"])
def api_location():
    """Set observer GPS location and push it to the telescope immediately.
    Body: {"lat": <degrees>, "lon": <degrees>, "alt": <metres, optional>}
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"status": "error", "message": "lat and lon (decimal degrees) are required"}), 400
    alt = float(body.get("alt", 0.0))
    cfg["lat"] = lat
    cfg["lon"] = lon
    cfg["alt"] = alt
    with _dwarf_lock:
        dwarf = _dwarf
    if dwarf and dwarf.state.get("connected"):
        dwarf.set_location(lat, lon, alt)
        log.info("Location updated: lat=%.5f lon=%.5f alt=%.0fm", lat, lon, alt)
        return jsonify({"status": "ok", "message": f"Location set and sent: {lat}, {lon}, {alt}m"})
    return jsonify({"status": "ok", "message": f"Location saved (telescope not connected yet): {lat}, {lon}, {alt}m"})


@app.route("/api/track/stop", methods=["POST"])
def api_track_stop():
    with _dwarf_lock:
        dwarf = _dwarf
    if not dwarf or not dwarf.state.get("connected"):
        return jsonify({"status": "error", "message": "Telescope not connected"}), 400
    dwarf.stop_tracking()
    return jsonify({"status": "ok", "message": "Tracking stopped"})


# ─────────────────────────────────────────────────────────────────────────────
# Inline web UI
# ─────────────────────────────────────────────────────────────────────────────

_HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dwarf3 ↔ Stellarium Bridge</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#c9d1d9;font:14px/1.6 ui-monospace,monospace}
  .hdr{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:12px}
  .hdr h1{color:#58a6ff;font-size:18px;letter-spacing:.5px;flex:1}
  .hdr p{color:#8b949e;font-size:12px}
  .main{padding:20px 24px;max-width:960px}
  .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
  .card h2{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  .row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #21262d}
  .row:last-child{border:none}
  .lbl{color:#8b949e;font-size:12px}
  .val{color:#e6edf3;font-size:12px;text-align:right}
  .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700}
  .g{background:#1a3d2b;color:#3fb950}.y{background:#3d2e0e;color:#d29922}
  .r{background:#3d1212;color:#f85149}.b{background:#0d2044;color:#58a6ff}
  .neutral{background:#21262d;color:#8b949e}
  .instr{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:12px}
  .instr h2{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  ol{padding-left:18px}li{margin-bottom:5px;font-size:13px}
  code{background:#0d1117;border:1px solid #30363d;border-radius:3px;padding:0 5px;color:#79c0ff;font-size:12px}
  footer{color:#484f58;font-size:11px;margin-top:14px}
</style>
</head>
<body>
<div class="hdr">
  <h1>Dwarf3 ↔ Stellarium Bridge</h1>
  <p id="hdr-ip">—</p>
</div>
<div class="main">

  <div class="grid">
    <div class="card">
      <h2>Telescope</h2>
      <div class="row"><span class="lbl">Connection</span><span id="tel-conn"  class="val">—</span></div>
      <div class="row"><span class="lbl">Battery</span>   <span id="tel-batt"  class="val">—</span></div>
      <div class="row"><span class="lbl">Temperature</span><span id="tel-temp" class="val">—</span></div>
      <div class="row"><span class="lbl">CMOS Temp</span> <span id="tel-cmos" class="val">—</span></div>
    </div>
    <div class="card">
      <h2>Mount State</h2>
      <div class="row"><span class="lbl">GoTo State</span> <span id="goto-state" class="val">—</span></div>
      <div class="row"><span class="lbl">Tracking</span>   <span id="tracking"   class="val">—</span></div>
      <div class="row"><span class="lbl">Stacking</span>   <span id="stacking"   class="val">—</span></div>
      <div class="row"><span class="lbl">Focus Pos</span>  <span id="focus-pos"  class="val">—</span></div>
    </div>
    <div class="card">
      <h2>Stellarium Link</h2>
      <div class="row"><span class="lbl">TCP Port</span>    <span id="tcp-port" class="val">—</span></div>
      <div class="row"><span class="lbl">Clients</span>     <span id="clients"  class="val">—</span></div>
      <div class="row"><span class="lbl">Last Target</span> <span id="last-name" class="val">—</span></div>
      <div class="row"><span class="lbl">Last GoTo</span>   <span id="last-time" class="val">—</span></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:12px">
    <h2>Current Pointing (J2000)</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:4px">
      <div class="row"><span class="lbl">RA</span> <span id="ra" class="val">—</span></div>
      <div class="row"><span class="lbl">Dec</span><span id="dec" class="val">—</span></div>
    </div>
  </div>

  <div class="instr">
    <h2>Stellarium Setup</h2>
    <ol>
      <li>Open Stellarium → <strong>Plugins</strong> tab → enable <strong>Telescope Control</strong> → restart Stellarium</li>
      <li>Press <code>F2</code> → <strong>Plugins</strong> → <em>Telescope Control</em> → click <strong>Configure</strong></li>
      <li>Click <strong>Add a new telescope</strong></li>
      <li>Set <em>"Telescope controlled by"</em> → <code>External software or remote computer</code></li>
      <li>Set <em>TCP port</em> to <code id="tcp-port-instr">10001</code> and leave host as <code>localhost</code></li>
      <li>Click <strong>Connect</strong> — the status dot turns green when connected</li>
      <li>Right-click any sky object → <strong>Slew telescope to</strong></li>
    </ol>
  </div>

  <footer>Status refreshes every 2 s &nbsp;|&nbsp; Position broadcast to Stellarium at 10 Hz</footer>
</div>

<script>
const GS_LABEL = {0:"Idle", 1:"Running", 2:"Stopping", 3:"Stopped", 4:"Plate Solving"};
const GS_CLS   = {0:"neutral", 1:"y", 2:"y", 3:"g", 4:"b"};

function hms(deg) {
  const h=deg/15, hh=Math.floor(h), m=(h-hh)*60, mm=Math.floor(m), ss=((m-mm)*60).toFixed(1);
  return String(hh).padStart(2,'0')+'h '+String(mm).padStart(2,'0')+'m '+ss.padStart(4,'0')+'s';
}
function dms(deg) {
  const s=deg<0?'-':'+', a=Math.abs(deg), dd=Math.floor(a),
        m=(a-dd)*60, mm=Math.floor(m), ss=((m-mm)*60).toFixed(0);
  return s+String(dd).padStart(2,'0')+'° '+String(mm).padStart(2,'0')+"' "+String(ss).padStart(2,'0')+'"';
}
function badge(cls,txt){return '<span class="badge '+cls+'">'+txt+'</span>';}
function yesno(v){return v ? badge('g','Yes') : badge('neutral','No');}
function fmt(v, unit){ return v===null||v===undefined ? '—' : v+unit; }
function elapsed(ts){
  if(!ts) return '—';
  const s=Math.floor(Date.now()/1000-ts);
  if(s<60) return s+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  return Math.floor(s/3600)+'h ago';
}

async function refresh() {
  try {
    const {telescope:t, bridge:b, config:c} = await (await fetch('/api/status')).json();
    document.getElementById('hdr-ip').textContent = 'Dwarf3 @ '+c.dwarf_ip;

    // Telescope card
    document.getElementById('tel-conn').innerHTML =
      t.connected ? badge('g','CONNECTED') : badge('r','DISCONNECTED');
    document.getElementById('tel-batt').textContent  = fmt(t.battery,'%');
    document.getElementById('tel-temp').textContent  = fmt(t.temperature,' °C');
    document.getElementById('tel-cmos').textContent  = fmt(t.cmos_temp,' °C');

    // Mount state card
    const gs = t.goto_state ?? 0;
    document.getElementById('goto-state').innerHTML = badge(GS_CLS[gs]||'neutral', GS_LABEL[gs]||gs);
    document.getElementById('tracking').innerHTML   = yesno(t.tracking);
    document.getElementById('stacking').innerHTML   = yesno(t.stacking);
    document.getElementById('focus-pos').textContent = fmt(t.focus_position,'');

    // Stellarium link card
    document.getElementById('tcp-port').textContent      = c.tcp_port;
    document.getElementById('tcp-port-instr').textContent = c.tcp_port;
    document.getElementById('clients').innerHTML =
      b.clients>0 ? badge('g', b.clients+' connected') : badge('neutral','0');
    document.getElementById('last-name').textContent = b.last_goto_name || '—';
    document.getElementById('last-time').textContent  = elapsed(b.last_goto_time);

    // Pointing
    document.getElementById('ra').textContent  = hms(b.ra_deg)  + '  ('+b.ra_deg.toFixed(4)+'°)';
    document.getElementById('dec').textContent = dms(b.dec_deg) + '  ('+b.dec_deg.toFixed(4)+'°)';
  } catch(e) { console.error(e); }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Dwarf3 ↔ Stellarium standalone telescope protocol bridge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "dwarf_ip", nargs="?", default="192.168.88.1",
        metavar="DWARF_IP",
        help="IP address of the Dwarf3 telescope",
    )
    parser.add_argument(
        "--tcp-port", type=int, default=10001,
        metavar="PORT",
        help="TCP port for the Stellarium telescope protocol server",
    )
    parser.add_argument(
        "--port", type=int, default=5002,
        metavar="PORT",
        help="HTTP port for this Flask status dashboard",
    )
    parser.add_argument(
        "--bind", default="127.0.0.1",
        metavar="HOST",
        help="Host/IP for the Flask dashboard (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    parser.add_argument(
        "--tcp-bind", default="127.0.0.1",
        metavar="HOST",
        help="Host/IP for the Stellarium TCP server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--lat", type=float, default=None,
        metavar="DEG",
        help="Observer latitude in decimal degrees (e.g. 48.8566)",
    )
    parser.add_argument(
        "--lon", type=float, default=None,
        metavar="DEG",
        help="Observer longitude in decimal degrees (e.g. 2.3522)",
    )
    parser.add_argument(
        "--alt", type=float, default=0.0,
        metavar="M",
        help="Observer altitude in metres above sea level (default: 0)",
    )
    args = parser.parse_args()

    cfg["dwarf_ip"]  = args.dwarf_ip
    cfg["tcp_port"]  = args.tcp_port
    cfg["tcp_host"]  = args.tcp_bind
    cfg["http_port"] = args.port
    cfg["http_host"] = args.bind
    cfg["lat"]       = args.lat
    cfg["lon"]       = args.lon
    cfg["alt"]       = args.alt

    log.info("Dwarf3 IP      : %s", cfg["dwarf_ip"])
    log.info("Stellarium TCP : port %d", cfg["tcp_port"])
    log.info("Flask HTTP     : port %d", cfg["http_port"])
    if cfg["lat"] is not None:
        log.info("Observer loc   : lat=%.5f lon=%.5f alt=%.0fm", cfg["lat"], cfg["lon"], cfg["alt"])
    else:
        log.info("Observer loc   : not set (use --lat/--lon)")

    # Start background threads
    threading.Thread(target=_connect_telescope, daemon=True, name="dwarf-connect").start()
    threading.Thread(target=_run_tcp_server,    daemon=True, name="stell-tcp").start()

    # Run Flask (use_reloader=False keeps background threads alive)
    app.run(host=cfg["http_host"], port=cfg["http_port"], threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
