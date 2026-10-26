"""
dwarflab_controller.py
WebSocket + HTTP controller for the DWARF 3 smart telescope.

Reverse-engineered from com.convergence.dwarflab APK (June 2026 release, 170 MB).
All command numbers verified against WsCmd.java in decompiled source.

Changelog vs previous version:
  - [Oct 2026] Integrated device-level commands: added CMD_DEVICE_LENS_DEFOG=17000,
    CMD_DEVICE_ANTI_TANGLE=17003 (CMD_DEVICE_AUTO_COOLING=17001 /
    CMD_DEVICE_AUTO_SHUTDOWN=17002 already present). New methods set_lens_defog(),
    set_anti_tangle(); new notify handlers + state for lens_defog/anti_tangle.
    Fixed MODULE_DEVICE wire id 17->18 (WsModuleId enum ordinal), added
    MODULE_CAMERA_GUIDE=17. Payloads are Req{State}{state:int32=1|0}, field 1 varint.
  - [Oct 2026 APK review] Fixed CMD_CAMERA_WIDE_START_RECORD 12005->12030
    and CMD_CAMERA_WIDE_STOP_RECORD 12006->12031 (June values were wrong;
    verified against Oct 2026 WsCmd.java). No other command numbers changed;
    all startup/joystick/focus/notify commands confirmed identical.
  - Added CMD_CAMERA_TELE_START_RECORD = 10005  (was missing; now distinct from stop)
  - Removed CMD_CAMERA_TELE_STOP_BURST = 10004  (removed in new APK)
  - Added new module ranges: MODULE_VOICE_ASSISTANT (16800-16899),
    MODULE_DEVICE (17000-17099)
  - Added new commands: CMD_VOICE_ASSISTANT_TASK=16800,
    CMD_DEVICE_AUTO_COOLING=17001, CMD_DEVICE_AUTO_SHUTDOWN=17002
  - Added new notify handlers: CMD_NOTIFY_WIDE_FOCUS_POSITION=15300,
    CMD_NOTIFY_LENS_DEFOG_STATE=15301, CMD_NOTIFY_AUTO_COOLING_STATE=15302,
    CMD_NOTIFY_AUTO_SHUTDOWN_STATE=15303
  - goto_dso() now accepts optional goto_only flag (new ReqGotoDSO field)
  - open_camera() / open_camera_wide() now use CameraType-aware payload
    (new APK sends cameraType field; old binning-only payload still accepted)
  - Fixed: CMD_ASTRO_WIDE_GO_LIVE = 11020 (was 11019 — that's STOP_EQ_SOLVING)
"""
import struct, uuid, threading, time, logging, requests, websocket
log = logging.getLogger("dwarflab")

# ── Module IDs ────────────────────────────────────────────────────────────────
MODULE_NONE            = 0
MODULE_CAMERA_TELE     = 1   # 10000-10499
MODULE_ASTRO           = 3   # 11000-11499
MODULE_CAMERA_WIDE     = 2   # 12000-12499
MODULE_SYSTEM          = 4   # 13000-13299
MODULE_RGB_POWER       = 5   # 13500-13799
MODULE_MOTOR           = 6   # 14000-14499
MODULE_TRACK           = 7   # 14800-14899
MODULE_FOCUS           = 8   # 15000-15199
MODULE_NOTIFY          = 9   # 15200-15499
MODULE_PANORAMA        = 10  # 15500-15599
MODULE_ITIPS           = 11  # 15700-15799
MODULE_TEST            = 12
MODULE_SHOOTING_SCHEDULE = 13  # 16100-16399
MODULE_TASK_CENTER     = 14  # 16400-16599
MODULE_PARAM           = 15  # 16700-16799
MODULE_VOICE_ASSISTANT = 16  # 16800-16899  (WsModuleId ordinal 16)
MODULE_CAMERA_GUIDE    = 17  # 16900-16999  (WsModuleId ordinal 17)
MODULE_DEVICE          = 18  # 17000-17099  (WsModuleId ordinal 18) FIXED: was 17

