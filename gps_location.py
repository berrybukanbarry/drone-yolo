"""Sumber koordinat (GPS) untuk foto bukti & peringatan.

Dibaca terus di thread latar, sehingga saat pelanggaran terdeteksi koordinat terakhir langsung tersedia
tanpa memperlambat deteksi.

Sumber (alert_config.json -> "gps" -> "source"):
  auto     - kamera HP (--source http://...) -> GPS HP lewat IP Webcam; selain itu -> MAVLink drone.
             Bila tidak ada fix, memakai koordinat tetap (static_lat/static_lon) bila diisi.
  mavlink  - flight controller (Pixhawk/ArduPilot/PX4) lewat MAVLink. Perlu: pip install pymavlink
             mavlink_url mis. "udp:0.0.0.0:14550", "/dev/ttyACM0" atau "/dev/serial0".
  ipwebcam - GPS HP dari aplikasi IP Webcam (Android). Di aplikasi aktifkan:
             Pengaturan video -> "Data logging"/"Sensor" -> GPS/lokasi.
  nmea     - modul GPS USB/serial (u-blox NEO-6M dsb.). Perlu: pip install pyserial
  static   - koordinat tetap dari static_lat / static_lon (kamera yang dipasang diam).
"""
import json
import time
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("gps")

DEFAULT_GPS_CONFIG = {
    "enabled": True,
    "source": "auto",
    "mavlink_url": "udp:0.0.0.0:14550",
    "mavlink_baud": 57600,
    "ipwebcam_url": "",          # kosong = diambil dari --source (http://ip:8080/video)
    "nmea_port": "/dev/ttyUSB0",
    "nmea_baud": 9600,
    "static_lat": None,
    "static_lon": None,
    "static_alt": None,
    "max_age_s": 60,             # koordinat lebih tua dari ini dianggap usang
    "poll_s": 2,
}


