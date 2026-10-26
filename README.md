# dwarf32stellarium

A standalone bridge server that lets [Stellarium](https://stellarium.org) control the **Dwarf3** smart telescope via the Stellarium Telescope Control plugin.

```
Stellarium  ──TCP 10001──►  dwarf32stellarium  ──WebSocket 9900──►  Dwarf3
```

## How it works

The bridge speaks two protocols simultaneously:

- **Stellarium Telescope Protocol** (TCP, port 10001) — receives GoTo commands from Stellarium and streams the current pointing position back at 10 Hz
- **DwarfLab WebSocket Protocol** (port 9900) — sends one-click goto commands directly to the Dwarf3 firmware

When Stellarium sends a slew command, the bridge triggers a one-click goto on the Dwarf3 which automatically plate-solves the current sky, slews to the target, and begins tracking.

A lightweight **status dashboard** (Flask, port 5002) shows live telescope state and pointing coordinates.

> **Security:** This bridge has no authentication, authorization, or
> transport encryption of any kind — neither the Stellarium TCP port, the
> status dashboard, nor the REST API. Run it only on an internal, trusted
> network — never expose it directly to the public internet.

## Requirements

- Python 3.8+
- Dwarf3 connected to the same network as the machine running this bridge

```bash
pip install flask requests websocket-client
```

## Usage

```bash
python3 server.py [DWARF3_IP] [--lat LAT] [--lon LON] [--alt ALT] [--tcp-port PORT] [--port PORT]
```

| Argument | Default | Description |
|---|---|---|
| `DWARF3_IP` | `192.168.88.1` | IP address of the Dwarf3 |
| `--lat` | *(none)* | Observer latitude in decimal degrees |
| `--lon` | *(none)* | Observer longitude in decimal degrees |
| `--alt` | `0` | Observer altitude in metres above sea level |
| `--tcp-port` | `10001` | TCP port for the Stellarium telescope protocol |
| `--port` | `5002` | HTTP port for the status dashboard |

**Example:**

```bash
python3 server.py 192.168.88.1 --lat 48.8566 --lon 2.3522 --alt 35
```

> Setting `--lat` / `--lon` is recommended. The bridge sends your location to the Dwarf3 on connect, which improves goto accuracy.

### Helper scripts

Convenience start/stop wrappers are provided for both platforms. They run the
bridge detached and log to a temp file; any extra arguments are forwarded to
`server.py`.

**Linux / macOS**

```bash
./start_bridge.sh 192.168.88.1 --lat 48.8566 --lon 2.3522 --alt 35
./stop_bridge.sh
```

**Windows**

```bat
start_bridge.bat 192.168.88.1 --lat 48.8566 --lon 2.3522 --alt 35
stop_bridge.bat
```

## Stellarium setup

1. **Enable the plugin** *(first time only)*
   - Open Stellarium → wrench icon → **Plugins** tab
   - Find **Telescope Control** → tick **Load at startup** → click **Configure**
   - Restart Stellarium

2. **Add the telescope**
   - Press `F2` → **Plugins** tab → **Telescope Control** → **Configure**
   - Click **Add a new telescope**
   - Set **Name** to anything, e.g. `Dwarf3`
   - Set **Telescope controlled by** → `External software or remote computer`
   - Set **Host** to `localhost` (or the IP of the machine running this bridge)
   - Set **TCP port** to `10001`
   - Click **OK**

3. **Connect**
   - Select `Dwarf3` in the Telescope Control window → click **Connect**
   - The status indicator turns green when the bridge accepts the connection

4. **Slew the telescope**
   - Click any object in the sky
   - Press `Ctrl+1` (or right-click → **Slew telescope to** → `Dwarf3`)
   - The bridge logs `GOTO → RA … Dec …` and the Dwarf3 begins its goto sequence

> **Note:** The first goto after power-on includes a plate-solve to calibrate pointing. The telescope must be outside with a clear view of the sky for this to succeed.

## Status dashboard

Open `http://localhost:5002` in a browser for a live view of telescope state, pointing coordinates, and Stellarium connection status.

![Dashboard shows telescope connection, battery, temperature, goto state, tracking, and current RA/Dec]

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Full telescope + bridge state as JSON |
| `POST` | `/api/goto` | Manual goto — body: `{"ra": <deg>, "dec": <deg>, "name": "<label>"}` |
| `POST` | `/api/stop` | Abort active goto |
| `POST` | `/api/location` | Update observer location — body: `{"lat": <deg>, "lon": <deg>, "alt": <m>}` |
| `POST` | `/api/track/start` | Start tracking |
| `POST` | `/api/track/stop` | Stop tracking |

**Example — manual goto (M42 Orion Nebula):**

```bash
curl -X POST http://localhost:5002/api/goto \
  -H 'Content-Type: application/json' \
  -d '{"ra": 83.82, "dec": -5.39, "name": "M42"}'
```

**Example — update location at runtime:**

```bash
curl -X POST http://localhost:5002/api/location \
  -H 'Content-Type: application/json' \
  -d '{"lat": 48.8566, "lon": 2.3522, "alt": 35}'
```

## Files

```
dwarf32stellarium/
├── server.py               # bridge server (Stellarium TCP + Flask HTTP)
├── dwarflab_controller.py  # DwarfLab WebSocket SDK (shared with dwarf_web)
├── requirements.txt        # Python dependencies
├── start_bridge.sh         # start wrapper (Linux/macOS)
├── stop_bridge.sh          # stop wrapper  (Linux/macOS)
├── start_bridge.bat        # start wrapper (Windows)
└── stop_bridge.bat         # stop wrapper  (Windows)
```

## Controller / protocol

`dwarflab_controller.py` is the shared DwarfLab SDK, kept in sync with the
`dwarf_web` project. It targets the **October 2026** DWARF 3 firmware
generation. The bridge uses only a stable subset of the SDK
(`connect`, `sync_time`, `get_device_state`, `set_location`,
`one_click_goto_dso`, `stop_goto`, `start_tracking`, `stop_tracking`); these
command numbers and signatures are unchanged across recent firmware updates,
so refreshing the controller does not require bridge changes.

When updating `dwarflab_controller.py`, copy the current version from
`../dwarf_web/` so both projects stay on the same protocol map.

## Coordinate system

All coordinates are **J2000 RA/Dec in decimal degrees**:
- RA: 0–360°
- Dec: −90° to +90°

Stellarium sends coordinates in its own binary-encoded format; the bridge converts them automatically.