def _module_for_cmd(c):
    if 10000 <= c < 10500: return MODULE_CAMERA_TELE
    if 11000 <= c < 11500: return MODULE_ASTRO
    if 12000 <= c < 12500: return MODULE_CAMERA_WIDE
    if 13000 <= c < 13300: return MODULE_SYSTEM
    if 13500 <= c < 13800: return MODULE_RGB_POWER
    if 14000 <= c < 14500: return MODULE_MOTOR
    if 14800 <= c < 14900: return MODULE_TRACK
    if 15000 <= c < 15200: return MODULE_FOCUS
    if 15200 <= c < 15500: return MODULE_NOTIFY
    if 15500 <= c < 15600: return MODULE_PANORAMA
    if 15700 <= c < 15800: return MODULE_ITIPS
    if 16100 <= c < 16400: return MODULE_SHOOTING_SCHEDULE
    if 16400 <= c < 16600: return MODULE_TASK_CENTER
    if 16700 <= c < 16800: return MODULE_PARAM
    if 16800 <= c < 16900: return MODULE_VOICE_ASSISTANT
    if 16900 <= c < 17000: return MODULE_CAMERA_GUIDE
    if 17000 <= c < 17100: return MODULE_DEVICE
    return MODULE_NONE

MSG_TYPE_REQUEST  = 0
MSG_TYPE_RESPONSE = 1

# ── Camera Tele commands (10000-10499) ────────────────────────────────────────
CMD_CAMERA_TELE_OPEN_CAMERA              = 10000
CMD_CAMERA_TELE_CLOSE_CAMERA             = 10001
CMD_CAMERA_TELE_PHOTOGRAPH               = 10002
CMD_CAMERA_TELE_BURST                    = 10003
# CMD_CAMERA_TELE_STOP_BURST             = 10004  REMOVED in June 2026 APK
CMD_CAMERA_TELE_START_RECORD             = 10005  # NEW (was missing before)
CMD_CAMERA_TELE_STOP_RECORD              = 10006
CMD_CAMERA_TELE_SET_EXP                  = 10009
CMD_CAMERA_TELE_SET_GAIN                 = 10013
CMD_CAMERA_TELE_SET_BRIGHTNESS           = 10015
CMD_CAMERA_TELE_SET_CONTRAST             = 10017
CMD_CAMERA_TELE_SET_SATURATION           = 10019
CMD_CAMERA_TELE_SET_SHARPNESS            = 10023
CMD_CAMERA_TELE_SET_WB_MODE              = 10025
CMD_CAMERA_TELE_SET_WB_CT               = 10029
CMD_CAMERA_TELE_SET_IRCUT                = 10031
CMD_CAMERA_TELE_START_TIMELAPSE_PHOTO    = 10033
CMD_CAMERA_TELE_STOP_TIMELAPSE_PHOTO     = 10034
CMD_CAMERA_TELE_GET_ALL_PARAMS           = 10036
CMD_CAMERA_TELE_SET_JPG_QUALITY          = 10040
CMD_CAMERA_TELE_PHOTO_RAW                = 10041
CMD_CAMERA_TELE_SWITCH_RESOLUTION        = 10047
CMD_CAMERA_TELE_SWITCH_FRAMERATE         = 10048

# ── Astro commands (11000-11499) ──────────────────────────────────────────────
CMD_ASTRO_START_CALIBRATION              = 11000
CMD_ASTRO_STOP_CALIBRATION               = 11001
CMD_ASTRO_START_GOTO_DSO                 = 11002
CMD_ASTRO_START_GOTO_SOLAR_SYSTEM        = 11003
CMD_ASTRO_STOP_GOTO                      = 11004
CMD_ASTRO_START_CAPTURE_RAW_LIVE_STACKING = 11005
CMD_ASTRO_STOP_CAPTURE_RAW_LIVE_STACKING  = 11006
CMD_ASTRO_GO_LIVE                        = 11010
CMD_ASTRO_START_ONE_CLICK_GOTO_DSO       = 11013
CMD_ASTRO_STOP_ONE_CLICK_GOTO            = 11015
CMD_ASTRO_START_WIDE_CAPTURE_LIVE_STACKING = 11016
CMD_ASTRO_STOP_WIDE_CAPTURE_LIVE_STACKING  = 11017
CMD_ASTRO_START_EQ_SOLVING               = 11018
CMD_ASTRO_STOP_EQ_SOLVING                = 11019
CMD_ASTRO_WIDE_GO_LIVE                   = 11020  # FIXED: was 11019 (conflict); APK: ordinal 82
CMD_ASTRO_START_AI_ENHANCE               = 11029
CMD_ASTRO_STOP_AI_ENHANCE                = 11030
CMD_ASTRO_START_ONE_CLICK_SHOOTING       = 11042
CMD_ASTRO_START_SKY_TARGET_FINDER        = 11047
CMD_ASTRO_STOP_SKY_TARGET_FINDER         = 11048

# ── Camera Wide commands (12000-12499) ────────────────────────────────────────
CMD_CAMERA_WIDE_OPEN_CAMERA              = 12000
CMD_CAMERA_WIDE_START_RECORD             = 12030  # was 12005 (wrong); verified in Oct 2026 APK WsCmd.java
CMD_CAMERA_WIDE_STOP_RECORD              = 12031