class GpsReader:
    def __init__(self, cfg, camera_source=None, stop_event=None):
        self.cfg = dict(DEFAULT_GPS_CONFIG, **(cfg or {}))
        self.camera_source = camera_source
        self.stop_event = stop_event or threading.Event()
        self.lock = threading.Lock()
        self.fix = None          # dict lat/lon/alt/accuracy/source/time
        self.error = None
        self.source = self._pick_source()

    # ------------------------------------------------------------ publik
    def start(self):
        if not self.cfg.get("enabled"):
            log.info("GPS dimatikan (alert_config.json -> gps.enabled = false)")
            return self
        target = {"mavlink": self._run_mavlink, "ipwebcam": self._run_ipwebcam,
                  "nmea": self._run_nmea}.get(self.source)
        log.info("Sumber koordinat: %s%s", self.source,
                 " (cadangan: koordinat tetap)" if self._static() and target else "")
        if target:
            threading.Thread(target=self._guard, args=(target,), name="gps", daemon=True).start()
        return self

    def get(self):
        """Koordinat terbaru (dict) atau None. Dipanggil saat pelanggaran terdeteksi."""
        if not self.cfg.get("enabled"):
            return None
        with self.lock:
            fix = dict(self.fix) if self.fix else None
        if fix:
            age = time.time() - fix["time"]
            if age <= float(self.cfg["max_age_s"]):
                fix["age_s"] = round(age, 1)
                return fix
        static = self._static()
        if static:
            return static
        return None

    def status(self):
        fix = self.get()
        return {"source": self.source, "fix": fix, "error": None if fix else self.error}

    # ------------------------------------------------------------ dalam
    def _pick_source(self):
        src = str(self.cfg.get("source", "auto")).lower()
        if src != "auto":
            return src
        cam = str(self.camera_source or "")
        if cam.startswith(("http://", "https://")) or self.cfg.get("ipwebcam_url"):
            return "ipwebcam"
        try:
            import pymavlink  # noqa: F401
            return "mavlink"
        except ImportError:
            return "static"

    def _static(self):
        lat, lon = self.cfg.get("static_lat"), self.cfg.get("static_lon")
        if lat is None or lon is None:
            return None
        return {"lat": float(lat), "lon": float(lon), "alt": self.cfg.get("static_alt"),
                "accuracy": None, "source": "static", "time": time.time(), "age_s": 0}

    def _set(self, lat, lon, alt=None, accuracy=None, source=None):
        if lat is None or lon is None or (abs(lat) < 1e-7 and abs(lon) < 1e-7):
            return  # 0,0 = belum ada fix
        with self.lock:
            self.fix = {"lat": round(float(lat), 7), "lon": round(float(lon), 7),
                        "alt": None if alt is None else round(float(alt), 1),
                        "accuracy": None if accuracy is None else round(float(accuracy), 1),
                        "source": source or self.source, "time": time.time()}
            self.error = None

    def _guard(self, target):
        """Jalankan pembaca; bila gagal (kabel lepas, jaringan putus) coba lagi."""
        warned = None
        while not self.stop_event.is_set():
            try:
                target()
            except Exception as e:  # jangan sampai GPS menghentikan detektor
                self.error = str(e)
                if warned != self.error:
                    log.warning("GPS (%s) gagal: %s - mencoba lagi", self.source, e)
                    warned = self.error
            self.stop_event.wait(5)

    # -- MAVLink (flight controller drone)
    def _run_mavlink(self):
        from pymavlink import mavutil
        url = self.cfg["mavlink_url"]
        conn = mavutil.mavlink_connection(url, baud=int(self.cfg["mavlink_baud"]))
        log.info("GPS: menunggu MAVLink di %s ...", url)
        try:
            while not self.stop_event.is_set():
                msg = conn.recv_match(type=["GLOBAL_POSITION_INT", "GPS_RAW_INT"], blocking=True, timeout=2)
                if msg is None:
                    continue
                if msg.get_type() == "GLOBAL_POSITION_INT":
                    self._set(msg.lat / 1e7, msg.lon / 1e7, msg.alt / 1000.0, source="mavlink")
                elif msg.fix_type >= 2:  # 2 = 2D fix, 3 = 3D fix
                    acc = msg.eph / 100.0 if msg.eph not in (0, 65535) else None
                    self._set(msg.lat / 1e7, msg.lon / 1e7, msg.alt / 1000.0, acc, source="mavlink")
        finally:
            conn.close()

    # -- IP Webcam (GPS HP)
    def _ipwebcam_urls(self):
        url = self.cfg.get("ipwebcam_url")
        if url:
            return [url]
        p = urllib.parse.urlparse(str(self.camera_source))
        root = f"{p.scheme}://{p.netloc}"
        # Versi IP Webcam berbeda-beda: coba keduanya
        return [f"{root}/gps.json", f"{root}/sensors.json?sense=gps"]

    def _run_ipwebcam(self):
        urls = self._ipwebcam_urls()
        p = urllib.parse.urlparse(str(self.camera_source))
        root = f"{p.scheme}://{p.netloc}"
        # Coba minta aplikasi IP Webcam menyalakan sensor GPS secara otomatis via web endpoint
        try:
            urllib.request.urlopen(f"{root}/settings/gps_active?set=on", timeout=3)
        except Exception:
            pass

        if not getattr(self, "_ipw_logged", False):
            log.info("GPS: membaca lokasi HP dari %s", " atau ".join(urls))
            self._ipw_logged = True
        while not self.stop_event.is_set():
            loc, errors = None, []
            for url in urls:
                try:
                    with urllib.request.urlopen(url, timeout=5) as r:
                        body = r.read().decode("utf-8", "replace").strip()
                    loc = _find_location(json.loads(body or "{}"))
                except urllib.error.HTTPError as e:
                    errors.append(f"{url.rsplit('/', 1)[-1]}: HTTP {e.code}")
                    continue
                except urllib.error.URLError as e:
                    if isinstance(e.reason, ConnectionRefusedError):
                        raise RuntimeError("server IP Webcam di HP tidak menyala (Start server di aplikasi)")
                    raise RuntimeError(f"HP tidak bisa dihubungi ({e.reason})")
                except ValueError:
                    errors.append(f"{url.rsplit('/', 1)[-1]}: bukan JSON")
                    continue
                if loc:
                    if urls[0] != url:
                        urls.remove(url)
                        urls.insert(0, url)  # pakai yang berhasil lebih dulu
                    break
            if loc:
                self._set(*loc, source="ipwebcam")
            else:
                self.error = ("HP belum mengirim GPS: di IP Webcam aktifkan Data logging & GPS, "
                              "izinkan Lokasi untuk aplikasi, dan nyalakan GPS HP"
                              + (f" ({'; '.join(errors)})" if errors else ""))
            self.stop_event.wait(float(self.cfg["poll_s"]))

    # -- Modul GPS serial (NMEA)
    def _run_nmea(self):
        import serial
        port = self.cfg["nmea_port"]
        log.info("GPS: membaca NMEA dari %s", port)
        with serial.Serial(port, int(self.cfg["nmea_baud"]), timeout=2) as ser:
            while not self.stop_event.is_set():
                line = ser.readline().decode("ascii", "replace").strip()
                fix = _parse_nmea(line)
                if fix:
                    self._set(*fix, source="nmea")