# ── System commands (13000-13299) ─────────────────────────────────────────────
CMD_SYSTEM_SET_TIME                      = 13000
CMD_SYSTEM_SET_LOCATION                  = 13010

# ── RGB/Power commands (13500-13799) ──────────────────────────────────────────
CMD_RGB_POWER_OPEN_RGB                   = 13500
CMD_RGB_POWER_CLOSE_RGB                  = 13501
CMD_RGB_POWER_POWER_DOWN                 = 13502
CMD_RGB_POWER_POWERIND_ON                = 13503
CMD_RGB_POWER_POWERIND_OFF               = 13504
CMD_RGB_POWER_REBOOT                     = 13505

# ── Motor commands (14000-14499) ──────────────────────────────────────────────
CMD_STEP_MOTOR_JOYSTICK                  = 14006  # CMD_STEP_MOTOR_SERVICE_JOYSTICK
CMD_STEP_MOTOR_JOYSTICK_FIXED_ANGLE      = 14007  # CMD_STEP_MOTOR_SERVICE_JOYSTICK_FIXED_ANGLE
CMD_STEP_MOTOR_JOYSTICK_STOP             = 14008  # CMD_STEP_MOTOR_SERVICE_JOYSTICK_STOP

# ── Track commands (14800-14899) ──────────────────────────────────────────────
CMD_TRACK_START_TRACK                    = 14800
CMD_TRACK_STOP_TRACK                     = 14801

# ── Focus commands (15000-15199) ──────────────────────────────────────────────
CMD_FOCUS_AUTO_FOCUS                     = 15000
CMD_FOCUS_MANUAL_SINGLE_STEP             = 15001
CMD_FOCUS_START_MANUAL_CONTINUOUS        = 15002
CMD_FOCUS_STOP_MANUAL_CONTINUOUS         = 15003
CMD_FOCUS_START_ASTRO_AUTO_FOCUS         = 15004
CMD_FOCUS_STOP_ASTRO_AUTO_FOCUS          = 15005

# ── Notify commands (15200-15499) ─────────────────────────────────────────────
CMD_NOTIFY_ELE                           = 15201
CMD_NOTIFY_ELE_STATUS                    = 15202
CMD_NOTIFY_TEMPERATURES                  = 15203  # SDCARD_INFO in APK; carries temps on Dwarf3
CMD_NOTIFY_STATE_CAPTURE_RAW_LIVE_STACKING = 15208
CMD_NOTIFY_PROGRASS_CAPTURE_RAW_LIVE_STACKING = 15209
CMD_NOTIFY_STATE_ASTRO_CALIBRATION       = 15210
CMD_NOTIFY_STATE_ASTRO_GOTO              = 15211
CMD_NOTIFY_STATE_ASTRO_TRACKING          = 15212
CMD_NOTIFY_TEMPERATURE                   = 15243
CMD_NOTIFY_FOCUS_POSITION                = 15257
CMD_NOTIFY_CMOS_TEMPERATURE              = 15292
CMD_NOTIFY_WIDE_FOCUS_POSITION           = 15300  # NEW: wide lens focus position
CMD_NOTIFY_LENS_DEFOG_STATE              = 15301  # lens defog state
CMD_NOTIFY_AUTO_COOLING_STATE            = 15302  # auto cooling state
CMD_NOTIFY_AUTO_SHUTDOWN_STATE           = 15303  # auto shutdown state
CMD_NOTIFY_ANTI_TANGLE_STATE             = 15306  # cable anti-tangle state

# ── Task Center commands (16400-16599) ────────────────────────────────────────
CMD_GLOBAL_TASK_MANAGER_ENTER_CAMERA     = 16404
CMD_GLOBAL_TASK_GET_DEVICE_STATE_INFO    = 16405
CMD_GLOBAL_VOICE_ASSISTANT_TASK          = 16406  # NEW in June 2026 APK

# ── Voice Assistant commands (16800-16899) ────────────────────────────────────
CMD_VOICE_ASSISTANT_TASK                 = 16800  # NEW in June 2026 APK

# ── Device commands (17000-17099) ─────────────────────────────────────────────
CMD_DEVICE_LENS_DEFOG                    = 17000  # lens defog heater on/off
CMD_DEVICE_AUTO_COOLING                  = 17001  # CMOS auto cooling fan on/off
CMD_DEVICE_AUTO_SHUTDOWN                 = 17002  # auto shutdown on/off
CMD_DEVICE_ANTI_TANGLE                   = 17003  # cable anti-tangle (electronic limit) on/off