def _find_location(data):
    """Cari latitude/longitude di JSON IP Webcam (formatnya beda-beda antar versi)."""
    if isinstance(data, dict):
        keys = {k.lower(): v for k, v in data.items()}
        # sensors.json: {"gps": {"unit": "", "data": [[waktu_ms, [lat, lon, alt, akurasi, ...]], ...]}}
        gps = keys.get("gps")
        if isinstance(gps, dict) and isinstance(gps.get("data"), list):
            for entry in reversed(gps["data"]):
                vals = entry[1] if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list) else entry
                if isinstance(vals, list) and len(vals) >= 2 and all(isinstance(v, (int, float)) for v in vals[:2]) \
                        and -90 <= vals[0] <= 90 and -180 <= vals[1] <= 180:
                    return (vals[0], vals[1], vals[2] if len(vals) > 2 else None,
                            vals[3] if len(vals) > 3 else None)
        lat = keys.get("latitude", keys.get("lat"))
        lon = keys.get("longitude", keys.get("lon", keys.get("lng")))
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return lat, lon, keys.get("altitude"), keys.get("accuracy")
        for v in data.values():
            found = _find_location(v)
            if found:
                return found
    elif isinstance(data, list):
        for v in reversed(data):  # data sensor: entri terakhir paling baru
            found = _find_location(v)
            if found:
                return found
    return None


def _nmea_deg(value, hemi):
    if not value:
        return None
    dot = value.index(".")
    deg = float(value[:dot - 2]) + float(value[dot - 2:]) / 60.0
    return -deg if hemi in ("S", "W") else deg


def _parse_nmea(line):
    if not line.startswith("$") or "*" not in line:
        return None
    f = line.split("*")[0].split(",")
    try:
        if f[0].endswith("GGA") and f[6] not in ("", "0"):
            return _nmea_deg(f[2], f[3]), _nmea_deg(f[4], f[5]), float(f[9]) if f[9] else None, None
        if f[0].endswith("RMC") and f[2] == "A":
            return _nmea_deg(f[3], f[4]), _nmea_deg(f[5], f[6]), None, None
    except (ValueError, IndexError):
        return None
    return None


def format_coords(loc):
    """Teks koordinat untuk pesan: '-6.2000000, 106.8166660'."""
    return f"{loc['lat']:.7f}, {loc['lon']:.7f}"


def maps_link(loc):
    return f"https://maps.google.com/?q={loc['lat']:.7f},{loc['lon']:.7f}"