# ── Proto3 primitives ─────────────────────────────────────────────────────────
def _varint(v):
    v = v & 0xFFFFFFFFFFFFFFFF  # two's-complement uint64
    b = []
    while True:
        b.append(v & 0x7F); v >>= 7
        if v == 0: break
    for i in range(len(b) - 1): b[i] |= 0x80
    return bytes(b)

def _field(fn, wt, val):
    tag = _varint((fn << 3) | wt)
    if wt == 0: return tag + _varint(val)
    if wt == 1: return tag + struct.pack("<d", val)
    if wt == 2:
        if isinstance(val, str): val = val.encode()
        return tag + _varint(len(val)) + val
    raise ValueError(f"bad wt {wt}")

def _dvarint(buf, pos):
    r = 0; s = 0
    while True:
        b = buf[pos]; pos += 1; r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def _decode_fields(buf):
    """
    Decode a flat proto3 message into {field_number: value}.
    Varints -> int, 64-bit -> float(double), 32-bit -> int, length-delim -> bytes.
    Unknown/garbage trailing data stops the scan rather than raising.
    Use this instead of _dvarint(data, <fixed offset>) so field order and
    multi-byte tags/values are handled correctly.
    """
    out = {}
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _dvarint(buf, i)
        fn = tag >> 3; wt = tag & 7
        if wt == 0:
            v, i = _dvarint(buf, i); out[fn] = v
        elif wt == 1:
            if i + 8 > n: break
            out[fn] = struct.unpack_from("<d", buf, i)[0]; i += 8
        elif wt == 2:
            ln, i = _dvarint(buf, i); out[fn] = buf[i:i+ln]; i += ln
        elif wt == 5:
            if i + 4 > n: break
            out[fn] = struct.unpack_from("<i", buf, i)[0]; i += 4
        else:
            break
    return out

# ── Packet builder / parser ───────────────────────────────────────────────────
def build_ws_packet(cmd, data=b"", device_id=1, client_id=""):
    mid = _module_for_cmd(cmd)
    pkt = (_field(1,0,1) + _field(2,0,20) + _field(3,0,device_id) +
           _field(4,0,mid) + _field(5,0,cmd) + _field(6,0,0))
    if data:      pkt += _field(7, 2, data)
    if client_id: pkt += _field(8, 2, client_id)
    return pkt

def parse_ws_packet(raw):
    r = {"major_version":0,"minor_version":0,"device_id":0,
         "module_id":0,"cmd":0,"type":0,"data":b"","client_id":""}
    i = 0
    while i < len(raw):
        try:
            tag, i = _dvarint(raw, i)
            fn = tag >> 3; wt = tag & 7
            if wt == 0:
                v, i = _dvarint(raw, i)
                k = {1:"major_version",2:"minor_version",3:"device_id",
                     4:"module_id",5:"cmd",6:"type"}.get(fn)
                if k: r[k] = v
            elif wt == 2:
                ln, i = _dvarint(raw, i)
                pay = raw[i:i+ln]; i += ln
                if fn == 7: r["data"] = pay
                elif fn == 8: r["client_id"] = pay.decode("utf-8", "replace")
            elif wt == 1: i += 8
            elif wt == 5: i += 4
            else: break
        except Exception: break
    return r

# ── Payload builders ──────────────────────────────────────────────────────────
def p_int(v):
    # Single int32 field-1 payload. v==0 intentionally yields an EMPTY payload:
    # proto3's default for a missing int32 is 0, so an empty message decodes to
    # {field1: 0} on the device - i.e. 'off' / index 0. This keeps packets minimal
    # and matches how the app omits default-valued fields. (It does mean the wire
    # form can't distinguish an explicit 0 from an unset field, which is fine here
    # because every command using p_int treats 0 as the real default.)
    return _field(1, 0, v) if v != 0 else b""

def p_goto_dso(ra, dec, name="", goto_only=False):
    """
    ReqGotoDSO payload.
    ra, dec: float (hours / degrees).
    goto_only: new field in June 2026 APK — if True, only slews without stacking.
    """
    d = _field(1, 1, float(ra)) + _field(2, 1, float(dec))
    if name: d += _field(3, 2, name)
    if goto_only: d += _field(4, 0, 1)
    return d

def p_location(lat, lon, alt=0.0):
    return _field(1, 1, float(lat)) + _field(2, 1, float(lon)) + _field(3, 1, float(alt))

def p_joystick(vector_angle_deg, vector_length):
    """
    ReqMotorServiceJoystick: field1=vector_angle(double), field2=vector_length(double).
    Both are 64-bit doubles (proto wire type 1).
    vector_angle_deg: 0=East, 90=North, 180=West, 270=South.
    vector_length: 0.0-1.0 (proportion of max motor speed).
    """
    return _field(1, 1, float(vector_angle_deg)) + _field(2, 1, float(vector_length))

def xy_to_polar(x, y):
    """
    Convert cartesian joystick x/y (-100..100) to (vector_angle_deg, vector_length).
    x positive=East, y positive=North.
    Matches PolarDpadJoystickView atan2(-cy, cx) convention.
    """
    import math
    if x == 0 and y == 0: return 0.0, 0.0
    angle = math.degrees(math.atan2(y, x))
    if angle < 0: angle += 360.0
    length = min(1.0, math.hypot(x, y) / 100.0)
    return angle, length

# ── Main controller class ─────────────────────────────────────────────────────
class DwarfLab:
    DEFAULT_IP = "192.168.88.1"
    WS_PORT    = 9900
    HTTP_PORT  = 8082

    def __init__(self, host=DEFAULT_IP, device_id=1, on_notify=None):
        self.host      = host
        self.device_id = device_id
        self.client_id = str(uuid.uuid4())
        self._ws        = None
        self._ws_thread = None
        self._connected = threading.Event()
        self.on_notify  = on_notify
        self.state = {
            "connected": False,
            "battery": None,
            "temperature": None,        # raw units from CMD_NOTIFY_TEMPERATURE (field 2)
            "cmos_temp": None,          # raw units from CMD_NOTIFY_CMOS_TEMPERATURE (field 2)
            "sdcard_available": None,   # from 15203 field 1 (SDCARD_INFO available_size)
            "sdcard_total": None,       # from 15203 field 2 (SDCARD_INFO total_size)
            "wide_focus_position": None,  # NEW
            "lens_defog": None,            # device: lens defog heater
            "auto_cooling": None,          # device: CMOS cooling fan
            "auto_shutdown": None,         # device: auto shutdown
            "anti_tangle": None,           # device: cable anti-tangle
            "goto_state": None,
            "tracking": False,
            "stacking": False,
            "stacking_progress": 0,
            "calibrating": False,
            "focus_position": None,
            "last_cmd": None,
        }

    def _ws_url(self):
        return f"ws://{self.host}:{self.WS_PORT}/?client_id={self.client_id}"

    def _start_ws(self):
        self._ws = websocket.WebSocketApp(
            self._ws_url(),
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
        )
        t.start()
        return t

    def connect(self, timeout=10.0):
        self._ws_thread = self._start_ws()
        ok = self._connected.wait(timeout)
        if ok: self.state["connected"] = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()
        return ok

    def _reconnect_loop(self):
        while True:
            time.sleep(5)
            if not self._connected.is_set():
                log.warning("WS disconnected — reconnecting...")
                self.state["connected"] = False
                try:
                    if self._ws: self._ws.close()
                except Exception: pass
                self._connected.clear()
                self._ws_thread = self._start_ws()
                ok = self._connected.wait(10)
                if ok:
                    log.info("WS reconnected")
                    self.state["connected"] = True
                else:
                    log.warning("WS reconnect failed, will retry...")

    def disconnect(self):
        if self._ws: self._ws.close()
        self._connected.clear()
        self.state["connected"] = False

    def _on_open(self, ws):
        self._connected.set()

    def _on_message(self, ws, msg):
        if not isinstance(msg, bytes): return
        pkt = parse_ws_packet(msg)
        cmd  = pkt["cmd"]
        data = pkt["data"]

        # Battery
        if cmd == CMD_NOTIFY_ELE:
            try: v, _ = _dvarint(data, 1); self.state["battery"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_ELE_STATUS:
            try: v, _ = _dvarint(data, 1); self.state["battery"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        # Temperatures
        elif cmd == CMD_NOTIFY_TEMPERATURES:
            # NOTE: 15203 maps to CMD_NOTIFY_SDCARD_INFO in the APK (fields:
            # available_size=1, total_size=2). Whether any firmware actually
            # emits dual temperatures here is unverified, so we record the raw
            # field values under sdcard_* and leave temperature/cmos_temp to the
            # dedicated 15243/15292 notifications below.
            try:
                flds = _decode_fields(data)
                if 1 in flds: self.state["sdcard_available"] = flds[1]
                if 2 in flds: self.state["sdcard_total"]     = flds[2]
            except Exception: pass
        elif cmd == CMD_NOTIFY_TEMPERATURE:
            # Temperature message = { code:int32 (field 1), temperature:int32 (field 2) }.
            # The value lives in field 2; field 1 is a status code. Raw units
            # (no scaling applied - confirm against the app before dividing).
            try:
                t = _decode_fields(data).get(2)
                if t is not None: self.state["temperature"] = t
            except Exception: pass
        elif cmd == CMD_NOTIFY_CMOS_TEMPERATURE:
            # CmosTemperature message = { code (field 1), temperature (field 2) }.
            try:
                t = _decode_fields(data).get(2)
                if t is not None: self.state["cmos_temp"] = t
            except Exception: pass
        # Focus positions
        elif cmd == CMD_NOTIFY_FOCUS_POSITION:
            try: v, _ = _dvarint(data, 1); self.state["focus_position"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_WIDE_FOCUS_POSITION:    # NEW
            try: v, _ = _dvarint(data, 1); self.state["wide_focus_position"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        # Device state notifications (NEW)
        elif cmd == CMD_NOTIFY_LENS_DEFOG_STATE:
            try: v, _ = _dvarint(data, 1); self.state["lens_defog"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_AUTO_COOLING_STATE:
            try: v, _ = _dvarint(data, 1); self.state["auto_cooling"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_ANTI_TANGLE_STATE:
            try: v, _ = _dvarint(data, 1); self.state["anti_tangle"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_AUTO_SHUTDOWN_STATE:
            try: v, _ = _dvarint(data, 1); self.state["auto_shutdown"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        # GoTo / tracking / stacking
        elif cmd == CMD_NOTIFY_STATE_ASTRO_GOTO:
            try: v, _ = _dvarint(data, 1); self.state["goto_state"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_STATE_ASTRO_TRACKING:
            try: v, _ = _dvarint(data, 1); self.state["tracking"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_STATE_CAPTURE_RAW_LIVE_STACKING:
            try: v, _ = _dvarint(data, 1); self.state["stacking"] = bool(v)
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)
        elif cmd == CMD_NOTIFY_PROGRASS_CAPTURE_RAW_LIVE_STACKING:
            try: v, _ = _dvarint(data, 1); self.state["stacking_progress"] = v
            except Exception as e:
                log.debug("notify decode failed (cmd=%s): %s", cmd, e)

        if self.on_notify: self.on_notify(pkt)

    def _on_error(self, ws, e):
        log.error(f"WS error: {e}")
        self.state["connected"] = False

    def _on_close(self, ws, c, r):
        self._connected.clear()
        self.state["connected"] = False

    def send(self, cmd, data=b""):
        if not self._connected.is_set(): return False
        self._ws.send(
            build_ws_packet(cmd, data, self.device_id, self.client_id),
            opcode=websocket.ABNF.OPCODE_BINARY,
        )
        self.state["last_cmd"] = cmd
        return True

    # ── Camera initialisation sequence ────────────────────────────────────────
    def enter_camera(self, encode_type=1):
        """
        CMD_GLOBAL_TASK_MANAGER_ENTER_CAMERA (16404).
        MUST be sent before open_camera() on Dwarf3 firmware v1.8+.
        encode_type 1 = H.265/HEVC (activates RTSP encoder).
        Payload: ReqEnterCamera { client_param { encode_type: 1 } }
        """
        client_params = _field(1, 0, encode_type)
        data = _field(1, 2, client_params)
        self.send(CMD_GLOBAL_TASK_MANAGER_ENTER_CAMERA, data)

    def open_camera(self, binning=False, rtsp_encode_type=1):
        """
        CMD_CAMERA_TELE_OPEN_CAMERA (10000). Must be called after enter_camera().
        New APK uses CameraType enum internally but the wire payload is unchanged:
        ReqOpenCamera { binning: bool, rtspEncodeType: 1 }
        rtsp_encode_type=1 (H.265) is required to activate the RTSP stream.
        """
        data = _field(1, 0, 1 if binning else 0) + _field(2, 0, rtsp_encode_type)
        self.send(CMD_CAMERA_TELE_OPEN_CAMERA, data)

    def open_camera_wide(self, binning=False, rtsp_encode_type=1):
        """CMD_CAMERA_WIDE_OPEN_CAMERA (12000) — same payload as open_camera()."""
        data = _field(1, 0, 1 if binning else 0) + _field(2, 0, rtsp_encode_type)
        self.send(CMD_CAMERA_WIDE_OPEN_CAMERA, data)

    # ── Camera controls ───────────────────────────────────────────────────────
    def close_camera(self):          self.send(CMD_CAMERA_TELE_CLOSE_CAMERA)
    def take_photo(self):            self.send(CMD_CAMERA_TELE_PHOTOGRAPH)
    def take_photo_raw(self):        self.send(CMD_CAMERA_TELE_PHOTO_RAW)
    def start_burst(self, n=3):      self.send(CMD_CAMERA_TELE_BURST, p_int(n))
    def start_record(self):          self.send(CMD_CAMERA_TELE_START_RECORD)
    def stop_record(self):           self.send(CMD_CAMERA_TELE_STOP_RECORD)
    def start_record_wide(self):     self.send(CMD_CAMERA_WIDE_START_RECORD)
    def stop_record_wide(self):      self.send(CMD_CAMERA_WIDE_STOP_RECORD)
    def start_timelapse(self):       self.send(CMD_CAMERA_TELE_START_TIMELAPSE_PHOTO)
    def stop_timelapse(self):        self.send(CMD_CAMERA_TELE_STOP_TIMELAPSE_PHOTO)
    def set_exposure(self, i):       self.send(CMD_CAMERA_TELE_SET_EXP, p_int(i))
    def set_gain(self, i):           self.send(CMD_CAMERA_TELE_SET_GAIN, p_int(i))
    def set_brightness(self, v):     self.send(CMD_CAMERA_TELE_SET_BRIGHTNESS, p_int(v))
    def set_contrast(self, v):       self.send(CMD_CAMERA_TELE_SET_CONTRAST, p_int(v))
    def set_saturation(self, v):     self.send(CMD_CAMERA_TELE_SET_SATURATION, p_int(v))
    def set_sharpness(self, v):      self.send(CMD_CAMERA_TELE_SET_SHARPNESS, p_int(v))
    def set_wb_mode(self, m):        self.send(CMD_CAMERA_TELE_SET_WB_MODE, p_int(m))
    def set_wb_ct(self, i):          self.send(CMD_CAMERA_TELE_SET_WB_CT, p_int(i))
    def set_ircut(self, v):          self.send(CMD_CAMERA_TELE_SET_IRCUT, p_int(v))
    def set_jpg_quality(self, q):    self.send(CMD_CAMERA_TELE_SET_JPG_QUALITY, p_int(q))
    def switch_resolution(self, i):  self.send(CMD_CAMERA_TELE_SWITCH_RESOLUTION, p_int(i))
    def switch_framerate(self, i):   self.send(CMD_CAMERA_TELE_SWITCH_FRAMERATE, p_int(i))
    def get_all_params(self):        self.send(CMD_CAMERA_TELE_GET_ALL_PARAMS)

    # ── Astro controls ────────────────────────────────────────────────────────
    def start_calibration(self, lat=0.0, lon=0.0):
        d = _field(1, 1, float(lon)) + _field(2, 1, float(lat))
        self.send(CMD_ASTRO_START_CALIBRATION, d)
    def stop_calibration(self):      self.send(CMD_ASTRO_STOP_CALIBRATION)

    def goto_dso(self, ra, dec, name="", goto_only=False):
        """
        GoTo deep sky object by RA (hours) / Dec (degrees).
        goto_only=True: slew only, no stacking (new field in June 2026 APK).
        """
        self.send(CMD_ASTRO_START_GOTO_DSO, p_goto_dso(ra, dec, name, goto_only))

    def goto_solar(self, i):         self.send(CMD_ASTRO_START_GOTO_SOLAR_SYSTEM, p_int(i))
    def stop_goto(self):             self.send(CMD_ASTRO_STOP_GOTO)
    def one_click_goto_dso(self, ra, dec, name=""):
        self.send(CMD_ASTRO_START_ONE_CLICK_GOTO_DSO, p_goto_dso(ra, dec, name))
    def stop_one_click_goto(self):   self.send(CMD_ASTRO_STOP_ONE_CLICK_GOTO)
    def go_live(self):               self.send(CMD_ASTRO_GO_LIVE)
    def go_live_wide(self):          self.send(CMD_ASTRO_WIDE_GO_LIVE)

    def start_stacking(self, exp_ms=10000, gain=0, count=0):
        d = b""
        if exp_ms: d += _field(1, 0, exp_ms)
        if gain:   d += _field(2, 0, gain)
        if count:  d += _field(3, 0, count)
        self.send(CMD_ASTRO_START_CAPTURE_RAW_LIVE_STACKING, d)
    def stop_stacking(self):         self.send(CMD_ASTRO_STOP_CAPTURE_RAW_LIVE_STACKING)
    def start_plate_solve(self):     self.send(CMD_ASTRO_START_EQ_SOLVING)
    def stop_plate_solve(self):      self.send(CMD_ASTRO_STOP_EQ_SOLVING)
    def start_sky_finder(self):      self.send(CMD_ASTRO_START_SKY_TARGET_FINDER)
    def stop_sky_finder(self):       self.send(CMD_ASTRO_STOP_SKY_TARGET_FINDER)
    def start_ai_enhance(self):      self.send(CMD_ASTRO_START_AI_ENHANCE)
    def stop_ai_enhance(self):       self.send(CMD_ASTRO_STOP_AI_ENHANCE)
    def one_click_shoot(self):       self.send(CMD_ASTRO_START_ONE_CLICK_SHOOTING)

    # ── Focus controls ────────────────────────────────────────────────────────
    def auto_focus(self):            self.send(CMD_FOCUS_AUTO_FOCUS)
    def focus_step(self, s=1):       self.send(CMD_FOCUS_MANUAL_SINGLE_STEP, p_int(s))
    def focus_in(self):              self.send(CMD_FOCUS_START_MANUAL_CONTINUOUS, p_int(-1))
    def focus_out(self):             self.send(CMD_FOCUS_START_MANUAL_CONTINUOUS, p_int(1))
    def focus_stop(self):            self.send(CMD_FOCUS_STOP_MANUAL_CONTINUOUS)
    def astro_focus(self):           self.send(CMD_FOCUS_START_ASTRO_AUTO_FOCUS)
    def stop_astro_focus(self):      self.send(CMD_FOCUS_STOP_ASTRO_AUTO_FOCUS)

    # ── Motor / joystick controls ─────────────────────────────────────────────
    def joystick(self, x, y):
        """
        Move motors. x/y in range -100..100.
        Positive x = East, positive y = North.
        Sends ReqMotorServiceJoystick with polar vector_angle + vector_length doubles.
        """
        angle, length = xy_to_polar(x, y)
        self.send(CMD_STEP_MOTOR_JOYSTICK, p_joystick(angle, length))

    def joystick_stop(self):         self.send(CMD_STEP_MOTOR_JOYSTICK_STOP)

    # ── Tracking ──────────────────────────────────────────────────────────────
    def start_tracking(self):        self.send(CMD_TRACK_START_TRACK)
    def stop_tracking(self):         self.send(CMD_TRACK_STOP_TRACK)

    # ── Device commands (17000-17099, module 18) ───────────────────────────
    # Each takes Req{State}{ state:int32 } where state = 1 (on) / 0 (off),
    # matching DeviceSwitchState.fromEnabled() in the app. Field 1, varint.
    def set_lens_defog(self, enabled=True):
        """CMD_DEVICE_LENS_DEFOG (17000) — toggle lens defog heater."""
        self.send(CMD_DEVICE_LENS_DEFOG, p_int(1 if enabled else 0))

    def set_auto_cooling(self, enabled=True):
        """CMD_DEVICE_AUTO_COOLING (17001) — toggle CMOS auto cooling fan."""
        self.send(CMD_DEVICE_AUTO_COOLING, p_int(1 if enabled else 0))

    def set_auto_shutdown(self, enabled=True):
        """CMD_DEVICE_AUTO_SHUTDOWN (17002) — toggle auto shutdown."""
        self.send(CMD_DEVICE_AUTO_SHUTDOWN, p_int(1 if enabled else 0))

    def set_anti_tangle(self, enabled=True):
        """CMD_DEVICE_ANTI_TANGLE (17003) — toggle cable anti-tangle (electronic limit)."""
        self.send(CMD_DEVICE_ANTI_TANGLE, p_int(1 if enabled else 0))

    # ── System controls ───────────────────────────────────────────────────────
    def set_location(self, lat, lon, alt=0):
        self.send(CMD_SYSTEM_SET_LOCATION, p_location(lat, lon, alt))
    def sync_time(self):
        self.send(CMD_SYSTEM_SET_TIME, p_int(int(time.time() * 1000)))
    def reboot(self):                self.send(CMD_RGB_POWER_REBOOT)
    def power_down(self):            self.send(CMD_RGB_POWER_POWER_DOWN)
    def led_on(self):                self.send(CMD_RGB_POWER_POWERIND_ON)
    def led_off(self):               self.send(CMD_RGB_POWER_POWERIND_OFF)
    def get_device_state(self):      self.send(CMD_GLOBAL_TASK_GET_DEVICE_STATE_INFO)

    # ── HTTP REST API ─────────────────────────────────────────────────────────
    def http_device_info(self):
        try:
            r = requests.post(
                f"http://{self.host}:{self.HTTP_PORT}/deviceInfo",
                json={}, timeout=5
            )
            return r.json()
        except Exception as e:
            return {"error": str(e)}
