"""Panel kontrol web: Start/Stop/Capture/Rekam detektor drone tanpa mengetik perintah.

Jalankan dengan klik dua kali "Mulai Panel.bat" (Windows) atau start_panel.sh (Linux),
lalu buka http://localhost:8000 (laptop) atau http://<IP-perangkat>:8000 (HP).
"""
import os
import sys
import json
import time
import signal
import socket
import logging
import argparse
import threading
import subprocess
import webbrowser
import collections
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = Path(__file__).resolve().parent
PANEL_PORT = 8000
DETECTOR_WEB_PORT = 8080
SETTINGS_FILE = BASE_DIR / "panel_settings.json"
MEDIA_DIRS = {"captures": BASE_DIR / "captures", "bukti": BASE_DIR / "bukti"}
LOG_LINES = 300
STOP_TIMEOUT = 5.0
RECORD_FPS = 10

MODES = {
    "ai": {
        "label": "Deteksi AI (dipastikan Gemini)",
        "script": "smoking_detector.py",
        "args": ["--engine", "ai"],
        "needs": ["smoking_vape.pt"],
    },
    "gcs": {
        "label": "Kirim ke GCS",
        "script": "drone1_yolo_sender.py",
        "args": [],
        "needs": [],
    },
    "smoking": {
        "label": "Deteksi rokok & vape (model lokal saja)",
        "script": "smoking_detector.py",
        "args": [],
        "needs": ["smoking_vape.pt"],
    },
    "smoking_demo": {
        "label": "Demo rokok (HP dianggap rokok)",
        "script": "smoking_detector.py",
        "args": ["--demo"],
        "needs": [],
    },
}

DEFAULT_SETTINGS = {
    "mode": "ai",
    "source_type": "0",       # "0", "1", atau "phone"
    "phone_url": "",
    "autostart": False,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("panel")


# ---------------------------------------------------------------- Pengaturan

settings_lock = threading.Lock()


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        # utf-8-sig: tetap terbaca walau file disimpan dengan BOM (mis. oleh Notepad)
        data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


settings = load_settings()


def source_from_settings(s):
    if s["source_type"] == "phone":
        return s["phone_url"].strip()
    return s["source_type"]


def mode_availability():
    result = {}
    for key, mode in MODES.items():
        missing = [f for f in [mode["script"], *mode["needs"]] if not (BASE_DIR / f).exists()]
        result[key] = {"label": mode["label"], "available": not missing, "missing": missing}
    return result


# ---------------------------------------------------------------- Proses detektor

class Detector:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.log = collections.deque(maxlen=LOG_LINES)
        self.started_at = None
        self.mode = None
        self.source = None
        self.last_exit_code = None
        self.stopping = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def add_log(self, line):
        self.log.append(line.rstrip())

    def start(self, mode_key, source):
        with self.lock:
            if self.running():
                return False, "Detektor sudah berjalan."
            mode = MODES.get(mode_key)
            if mode is None:
                return False, "Mode tidak dikenal."
            missing = mode_availability()[mode_key]["missing"]
            if missing:
                return False, "File belum ada: " + ", ".join(missing)
            if not source:
                return False, "Sumber kamera kosong. Isi URL kamera HP dulu."

            cmd = [sys.executable, "-u", str(BASE_DIR / mode["script"]),
                   "--source", source, "--web-port", str(DETECTOR_WEB_PORT), *mode["args"]]
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
            except OSError as e:
                return False, f"Gagal menjalankan: {e}"

            self.started_at = time.time()
            self.mode = mode_key
            self.source = source
            self.last_exit_code = None
            self.stopping = False
            self.add_log(f"===== START {mode['label']} | sumber: {source} =====")
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            log.info("Detektor dijalankan: %s", " ".join(cmd))
            return True, "Detektor dijalankan."

    def _pump(self, proc):
        for line in proc.stdout:
            self.add_log(line)
        code = proc.wait()
        with self.lock:
            if proc is self.proc:
                self.last_exit_code = code
        self.add_log(f"===== BERHENTI (kode {code}) =====")

    def stop(self):
        with self.lock:
            if not self.running():
                return False, "Detektor tidak sedang berjalan."
            self.stopping = True
            proc = self.proc
        recorder.stop()
        proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return True, "Detektor dihentikan."

    def status(self):
        with self.lock:
            running = self.running()
            state = "running" if running else ("error" if self.last_exit_code not in (None, 0)
                                               and not self.stopping else "stopped")
            return {
                "state": state,
                "mode": self.mode,
                "source": self.source,
                "uptime_s": round(time.time() - self.started_at, 1) if running else 0,
                "exit_code": self.last_exit_code,
            }


detector = Detector()


def detector_url(path):
    return f"http://127.0.0.1:{DETECTOR_WEB_PORT}{path}"


def fetch_detector(path, timeout=2.0):
    with urllib.request.urlopen(detector_url(path), timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- Capture & rekam

def media_name(prefix, ext):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"


def capture_photo():
    if not detector.running():
        return False, "Start detektor dulu sebelum capture."
    try:
        jpeg = fetch_detector("/snapshot.jpg")
    except OSError as e:
        return False, f"Belum ada gambar dari detektor ({e})."
    folder = MEDIA_DIRS["captures"]
    folder.mkdir(exist_ok=True)
    name = media_name("FOTO", "jpg")
    (folder / name).write_bytes(jpeg)
    return True, f"Foto disimpan: {name}"


class Recorder:
    """Merekam stream MJPEG detektor ke file mp4 (kecepatan tetap real-time)."""

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.filename = None
        self.started_at = None

    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.active():
            return False, "Sedang merekam."
        if not detector.running():
            return False, "Start detektor dulu sebelum merekam."
        MEDIA_DIRS["captures"].mkdir(exist_ok=True)
        self.filename = media_name("VIDEO", "webm")
        self.stop_event.clear()
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self.thread.start()
        return True, f"Mulai merekam: {self.filename}"

    def stop(self):
        if not self.active():
            return False, "Tidak sedang merekam."
        self.stop_event.set()
        self.thread.join(timeout=10)
        return True, f"Rekaman disimpan: {self.filename}"

    def _run(self):
        import cv2  # hanya dimuat saat merekam
        path = MEDIA_DIRS["captures"] / self.filename
        cap = cv2.VideoCapture(detector_url("/stream.mjpg"))
        writer = None
        written = 0
        try:
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    if not detector.running():
                        break
                    time.sleep(0.1)
                    continue
                if writer is None:
                    h, w = frame.shape[:2]
                    # WebM/VP8 bisa diputar langsung di browser (Chrome, Edge, Firefox, Android)
                    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"VP80"), RECORD_FPS, (w, h))
                    if not writer.isOpened():
                        # Cadangan bila OpenCV tanpa encoder VP8: MP4 (harus diunduh untuk diputar)
                        self.filename = Path(self.filename).with_suffix(".mp4").name
                        path = MEDIA_DIRS["captures"] / self.filename
                        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), RECORD_FPS, (w, h))
                # Ulangi frame bila detektor lebih lambat dari RECORD_FPS agar durasi sesuai waktu nyata
                target = int((time.time() - self.started_at) * RECORD_FPS) + 1
                while written < target:
                    writer.write(frame)
                    written += 1
        finally:
            cap.release()
            if writer is not None:
                writer.release()
            elif path.exists():
                path.unlink()
            log.info("Rekaman selesai: %s (%d frame)", self.filename, written)

    def status(self):
        return {"active": self.active(), "file": self.filename if self.active() else None,
                "elapsed_s": round(time.time() - self.started_at, 1) if self.active() else 0}


recorder = Recorder()


MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webm": "video/webm", ".mp4": "video/mp4"}
VIDEO_SUFFIXES = (".webm", ".mp4")


def list_media(limit=40):
    items = []
    for folder_key, folder in MEDIA_DIRS.items():
        if not folder.exists():
            continue
        for f in folder.iterdir():
            suffix = f.suffix.lower()
            if suffix in MEDIA_TYPES and f.is_file():
                st = f.stat()
                items.append({"folder": folder_key, "name": f.name, "size": st.st_size, "mtime": st.st_mtime,
                              "video": suffix in VIDEO_SUFFIXES,
                              # mp4v (rekaman lama) tidak bisa diputar browser, hanya diunduh
                              "playable": suffix != ".mp4"})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items[:limit]


def resolve_media(folder_key, name):
    folder = MEDIA_DIRS.get(folder_key)
    if folder is None or not name or name != Path(name).name:
        return None
    path = folder / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- Tes kamera HP

def test_camera(url):
    url = url.strip()
    if not url.startswith(("http://", "https://", "rtsp://")):
        return False, "URL harus diawali http:// atau rtsp://"
    # IP Webcam (Android): coba ambil satu foto dari /shot.jpg
    if url.startswith("http") and url.rstrip("/").endswith("/video"):
        shot = url.rstrip("/")[:-len("/video")] + "/shot.jpg"
        try:
            with urllib.request.urlopen(shot, timeout=5) as r:
                if r.headers.get_content_type() == "image/jpeg":
                    return True, f"Kamera HP terhubung ({len(r.read()) // 1024} KB per foto)."
        except OSError:
            pass
    result = {}

    def probe():
        import cv2
        cap = cv2.VideoCapture(url)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        result["ok"] = ok
        result["size"] = f"{frame.shape[1]}x{frame.shape[0]}" if ok else None

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout=10)
    if result.get("ok"):
        return True, f"Kamera terhubung ({result['size']})."
    return False, ("Kamera tidak bisa dihubungi. Pastikan aplikasi kamera di HP sudah Start server, "
                   "HP dan perangkat ini di Wi-Fi yang sama, dan URL benar (akhiri dengan /video).")


def scan_cameras(max_index=4):
    """Coba kamera 0..max_index-1 di perangkat ini dan kembalikan yang menghasilkan gambar."""
    if detector.running():
        return False, "Hentikan detektor dulu: kamera yang sedang dipakai tidak bisa dicari.", []
    import cv2
    found = []
    for index in range(max_index):
        if os.name == "nt":
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)   # DirectShow: cepat di Windows
        else:
            cap = cv2.VideoCapture(index)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        cap.release()
        if ok and frame is not None:
            found.append({"index": index, "size": f"{frame.shape[1]}x{frame.shape[0]}"})
    if not found:
        return False, "Tidak ada kamera yang terdeteksi. Cek kabel/izin kamera, atau pakai Kamera HP.", []
    names = ", ".join(f"Kamera {c['index']} ({c['size']})" for c in found)
    return True, f"Kamera ditemukan: {names}.", found


# ---------------------------------------------------------------- Server web panel

def local_ip():
    # connect() pada UDP tidak mengirim paket, hanya memilih interface keluar.
    # 10.42.0.1 (GCS) dipakai bila jaringan drone tidak punya jalur ke internet.
    for target in ("8.8.8.8", "10.42.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


_ip_cache = {"time": 0.0, "ips": []}


def all_ips():
    """Semua IPv4 perangkat ini (Wi-Fi, hotspot, LAN), dicache 5 detik."""
    now = time.time()
    if now - _ip_cache["time"] > 5:
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        _ip_cache["ips"] = sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))
        _ip_cache["time"] = now
    return _ip_cache["ips"]


def full_status():
    active = local_ip()
    others = [ip for ip in all_ips() if ip != active]
    status = {
        "detector": detector.status(),
        "recording": recorder.status(),
        "settings": settings,
        "modes": mode_availability(),
        "panel_url": f"http://{active}:{PANEL_PORT}",
        "panel_urls": [f"http://{ip}:{PANEL_PORT}" for ip in [active, *others]],
        "stream_port": DETECTOR_WEB_PORT,
        "live": None,
    }
    if detector.running():
        try:
            status["live"] = json.loads(fetch_detector("/status.json", timeout=1.0))
        except (OSError, ValueError):
            pass
    return status


class PanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PANEL_HTML.encode())
        elif path == "/api/status":
            self._json(full_status())
        elif path == "/api/log":
            self._json({"lines": list(detector.log)[-80:]})
        elif path == "/api/media":
            self._json({"items": list_media()})
        elif path.startswith("/media/"):
            parts = path.split("/")
            media = resolve_media(parts[2], urllib.parse.unquote(parts[3])) if len(parts) == 4 else None
            if media is None:
                self._send(404, "text/plain", b"Tidak ditemukan")
                return
            ctype = MEDIA_TYPES.get(media.suffix.lower(), "application/octet-stream")
            download = urllib.parse.parse_qs(parsed.query).get("download") == ["1"]
            self._send_file(media, ctype, download)
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}

        if path == "/api/start":
            with settings_lock:
                ok, msg = detector.start(settings["mode"], source_from_settings(settings))
        elif path == "/api/stop":
            ok, msg = detector.stop()
        elif path == "/api/restart":
            detector.stop()
            with settings_lock:
                ok, msg = detector.start(settings["mode"], source_from_settings(settings))
            msg = "Restart: " + msg
        elif path == "/api/capture":
            ok, msg = capture_photo()
        elif path == "/api/record":
            ok, msg = recorder.stop() if recorder.active() else recorder.start()
        elif path == "/api/test-camera":
            ok, msg = test_camera(body.get("url") or settings["phone_url"])
        elif path == "/api/scan-cameras":
            ok, msg, cams = scan_cameras()
            self._json({"ok": ok, "message": msg, "cameras": cams})
            return
        elif path == "/api/settings":
            with settings_lock:
                for key in DEFAULT_SETTINGS:
                    if key in body:
                        settings[key] = body[key]
                save_settings(settings)
            ok, msg = True, "Pengaturan disimpan."
        elif path == "/api/delete":
            media = resolve_media(body.get("folder"), body.get("name"))
            if media is None:
                ok, msg = False, "File tidak ditemukan."
            else:
                media.unlink()
                ok, msg = True, f"Dihapus: {media.name}"
        else:
            self._send(404, "text/plain", b"Not found")
            return
        self._json({"ok": ok, "message": msg})

    def _json(self, data):
        self._send(200, "application/json", json.dumps(data).encode())

    def _send(self, code, content_type, body, download=False, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download and filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            pass

    def _send_file(self, path, content_type, download):
        """Kirim file, mendukung Range agar video bisa digeser (seek) di pemutar browser."""
        data = path.read_bytes()
        total = len(data)
        start, end = 0, total - 1
        rng = self.headers.get("Range", "")
        partial = rng.startswith("bytes=") and total > 0
        if partial:
            first, _, last = rng[6:].split(",")[0].partition("-")
            try:
                if first:
                    start, end = int(first), int(last) if last else total - 1
                else:
                    start, end = max(0, total - int(last)), total - 1
            except ValueError:
                partial = False
            end = min(end, total - 1)
            if partial and start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        try:
            self.wfile.write(data[start:end + 1])
        except (ConnectionError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        log.debug("panel %s - " + fmt, self.client_address[0], *args)


PANEL_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel Drone</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" media="print" onload="this.media='all'"
      href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root {
    --bg: #0E1624;
    --side: #0B1220;
    --card: #131D2C;
    --card-2: #19253A;
    --line: #243246;
    --text: #E6EBF2;
    --muted: #93A0B4;
    --blue: #3B82F6;
    --blue-soft: #18284A;
    --green: #22A06B;
    --green-soft: #0E2A22;
    --red: #E5484D;
    --red-soft: #3A1519;
    --amber: #F5A524;
    --amber-soft: #33270F;
    --font: "Inter", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
    --mono: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
  }
  * { box-sizing: border-box; }
  /* Atribut hidden harus selalu menang atas display: flex/grid di bawah (mis. penutup video) */
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text); font-family: var(--font);
    font-size: 15px; line-height: 1.5; font-variant-numeric: tabular-nums;
  }
  h1, h2, h3, p { margin: 0; }
  a { color: var(--text); text-underline-offset: 3px; }
  button, select, input { font: inherit; }
  :focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
  svg.i { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }

  /* ---------- kerangka ---------- */
  .app {
    display: grid; min-height: 100vh;
    grid-template-columns: 220px minmax(0, 1fr);
    grid-template-rows: 64px minmax(0, 1fr);
    grid-template-areas: "top top" "side main";
  }
  .topbar {
    grid-area: top; display: flex; align-items: center; gap: 16px; padding: 0 24px;
    background: var(--side); border-bottom: 1px solid var(--line);
  }
  .logo { display: flex; align-items: center; gap: 12px; font-size: 19px; font-weight: 700; margin-right: auto; }
  .logo svg { width: 40px; height: 28px; color: var(--blue); }
  .online { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px; border-radius: 999px; background: var(--green-soft); font-size: 13px; font-weight: 600; }
  .online::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--green); }
  .online[data-ok="false"] { background: var(--red-soft); }
  .online[data-ok="false"]::before { background: var(--red); }
  .reach { padding-left: 16px; border-left: 1px solid var(--line); color: var(--muted); font-size: 13px; }
  .reach a { font-weight: 600; overflow-wrap: anywhere; }

  .sidebar { grid-area: side; padding: 20px 10px; background: var(--side); border-right: 1px solid var(--line); }
  .nav { display: flex; flex-direction: column; gap: 4px; }
  .nav button {
    display: flex; align-items: center; gap: 14px; width: 100%; min-height: 48px; padding: 0 16px;
    border: 0; border-radius: 8px; background: none; color: var(--text); font-weight: 500; text-align: left; cursor: pointer;
  }
  .nav button svg { color: var(--muted); }
  .nav button:hover { background: var(--card); }
  .nav button[aria-current="page"] { background: var(--blue-soft); color: #7FB0FF; }
  .nav button[aria-current="page"] svg { color: var(--blue); }

  main { grid-area: main; padding: 24px; min-width: 0; }
  .view[hidden] { display: none; }
  .view-title { font-size: 20px; font-weight: 700; margin-bottom: 18px; }

  /* ---------- kartu ---------- */
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; min-width: 0; }
  .card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; font-weight: 600; }
  .card-head .link { margin-left: auto; color: #7FB0FF; font-weight: 500; font-size: 14px; background: none; border: 0; cursor: pointer; padding: 4px; }
  .card-head .link:hover { text-decoration: underline; }

  /* ---------- tata letak Live View ---------- */
  .live-grid { display: grid; grid-template-columns: minmax(0, 1fr) 420px; gap: 24px; align-items: start; }
  .live-main > *, .live-side > * { min-width: 0; }
  .live-main, .live-side { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; min-width: 0; }

  /* ---------- video ---------- */
  .feed { position: relative; aspect-ratio: 4 / 3; max-height: 62vh; width: 100%; border: 1px solid #33445E; border-radius: 12px; background: #0A111C; overflow: hidden; }
  .feed img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; visibility: hidden; }
  .feed img[data-live="true"] { visibility: visible; }
  .feed-empty { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; text-align: center; padding: 24px; }
  .feed-empty svg { width: 48px; height: 48px; color: var(--muted); stroke-width: 1.4; }
  .feed-empty strong { font-size: 17px; }
  .feed-empty span { color: var(--muted); max-width: 34ch; }
  .chip {
    position: absolute; display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px;
    border-radius: 999px; background: rgba(14, 22, 36, .85); border: 1px solid var(--line); font-size: 13px; font-weight: 500;
  }
  .chip-live { top: 14px; left: 14px; }
  .chip-live::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
  .chip-live[data-on="true"]::before { background: var(--red); }
  .chip-cam { bottom: 14px; left: 14px; }
  .chip-cam .sep { width: 1px; height: 14px; background: var(--line); }
  .chip-cam .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .chip-cam .dot[data-on="true"] { background: var(--green); }
  .feed-tools { position: absolute; top: 12px; right: 12px; display: flex; gap: 8px; }
  .icon-btn {
    display: inline-flex; align-items: center; justify-content: center; width: 40px; height: 40px;
    border-radius: 8px; border: 1px solid var(--line); background: rgba(14, 22, 36, .85); color: var(--text); cursor: pointer;
  }
  .icon-btn:hover { background: var(--card-2); }
  .icon-btn[aria-pressed="true"] { background: var(--blue-soft); border-color: var(--blue); color: #7FB0FF; }

  /* ---------- tombol ---------- */
  .toolbar { display: flex; flex-wrap: wrap; gap: 12px; }
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-height: 44px; padding: 0 18px;
    border: 1px solid #33445E; border-radius: 8px; background: var(--card); color: var(--text); font-weight: 600; cursor: pointer;
  }
  .btn:hover { background: var(--card-2); }
  .btn:disabled { opacity: .4; cursor: not-allowed; background: var(--card); }
  .btn-primary { background: var(--green); border-color: var(--green); color: #fff; }
  .btn-primary:hover { background: #1B8A5B; }
  .btn-primary:disabled { background: var(--green); }
  .btn-primary[data-running="true"] { background: var(--red); border-color: var(--red); }
  .btn-primary[data-running="true"]:hover { background: #C93C41; }
  .btn[aria-pressed="true"] { border-color: var(--red); color: #FF9EA1; }

  /* ---------- pelat status ---------- */
  .sign {
    display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 20px; align-items: center; padding: 22px;
    border-radius: 12px; border: 1px solid var(--line); border-left-width: 5px; background: var(--card);
  }
  .sign-mark { width: 96px; height: 96px; display: block; }
  .sign-disc { fill: #fff; }
  .sign-ring { fill: none; stroke: #7A8699; stroke-width: 9; }
  .sign-slash { stroke: #7A8699; stroke-width: 9; }
  .pict { fill: #1C2430; }
  .pict-smoke { fill: none; stroke: #1C2430; stroke-width: 3; stroke-linecap: round; }
  .pict-cam { display: none; }
  .sign[data-kind="cam"] .pict-cig, .sign[data-kind="cam"] .sign-slash { display: none; }
  .sign[data-kind="cam"] .pict-cam { display: inline; }
  .sign-title { font-size: 25px; font-weight: 700; line-height: 1.2; }
  .sign-sub { margin-top: 6px; color: var(--muted); }
  .sign[data-state="running"] { background: var(--green-soft); border-color: #1F5A45; border-left-color: var(--green); }
  .sign[data-state="running"] .sign-ring, .sign[data-state="running"] .sign-slash,
  .sign[data-state="alert"] .sign-ring, .sign[data-state="alert"] .sign-slash { stroke: var(--red); }
  .sign[data-state="running"][data-kind="cam"] .sign-ring { stroke: var(--green); }
  .sign[data-state="loading"] { background: var(--blue-soft); border-color: #2A4170; border-left-color: var(--blue); }
  .sign[data-state="alert"] { background: var(--red-soft); border-color: #6B2328; border-left-color: var(--red); }
  .sign[data-state="alert"] .sign-title { color: #FF8A8E; }
  .sign[data-state="alert"] .sign-sub { color: #F2C7C9; }
  .sign[data-state="error"] { background: var(--amber-soft); border-color: #5C4517; border-left-color: var(--amber); }
  .sign[data-state="error"] .sign-title { color: var(--amber); }
  .sign.is-stamped .sign-mark { animation: stamp 260ms cubic-bezier(.2, 1.4, .4, 1); }
  @keyframes stamp { from { transform: scale(.82) rotate(-6deg); } to { transform: none; } }
  @media (prefers-reduced-motion: reduce) { .sign.is-stamped .sign-mark { animation: none; } }

  /* ---------- angka ---------- */
  .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .stat { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 14px; min-width: 0; }
  .stat-label { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat-label svg { width: 16px; height: 16px; }
  .stat-value { margin-top: 12px; font-size: 26px; font-weight: 700; line-height: 1.1; white-space: nowrap; }

  /* ---------- kendali & formulir ---------- */
  .field { margin-top: 14px; }
  .field:first-of-type { margin-top: 0; }
  .field label { display: block; font-size: 14px; font-weight: 500; margin-bottom: 6px; }
  select, input[type="text"] {
    width: 100%; min-height: 44px; padding: 0 12px; color: var(--text); background: var(--card-2);
    border: 1px solid var(--line); border-radius: 8px;
  }
  select:focus-visible, input[type="text"]:focus-visible { border-color: var(--blue); outline-offset: 0; }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
  .switch { display: flex; align-items: center; gap: 12px; margin-top: 16px; cursor: pointer; }
  .switch input { appearance: none; width: 42px; height: 24px; border-radius: 999px; background: #33445E; position: relative; cursor: pointer; flex: none; margin: 0; transition: background .15s; }
  .switch input::after { content: ""; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: transform .15s; }
  .switch input:checked { background: var(--green); }
  .switch input:checked::after { transform: translateX(18px); }
  .hint { margin-top: 12px; font-size: 13px; color: var(--muted); }
  /* ---------- lokasi GPS ---------- */
  .gps-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; align-items: baseline; }
  .gps-coord { font-family: ui-monospace, Consolas, monospace; font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .gps-meta { grid-column: 1 / -1; font-size: 13px; color: var(--muted); margin: 0; }
  .gps-maps { color: #7FB0FF; font-size: 14px; text-decoration: none; white-space: nowrap; }
  .gps-maps:hover { text-decoration: underline; }
  .gps-maps[hidden] { display: none; }
  .gps-last { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
  .gps-last-label { font-size: 13px; color: var(--muted); margin: 0 0 4px; }
  .gps-last .gps-coord { color: #FF8A8E; font-size: 15px; }
  .gps-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
  .gps-dot[data-on="true"] { background: var(--green); }
  .chip-gps { position: absolute; right: 14px; bottom: 14px; display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; background: rgba(10, 17, 28, .78); font-size: 12px; font-weight: 600; font-family: ui-monospace, Consolas, monospace; pointer-events: none; }
  .chip-gps[hidden] { display: none; }
  .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 18px; }
  .msg { min-height: 20px; margin-top: 12px; font-size: 14px; color: var(--muted); }
  .msg[data-tone="ok"] { color: #5DD39E; }
  .msg[data-tone="err"] { color: #FF8A8E; }

  /* ---------- deteksi ---------- */
  .dets { list-style: none; margin: 0; padding: 0; }
  .dets li { display: grid; grid-template-columns: 7ch minmax(0, 1fr) 4.5ch; gap: 12px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--line); }
  .dets li:last-child { border-bottom: 0; }
  .dets li.empty { display: block; color: var(--muted); padding: 2px 0; }
  .det-name { font-weight: 600; }
  .det-bar { height: 6px; background: var(--card-2); border-radius: 3px; overflow: hidden; }
  .det-bar i { display: block; height: 100%; background: var(--blue); }
  .det-pct { text-align: right; color: var(--muted); }
  .dets li.is-violation .det-name { color: #FF8A8E; }
  .dets li.is-violation .det-bar i { background: var(--red); }
  .stat-sub { margin-top: 4px; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ai-verdict { margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: var(--card-2); color: var(--muted); font-size: 13px; line-height: 1.5; }
  .ai-verdict[data-yes="true"] { background: var(--red-soft); color: #F2C7C9; }
  .addr-list { margin: 0; padding: 0; list-style: none; }
  .addr-list li + li { margin-top: 4px; }
  .addr-list small { color: var(--muted); margin-left: 6px; }

  /* ---------- foto ---------- */
  .strip { display: grid; grid-auto-flow: column; grid-auto-columns: 168px; gap: 12px; overflow-x: auto; padding-bottom: 6px; }
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
  .shot { position: relative; margin: 0; border-radius: 8px; overflow: hidden; background: var(--card-2); border: 1px solid var(--line); }
  .shot.is-evidence { border-color: var(--red); }
  .shot-open { display: block; width: 100%; aspect-ratio: 4 / 3; padding: 0; border: 0; background: #0A111C; cursor: pointer; }
  .shot-open img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .shot-open .play { color: #fff; font-size: 30px; }
  .shot-time { position: absolute; left: 0; right: 0; bottom: 0; padding: 18px 8px 6px; font-size: 12px; font-weight: 600; background: linear-gradient(transparent, rgba(0, 0, 0, .75)); pointer-events: none; }
  .badge { position: absolute; top: 6px; left: 6px; padding: 1px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; background: rgba(14, 22, 36, .85); }
  .shot.is-evidence .badge { background: var(--red); }
  .shot-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; font-size: 13px; color: var(--muted); }
  .shot-foot .actions-mini { display: flex; gap: 4px; }
  .shot-foot button { border: 0; background: none; color: var(--muted); cursor: pointer; padding: 6px; border-radius: 6px; display: inline-flex; }
  .shot-foot button:hover { color: var(--text); background: var(--card); }
  .shot-foot button.del:hover { color: #FF8A8E; }
  .empty-note { color: var(--muted); }
  .filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .filters button { min-height: 36px; padding: 0 14px; border-radius: 999px; border: 1px solid var(--line); background: var(--card); color: var(--text); cursor: pointer; font-weight: 500; }
  .filters button[aria-pressed="true"] { background: var(--blue-soft); border-color: var(--blue); color: #7FB0FF; }

  /* ---------- log ---------- */
  .log { margin: 0; padding: 12px 14px; background: #0A111C; border-radius: 8px; font-family: var(--mono); font-size: 12.5px; line-height: 1.6; color: #B9C3D1; white-space: pre-wrap; word-break: break-word; overflow: auto; }
  .log-short { max-height: 92px; }
  .log-full { max-height: calc(100vh - 220px); min-height: 300px; }

  /* ---------- tentang ---------- */
  .about { display: grid; gap: 16px; max-width: 760px; }
  .about dl { display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 10px 16px; margin: 0; }
  .about dt { color: var(--muted); }
  .about dd { margin: 0; }
  .about p + p { margin-top: 10px; }
  .settings-wrap { display: grid; gap: 20px; max-width: 640px; }

  /* ---------- penampil foto ---------- */
  .viewer { border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: var(--card); color: var(--text); max-width: min(980px, 94vw); }
  .viewer::backdrop { background: rgba(5, 9, 15, .85); }
  .viewer img, .viewer video { display: block; max-width: 100%; max-height: 76vh; border-radius: 8px; background: #000; }
  .viewer video { width: min(900px, 90vw); }
  .viewer-note { margin-top: 10px; color: var(--muted); font-size: 14px; max-width: 60ch; }
  .shot-open video { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
  .shot-open { position: relative; }
  .shot-open .play {
    position: absolute; inset: 0; margin: auto; width: 46px; height: 46px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center; background: rgba(14, 22, 36, .75); font-size: 18px; padding-left: 3px;
  }
  .viewer-bar { display: flex; gap: 10px; margin-top: 12px; }
  .viewer-bar .btn { flex: 1; }

  /* ---------- layar kecil ---------- */
  @media (max-width: 1180px) {
    .live-grid { grid-template-columns: minmax(0, 1fr) 340px; }
    .sign { grid-template-columns: 72px minmax(0, 1fr); }
    .sign-mark { width: 72px; height: 72px; }
    .stat-label svg { display: none; }
    .app { grid-template-columns: 190px minmax(0, 1fr); }
    .stat { padding: 12px 10px; }
    .stat-label { font-size: 12px; }
    .stat-value { font-size: 21px; }
    .actions .btn { padding: 0 10px; white-space: nowrap; }
  }
  @media (max-width: 900px) {
    .app { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr) auto; grid-template-areas: "top" "main" "side"; }
    .topbar { flex-wrap: wrap; padding: 10px 16px; gap: 8px 12px; }
    .reach { display: none; }
    .sidebar { position: sticky; bottom: 0; padding: 4px 6px; border-right: 0; border-top: 1px solid var(--line); z-index: 5; }
    .nav { flex-direction: row; justify-content: space-around; }
    .nav button { flex-direction: column; gap: 2px; min-height: 56px; padding: 6px 4px; font-size: 11px; justify-content: center; text-align: center; }
    main { padding: 16px; }
    .live-grid { grid-template-columns: minmax(0, 1fr); gap: 16px; }
    .live-grid > * { min-width: 0; }
    .stat-label { font-size: 12px; }
    .live-main, .live-side { display: contents; }
    .sign { order: 1; } .stats { order: 2; } .feed { order: 3; } .toolbar { order: 4; }
    .card-dets { order: 5; } .control-card { order: 6; } .card-photos { order: 7; } .card-log { order: 8; }
    .stat { padding: 12px; }
    .stat-value { font-size: 20px; margin-top: 8px; }
    .about dl { grid-template-columns: minmax(0, 1fr); gap: 2px; }
    .about dd { margin-bottom: 10px; }
  }
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-camera" viewBox="0 0 24 24"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z"/><circle cx="12" cy="13" r="3.2"/></symbol>
  <symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/></symbol>
  <symbol id="i-gear" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></symbol>
  <symbol id="i-video" viewBox="0 0 24 24"><path d="m22 8-6 4 6 4z"/><rect x="2" y="6" width="14" height="12" rx="2"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></symbol>
  <symbol id="i-max" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></symbol>
  <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6M9 6V4h6v2"/></symbol>
  <symbol id="i-sound" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></symbol>
  <symbol id="i-radar" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="5"/><path d="M12 12 19 5"/></symbol>
  <symbol id="i-pin" viewBox="0 0 24 24"><path d="M12 22s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/></symbol>
</svg>

<div class="app">
  <header class="topbar">
    <div class="logo">
      <svg viewBox="0 0 40 28" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
        <ellipse cx="7" cy="5" rx="6" ry="2"/><ellipse cx="33" cy="5" rx="6" ry="2"/>
        <path d="M7 7v3h26V7M13 10l3 6h8l3-6M16 16l-2 6M24 16l2 6"/><circle cx="20" cy="13" r="1.6" fill="currentColor"/>
      </svg>
      Panel Drone
    </div>
    <span class="online" id="online" data-ok="true">Online</span>
    <p class="reach">Buka dari HP: <a id="panelUrl" href="#">memuat…</a></p>
  </header>

  <nav class="sidebar" aria-label="Menu">
    <div class="nav">
      <button data-view="live" aria-current="page"><svg class="i"><use href="#i-camera"/></svg>Live View</button>
      <button data-view="photos"><svg class="i"><use href="#i-image"/></svg>Riwayat Foto</button>
      <button data-view="settings"><svg class="i"><use href="#i-gear"/></svg>Pengaturan</button>
      <button data-view="log"><svg class="i"><use href="#i-file"/></svg>Log Detektor</button>
      <button data-view="about"><svg class="i"><use href="#i-info"/></svg>Tentang</button>
    </div>
  </nav>

  <main>
    <!-- ================= LIVE VIEW ================= -->
    <section class="view" id="view-live" aria-label="Live View">
      <div class="live-grid">
        <div class="live-main">
          <div class="feed" id="liveBox">
            <img id="liveImg" alt="Video langsung dari kamera">
            <div class="feed-empty" id="livePlaceholder">
              <svg class="i"><use href="#i-camera"/></svg>
              <strong id="phTitle">Kamera mati</strong>
              <span id="phSub">Tekan Mulai untuk menyalakan kamera.</span>
            </div>
            <span class="chip chip-live" id="liveChip" data-on="false">Live Feed</span>
            <div class="feed-tools">
              <button class="icon-btn" id="btnSoundQuick" aria-pressed="false" aria-label="Bunyikan saat ada pelanggaran" title="Bunyikan saat ada pelanggaran" onclick="toggleSound()"><svg class="i"><use href="#i-sound"/></svg></button>
              <button class="icon-btn" aria-label="Layar penuh" title="Layar penuh" onclick="fullscreen()"><svg class="i"><use href="#i-max"/></svg></button>
            </div>
            <span class="chip chip-cam"><svg class="i" style="width:18px;height:18px"><use href="#i-eye"/></svg><span id="camName">Kamera 0</span><span class="sep"></span><span class="dot" id="camDot" data-on="false"></span><span id="camFps">–</span></span>
            <span class="chip-gps" id="gpsChip" hidden><svg class="i" style="width:16px;height:16px"><use href="#i-pin"/></svg><span id="gpsChipText"></span></span>
          </div>

          <div class="toolbar">
            <button class="btn" id="btnCapture" onclick="act('capture', {}, true)"><svg class="i"><use href="#i-camera"/></svg>Ambil Foto</button>
            <button class="btn" id="btnRecord" aria-pressed="false" onclick="act('record', {}, true)"><svg class="i"><use href="#i-video"/></svg><span id="recText">Rekam Video</span></button>
            <button class="btn" onclick="fullscreen()"><svg class="i"><use href="#i-max"/></svg>Layar Penuh</button>
          </div>

          <div class="card card-photos">
            <div class="card-head"><svg class="i"><use href="#i-image"/></svg>Foto Terbaru<button class="link" onclick="showView('photos')">Lihat Semua &rarr;</button></div>
            <div class="strip" id="strip"></div>
            <p class="empty-note" id="stripEmpty">Belum ada foto. Tekan Ambil Foto, atau tunggu sampai ada pelanggaran.</p>
          </div>

          <div class="card card-log">
            <div class="card-head"><svg class="i"><use href="#i-shield"/></svg>Log Detektor<button class="link" onclick="showView('log')">Lihat Semua &rarr;</button></div>
            <pre class="log log-short" id="logShort">Belum ada pesan.</pre>
          </div>
        </div>

        <aside class="live-side" aria-label="Status dan kendali">
          <div class="sign" id="sign" data-state="stopped" data-kind="smoke" role="status" aria-live="polite">
            <svg class="sign-mark" viewBox="0 0 100 100" aria-hidden="true">
              <circle class="sign-disc" cx="50" cy="50" r="42"/>
              <g class="pict-cig">
                <rect class="pict" x="20" y="52" width="46" height="10"/>
                <rect class="pict" x="68" y="52" width="10" height="10"/>
                <path class="pict-smoke" d="M73 46c-5-5 5-8 0-14"/>
              </g>
              <g class="pict-cam">
                <rect class="pict" x="24" y="38" width="42" height="28" rx="3"/>
                <path class="pict" d="M68 46l12-7v26l-12-7z"/>
              </g>
              <circle class="sign-ring" cx="50" cy="50" r="42"/>
              <line class="sign-slash" x1="21" y1="21" x2="79" y2="79"/>
            </svg>
            <div>
              <p class="sign-title" id="signTitle">Detektor mati</p>
              <p class="sign-sub" id="signSub">Tekan Mulai untuk menyalakan kamera.</p>
            </div>
          </div>

          <div class="stats">
            <div class="stat"><p class="stat-label"><svg class="i"><use href="#i-video"/></svg>Kecepatan</p><p class="stat-value" id="sFps">–</p><p class="stat-sub" id="sFpsSub"></p></div>
            <div class="stat"><p class="stat-label"><svg class="i"><use href="#i-clock"/></svg>Waktu jalan</p><p class="stat-value" id="sUptime">–</p></div>
            <div class="stat"><p class="stat-label"><svg class="i"><use href="#i-shield"/></svg><span id="sExtraLabel">Pelanggaran</span></p><p class="stat-value" id="sExtra">–</p></div>
          </div>

          <div class="card gps-card">
            <div class="card-head"><svg class="i"><use href="#i-pin"/></svg>Lokasi GPS<span class="gps-dot" id="gpsDot" data-on="false" style="margin-left:auto"></span></div>
            <div class="gps-row">
              <span class="gps-coord" id="gpsCoord">–</span>
              <a class="gps-maps" id="gpsMaps" target="_blank" rel="noopener" hidden>Buka Maps &rarr;</a>
              <p class="gps-meta" id="gpsMeta">Nyalakan detektor untuk membaca GPS.</p>
            </div>
            <div class="gps-last" id="gpsLast" hidden>
              <p class="gps-last-label" id="gpsLastLabel">Lokasi pelanggaran terakhir</p>
              <div class="gps-row">
                <span class="gps-coord" id="gpsLastCoord">–</span>
                <a class="gps-maps" id="gpsLastMaps" target="_blank" rel="noopener" hidden>Buka Maps &rarr;</a>
              </div>
            </div>
          </div>

          <div class="card control-card">
            <div class="card-head"><svg class="i"><use href="#i-sliders"/></svg>Kendali</div>
            <div class="field">
              <label for="mode">Mode</label>
              <select id="mode" onchange="saveSettings(this)"></select>
            </div>
            <div class="field">
              <label for="source">Kamera</label>
              <select id="source" onchange="saveSettings(this)">
                <option value="0">Kamera 0 (kamera drone atau webcam)</option>
                <option value="1">Kamera 1 (kamera USB kedua)</option>
                <option value="phone">Kamera HP (IP Webcam)</option>
              </select>
            </div>
            <div class="field" id="phoneBox">
              <label for="phoneUrl">Alamat kamera HP</label>
              <div class="row">
                <input type="text" id="phoneUrl" placeholder="http://192.168.x.x:8080/video" onchange="saveSettings(this)">
                <button class="btn" onclick="testCamera('phoneUrl')">Tes</button>
              </div>
            </div>
            <label class="switch"><input type="checkbox" role="switch" id="autostart" onchange="saveSettings(this)"> Nyalakan detektor otomatis saat panel dibuka</label>
            <p class="hint">Perubahan mode atau kamera berlaku setelah Mulai ulang.</p>
            <div class="actions">
              <button class="btn btn-primary" id="btnPower" onclick="togglePower()"><svg class="i"><use href="#i-play" id="powerIcon"/></svg><span id="powerText">Mulai</span></button>
              <button class="btn" id="btnRestart" onclick="act('restart')"><svg class="i"><use href="#i-refresh"/></svg>Mulai ulang</button>
            </div>
            <p class="msg" id="msg" role="status" aria-live="polite"></p>
          </div>
          <div class="card card-dets">
            <div class="card-head"><svg class="i"><use href="#i-radar"/></svg>Terlihat Sekarang</div>
            <ul class="dets" id="detList"><li class="empty">Nyalakan detektor untuk melihat deteksi.</li></ul>
            <p class="ai-verdict" id="aiVerdict" hidden></p>
          </div>

        </aside>
      </div>
    </section>

    <!-- ================= RIWAYAT FOTO ================= -->
    <section class="view" id="view-photos" aria-label="Riwayat Foto" hidden>
      <h1 class="view-title">Riwayat Foto</h1>
      <div class="filters" role="group" aria-label="Saring">
        <button data-filter="all" aria-pressed="true">Semua</button>
        <button data-filter="bukti" aria-pressed="false">Bukti pelanggaran</button>
        <button data-filter="foto" aria-pressed="false">Foto</button>
        <button data-filter="video" aria-pressed="false">Video</button>
      </div>
      <div class="gallery" id="gallery"></div>
      <p class="empty-note" id="galleryEmpty" hidden>Tidak ada file untuk pilihan ini.</p>
    </section>

    <!-- ================= PENGATURAN ================= -->
    <section class="view" id="view-settings" aria-label="Pengaturan" hidden>
      <h1 class="view-title">Pengaturan</h1>
      <div class="settings-wrap">
        <div class="card">
          <div class="card-head"><svg class="i"><use href="#i-sliders"/></svg>Deteksi</div>
          <div class="field">
            <label for="mode2">Mode</label>
            <select id="mode2" onchange="saveSettings(this)"></select>
          </div>
          <div class="field">
            <label for="source2">Kamera</label>
            <select id="source2" onchange="saveSettings(this)">
              <option value="0">Kamera 0 (kamera drone atau webcam)</option>
              <option value="1">Kamera 1 (kamera USB kedua)</option>
              <option value="phone">Kamera HP (IP Webcam)</option>
            </select>
          </div>
          <div class="field">
            <label for="phoneUrl2">Alamat kamera HP</label>
            <div class="row">
              <input type="text" id="phoneUrl2" placeholder="http://192.168.x.x:8080/video" onchange="saveSettings(this)">
              <button class="btn" onclick="testCamera('phoneUrl2')">Tes kamera HP</button>
            </div>
            <p class="hint">Dari aplikasi IP Webcam di HP, akhiri dengan /video.</p>
          </div>
          <div class="field">
            <label>Kamera di perangkat ini</label>
            <button class="btn" id="btnScan" onclick="scanCameras()"><svg class="i"><use href="#i-camera"/></svg>Cari kamera</button>
            <p class="hint">Mencari webcam bawaan dan kamera USB (Kamera 0 sampai 3). Hentikan detektor dulu, karena kamera yang sedang dipakai tidak bisa dicari.</p>
          </div>
          <label class="switch"><input type="checkbox" role="switch" id="autostart2" onchange="saveSettings(this)"> Nyalakan detektor otomatis saat panel dibuka</label>
          <p class="hint">Perubahan mode atau kamera berlaku setelah Mulai ulang.</p>
        </div>
        <div class="card">
          <div class="card-head"><svg class="i"><use href="#i-sound"/></svg>Peringatan di perangkat ini</div>
          <label class="switch"><input type="checkbox" role="switch" id="soundSwitch" onchange="setSound(this.checked)"> Bunyikan saat ada pelanggaran</label>
          <p class="hint">Berlaku untuk browser ini saja. Peringatan Telegram, WhatsApp, dan GCS diatur di file alert_config.json.</p>
        </div>
        <p class="msg" id="msg2" role="status" aria-live="polite"></p>
      </div>
    </section>

    <!-- ================= LOG ================= -->
    <section class="view" id="view-log" aria-label="Log Detektor" hidden>
      <h1 class="view-title">Log Detektor</h1>
      <pre class="log log-full" id="logFull">Belum ada pesan.</pre>
    </section>

    <!-- ================= TENTANG ================= -->
    <section class="view" id="view-about" aria-label="Tentang" hidden>
      <h1 class="view-title">Tentang</h1>
      <div class="about">
        <div class="card">
          <p>Panel ini mengendalikan kamera drone atau kamera tetap yang mendeteksi orang merokok dan memakai vape di area larangan. Pelanggaran dicatat dengan foto bukti dan dikirim sebagai peringatan.</p>
          <p>Rokok, vape, atau asap hanya dihitung bila berada di dekat orang dan terlihat di 3 dari 5 frame berturut-turut.</p>
        </div>
        <div class="card">
          <dl>
            <dt>Alamat panel</dt><dd><ul class="addr-list" id="aboutUrls"><li>–</li></ul></dd>
            <dt>Gemini</dt><dd id="aboutGemini">–</dd>
            <dt>Video langsung</dt><dd id="aboutStream">–</dd>
            <dt>Model orang</dt><dd>YOLO11n (COCO, kelas person)</dd>
            <dt>Model pelanggaran</dt><dd>smoking_vape.pt (YOLO11n, kelas rokok, vape, asap)</dd>
            <dt>Mode tersedia</dt><dd id="aboutModes">–</dd>
            <dt>Panduan</dt><dd>README.md di folder program</dd>
          </dl>
        </div>
      </div>
    </section>
  </main>
</div>

<dialog class="viewer" id="viewer" onclick="if (event.target === this) this.close()" onclose="$('viewerVideo').pause()">
  <img id="viewerImg" alt="">
  <video id="viewerVideo" controls playsinline preload="metadata" hidden></video>
  <p class="viewer-note" id="viewerNote" hidden>Rekaman ini memakai format lama (MP4) yang tidak bisa diputar di browser. Unduh lalu buka dengan pemutar video seperti VLC. Rekaman baru otomatis memakai format yang bisa diputar.</p>
  <div class="viewer-bar">
    <button class="btn btn-primary" id="viewerDownload"><svg class="i"><use href="#i-download"/></svg>Unduh</button>
    <button class="btn" onclick="document.getElementById('viewer').close()">Tutup</button>
  </div>
</dialog>

<script>
const $ = id => document.getElementById(id);
const LABELS = {rokok: "rokok", vape: "vape", asap: "asap", memegang: "memegang rokok/vape"};
let streamOn = false, soundOn = false, lastAlert = 0, streamPort = 8080, galleryKey = "", prevSign = "";
let running = false, mediaItems = [], filter = "all", lastStatus = null;

/* ---------- navigasi ---------- */
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.hidden = v.id !== "view-" + name);
  document.querySelectorAll(".nav button").forEach(b => b.toggleAttribute("aria-current", b.dataset.view === name));
  document.querySelectorAll(".nav button[aria-current]").forEach(b => b.setAttribute("aria-current", "page"));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  if (name === "photos") renderGallery();
}
document.querySelectorAll(".nav button").forEach(b => b.onclick = () => showView(b.dataset.view));
document.querySelectorAll(".filters button").forEach(b => b.onclick = () => {
  filter = b.dataset.filter;
  document.querySelectorAll(".filters button").forEach(x => x.setAttribute("aria-pressed", x === b));
  renderGallery();
});

function showMsg(ok, text) {
  ["msg", "msg2"].forEach(id => {
    const m = $(id);
    m.textContent = text;
    m.dataset.tone = ok ? "ok" : "err";
  });
  clearTimeout(showMsg.t);
  showMsg.t = setTimeout(() => ["msg", "msg2"].forEach(id => { $(id).textContent = ""; delete $(id).dataset.tone; }), 6000);
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {cache: "no-store"} :
    {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}

async function act(name, body = {}, refreshGallery = false) {
  ["btnPower", "btnRestart", "btnCapture", "btnRecord"].forEach(id => $(id).disabled = true);
  try {
    const r = await api("/api/" + name, body);
    showMsg(r.ok, r.message);
    if (refreshGallery) loadGallery(true);
  } catch (e) { showMsg(false, "Panel tidak merespons. Pastikan jendela panel masih terbuka."); }
  await refresh();
}
function togglePower() { act(running ? "stop" : "start"); }

function fmtTime(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), d = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(d).padStart(2, "0");
}
const fmtNum = (x, digits) => x.toLocaleString("id-ID", {minimumFractionDigits: digits, maximumFractionDigits: digits});

/* ---------- video ---------- */
function setPlaceholder(title, sub) { $("phTitle").textContent = title; $("phSub").textContent = sub; }
function setStream(on) {
  if (on === streamOn) return;
  streamOn = on;
  const img = $("liveImg");
  if (on) {
    img.src = location.protocol + "//" + location.hostname + ":" + streamPort + "/stream.mjpg?t=" + Date.now();
  } else {
    img.removeAttribute("src");
    img.dataset.live = "false";
  }
  $("livePlaceholder").hidden = false;
}
$("liveImg").onload = () => { $("liveImg").dataset.live = "true"; $("livePlaceholder").hidden = true; };
$("liveImg").onerror = () => {
  $("liveImg").dataset.live = "false";
  $("livePlaceholder").hidden = false;
  if (streamOn) { streamOn = false; setTimeout(refresh, 2000); }
};

/* ---------- pengaturan (dua salinan: Kendali & Pengaturan) ---------- */
const PAIRS = {mode: ["mode", "mode2"], source: ["source", "source2"], phone: ["phoneUrl", "phoneUrl2"], auto: ["autostart", "autostart2"]};
function fillModes(sel, modes) {
  sel.innerHTML = "";
  for (const [key, m] of Object.entries(modes)) {
    const o = document.createElement("option");
    o.value = key;
    o.textContent = m.label + (m.available ? "" : " (perlu " + m.missing.join(", ") + ")");
    o.disabled = !m.available;
    sel.appendChild(o);
  }
}
function applySettings(s, modes) {
  const key = JSON.stringify(modes);
  PAIRS.mode.forEach(id => {
    const sel = $(id);
    if (sel.dataset.key !== key) { sel.dataset.key = key; fillModes(sel, modes); }
    if (document.activeElement !== sel) sel.value = s.mode;
  });
  PAIRS.source.forEach(id => { if (document.activeElement !== $(id)) $(id).value = s.source_type; });
  PAIRS.phone.forEach(id => { if (document.activeElement !== $(id)) $(id).value = s.phone_url; });
  PAIRS.auto.forEach(id => $(id).checked = s.autostart);
  $("phoneBox").hidden = s.source_type !== "phone";
}
async function saveSettings(el) {
  // Ambil nilai dari elemen yang baru diubah, lalu samakan salinannya
  const pick = (pair, prop) => { const src = pair.includes(el.id) ? $(el.id) : $(pair[0]); pair.forEach(id => $(id)[prop] = src[prop]); return src[prop]; };
  const body = {mode: pick(PAIRS.mode, "value"), source_type: pick(PAIRS.source, "value"),
                phone_url: pick(PAIRS.phone, "value").trim(), autostart: pick(PAIRS.auto, "checked")};
  $("phoneBox").hidden = body.source_type !== "phone";
  const r = await api("/api/settings", body);
  showMsg(r.ok, running ? "Pengaturan disimpan. Tekan Mulai ulang agar berlaku." : r.message);
}
async function testCamera(inputId) {
  showMsg(true, "Mengetes kamera HP…");
  const r = await api("/api/test-camera", {url: $(inputId).value.trim()});
  showMsg(r.ok, r.message);
}

async function scanCameras() {
  $("btnScan").disabled = true;
  showMsg(true, "Mencari kamera…");
  try {
    const r = await api("/api/scan-cameras", {});
    showMsg(r.ok, r.message);
    if (r.ok && r.cameras.length) {
      // Isi ulang pilihan Kamera dengan kamera yang benar-benar ada di perangkat ini
      ["source", "source2"].forEach(id => {
        const sel = $(id), current = sel.value;
        sel.innerHTML = "";
        r.cameras.forEach(c => {
          const o = document.createElement("option");
          o.value = String(c.index);
          o.textContent = "Kamera " + c.index + " (" + c.size + ")" + (c.index === 0 ? " - webcam bawaan" : "");
          sel.appendChild(o);
        });
        const o = document.createElement("option");
        o.value = "phone"; o.textContent = "Kamera HP (IP Webcam)";
        sel.appendChild(o);
        sel.value = [...sel.options].some(x => x.value === current) ? current : String(r.cameras[0].index);
      });
      if ($("source").value !== (lastStatus && lastStatus.settings.source_type)) saveSettings($("source"));
    }
  } catch (e) { showMsg(false, "Gagal mencari kamera."); }
  $("btnScan").disabled = false;
}

/* ---------- suara ---------- */
function beep() {
  try {
    const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
    [0, 0.35, 0.7].forEach(t => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.3, ctx.currentTime + t);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.25);
    });
  } catch (e) {}
}
function setSound(on) {
  soundOn = on;
  $("btnSoundQuick").setAttribute("aria-pressed", on);
  $("soundSwitch").checked = on;
  if (on) beep();
}
function toggleSound() { setSound(!soundOn); }
function fullscreen() {
  const el = $("liveBox");
  (el.requestFullscreen || el.webkitRequestFullscreen || (() => {})).call(el);
}

/* ---------- status ---------- */
function renderSign(s) {
  const d = s.detector, live = s.live, sign = $("sign");
  const mode = d.mode || s.settings.mode;
  const smoking = mode && (mode === "ai" || mode.startsWith("smoking"));
  let state, title, sub;
  if (d.state === "running") {
    if (live && live.alert && live.alert.active) {
      state = "alert";
      title = "Terdeteksi merokok";
      const last = live.alert.last || {};
      const names = [...new Set((last.objects || []).map(o => LABELS[o["class"]] || o["class"]))];
      const time = new Date((last.timestamp || live.alert.last_time) * 1000).toLocaleTimeString("id-ID", {hour: "2-digit", minute: "2-digit"});
      sub = (names.length ? names.join(" dan ") : "Pelanggaran") + " pukul " + time + ". "
          + (last.location ? "Lokasi: " + last.location.lat.toFixed(6) + ", " + last.location.lon.toFixed(6) + ". " : "")
          + (last.reason ? "AI: " + last.reason : "Foto bukti sudah disimpan.");
    } else if (!live) {
      state = "loading";
      title = "Menyalakan kamera";
      sub = "Sebentar lagi gambar muncul.";
    } else if (live.phase === "camera") {
      state = "loading";
      title = "Membuka kamera";
      sub = "Menunggu gambar pertama dari kamera.";
    } else if (live.phase === "loading") {
      state = "loading";
      title = "Kamera menyala";
      sub = "AI sedang disiapkan beberapa detik. Video sudah bisa dilihat.";
    } else {
      state = "running";
      title = smoking ? "Area aman" : "Menyala";
      const ai = live.ai;
      sub = mode === "smoking_demo" ? "Mode demo. HP di tangan dianggap rokok."
          : mode === "ai" ? (ai && ai.active && !ai.status.startsWith("Batas")
              ? "Tidak ada pelanggaran. Gemini memastikan setiap kali ada yang mencurigakan."
              : "Tidak ada pelanggaran. Gemini tidak aktif (" + (ai ? ai.status : "-") + "), memakai deteksi lokal.")
          : smoking ? "Kamera menyala. Tidak ada rokok, vape, atau asap di dekat orang."
          : "Mengirim video dan deteksi ke GCS.";
    }
  } else if (d.state === "error") {
    state = "error";
    title = "Detektor berhenti";
    sub = "Terjadi masalah (kode " + d.exit_code + "). Buka Log Detektor.";
  } else {
    state = "stopped";
    title = "Detektor mati";
    sub = "Tekan Mulai untuk menyalakan kamera.";
  }
  sign.dataset.kind = smoking || d.state !== "running" ? "smoke" : "cam";
  sign.dataset.state = state;
  $("signTitle").textContent = title;
  $("signSub").textContent = sub;
  if (state === "alert" && prevSign !== "alert") {
    sign.classList.remove("is-stamped"); void sign.offsetWidth; sign.classList.add("is-stamped");
  }
  prevSign = state;

  if ($("liveImg").dataset.live !== "true") {
    if (d.state !== "running") setPlaceholder("Kamera mati", "Tekan Mulai untuk menyalakan kamera.");
    else if (!live) setPlaceholder("Menyalakan kamera…", "Sebentar lagi gambar muncul.");
    else if (live.phase === "camera") setPlaceholder("Membuka kamera…", "Menunggu gambar pertama dari kamera.");
    else setPlaceholder("Menyambungkan video…", "Gambar akan muncul dalam beberapa detik.");
  }
}

function renderVerdict(s) {
  const box = $("aiVerdict"), ai = s.live && s.live.ai;
  box.hidden = !ai;
  if (!ai) return;
  const v = ai.last_verdict;
  if (!v) { box.textContent = "Gemini: " + ai.status + ". Belum ada pemeriksaan."; return; }
  const found = ["merokok", "vape", "asap", "memegang"].filter(k => v[k]).map(k => k === "memegang" ? "memegang rokok/vape" : k);
  const time = new Date(v.time * 1000).toLocaleTimeString("id-ID", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  box.dataset.yes = found.length > 0;
  box.textContent = "Pemeriksaan Gemini pukul " + time + ": " + (found.length ? found.join(", ") : "tidak ada pelanggaran")
    + ". " + (v.alasan || "");
}

function renderDetections(s) {
  const list = $("detList"), live = s.live;
  renderVerdict(s);
  if (s.detector.state !== "running" || !live || !live.latest) {
    list.innerHTML = '<li class="empty">' + (s.detector.state === "running" ? "Menunggu gambar pertama." : "Nyalakan detektor untuk melihat deteksi.") + "</li>";
    return;
  }
  const dets = live.latest.detections || [];
  if (!dets.length) { list.innerHTML = '<li class="empty">Tidak ada objek di depan kamera.</li>'; return; }
  list.innerHTML = "";
  dets.slice().sort((a, b) => b.confidence - a.confidence).slice(0, 6).forEach(x => {
    const li = document.createElement("li");
    if (x.violation) { li.className = "is-violation"; li.title = "Melanggar: berada di dekat orang"; }
    else if (x.candidate !== undefined) { li.title = "Kandidat dari model lokal, Gemini yang memastikan"; }
    const pct = Math.round(x.confidence * 100);
    li.innerHTML = '<span class="det-name"></span><span class="det-bar"><i></i></span><span class="det-pct"></span>';
    li.querySelector(".det-name").textContent = (LABELS[x["class"]] || x["class"]) + (x.candidate !== undefined ? "?" : "");
    li.querySelector(".det-bar i").style.width = pct + "%";
    li.querySelector(".det-pct").textContent = pct + "%";
    list.appendChild(li);
  });
}

function fmtCoord(loc) { return loc.lat.toFixed(6) + ", " + loc.lon.toFixed(6); }
function mapsUrl(loc) { return "https://maps.google.com/?q=" + loc.lat.toFixed(7) + "," + loc.lon.toFixed(7); }
const GPS_SOURCES = {mavlink: "flight controller (MAVLink)", ipwebcam: "GPS HP (IP Webcam)", nmea: "modul GPS", static: "koordinat tetap"};

function renderGps(s) {
  const live = s.live, gps = live && live.gps, running = s.detector.state === "running";
  const fix = running && gps ? gps.fix : null;
  $("gpsDot").dataset.on = !!fix;
  $("gpsChip").hidden = !fix;
  if (fix) {
    $("gpsCoord").textContent = fmtCoord(fix);
    $("gpsChipText").textContent = fmtCoord(fix);
    $("gpsMaps").href = mapsUrl(fix);
    $("gpsMaps").hidden = false;
    const meta = ["Sumber: " + (GPS_SOURCES[fix.source] || fix.source)];
    if (fix.alt !== null && fix.alt !== undefined) meta.push("ketinggian " + Math.round(fix.alt) + " m");
    if (fix.accuracy !== null && fix.accuracy !== undefined) meta.push("±" + Math.round(fix.accuracy) + " m");
    if (fix.source !== "static" && fix.age_s !== undefined) meta.push(fix.age_s < 5 ? "baru saja" : Math.round(fix.age_s) + " dtk lalu");
    $("gpsMeta").textContent = meta.join(" · ");
  } else {
    $("gpsCoord").textContent = "–";
    $("gpsMaps").hidden = true;
    $("gpsMeta").textContent = !running ? "Nyalakan detektor untuk membaca GPS."
      : !live ? "Menunggu detektor…"
      : !gps ? "GPS tidak aktif untuk mode ini (atur bagian gps di alert_config.json)."
      : "Belum ada sinyal dari " + (GPS_SOURCES[gps.source] || gps.source) + (gps.error ? ": " + gps.error : ".");
  }
  // Lokasi pelanggaran terakhir (tetap tampil setelah banner peringatan hilang)
  const last = live && live.alert && live.alert.last;
  const loc = last && last.location;
  $("gpsLast").hidden = !last;
  if (last) {
    const time = new Date(last.timestamp * 1000).toLocaleTimeString("id-ID", {hour: "2-digit", minute: "2-digit"});
    $("gpsLastLabel").textContent = "Lokasi pelanggaran terakhir (" + time + ")";
    $("gpsLastCoord").textContent = loc ? fmtCoord(loc) : "tidak tersedia (GPS belum dapat sinyal)";
    $("gpsLastMaps").hidden = !loc;
    if (loc) $("gpsLastMaps").href = mapsUrl(loc);
  }
}

async function refresh() {
  let s;
  try { s = await api("/api/status"); } catch (e) {
    $("online").dataset.ok = "false"; $("online").textContent = "Terputus";
    $("signTitle").textContent = "Panel terputus";
    $("signSub").textContent = "Jendela panel di laptop mungkin tertutup. Buka lagi Mulai Panel.bat.";
    $("sign").dataset.state = "error";
    return;
  }
  lastStatus = s;
  $("online").dataset.ok = "true"; $("online").textContent = "Online";
  const d = s.detector, live = s.live, rec = s.recording;
  running = d.state === "running";
  streamPort = s.stream_port;

  $("btnPower").disabled = false;
  $("btnPower").dataset.running = running;
  $("powerText").textContent = running ? "Hentikan" : "Mulai";
  $("powerIcon").setAttribute("href", running ? "#i-stop" : "#i-play");
  $("btnRestart").disabled = false;
  $("btnCapture").disabled = !running;
  $("btnRecord").disabled = !running && !rec.active;
  $("btnRecord").setAttribute("aria-pressed", rec.active);
  $("recText").textContent = rec.active ? "Stop Rekam " + fmtTime(rec.elapsed_s) : "Rekam Video";

  const hasFrame = $("liveImg").dataset.live === "true";
  $("liveChip").dataset.on = running && hasFrame;
  $("liveChip").textContent = rec.active ? "REC " + fmtTime(rec.elapsed_s) : !running ? "Kamera mati" : hasFrame ? "Live Feed" : "Menyambungkan";
  const src = d.source || (s.settings.source_type === "phone" ? "phone" : s.settings.source_type);
  $("camName").textContent = String(src).startsWith("http") || src === "phone" ? "Kamera HP" : "Kamera " + src;
  $("camDot").dataset.on = running && live && live.stats.camera_ok;
  $("camFps").textContent = live ? fmtNum(live.stats.fps, 1) + " FPS" : "–";

  $("panelUrl").textContent = s.panel_url; $("panelUrl").href = s.panel_url;
  const urls = s.panel_urls || [s.panel_url];
  const urlKey = urls.join("|");
  if ($("aboutUrls").dataset.key !== urlKey) {
    $("aboutUrls").dataset.key = urlKey;
    $("aboutUrls").innerHTML = "";
    urls.forEach((u, i) => {
      const li = document.createElement("li");
      const a = document.createElement("a"); a.href = u; a.textContent = u;
      li.appendChild(a);
      if (i === 0) { const sm = document.createElement("small"); sm.textContent = "jaringan aktif sekarang"; li.appendChild(sm); }
      $("aboutUrls").appendChild(li);
    });
  }
  const ai = live && live.ai;
  $("aboutGemini").textContent = ai
    ? ai.status + " · pemakaian hari ini " + ai.calls_today + " dari " + ai.max_per_day + " (batas lokal, tetap gratis)"
    : "Dipakai di mode Deteksi AI. Nyalakan mode itu untuk melihat statusnya.";
  $("aboutStream").textContent = location.protocol + "//" + location.hostname + ":" + s.stream_port + "/stream.mjpg";
  $("aboutModes").textContent = Object.values(s.modes).map(m => m.label + (m.available ? "" : " (belum tersedia)")).join(", ");

  applySettings(s.settings, s.modes);
  setStream(running);
  renderSign(s);
  renderDetections(s);
  renderGps(s);

  $("sUptime").textContent = running ? fmtTime(d.uptime_s) : "–";
  $("sFps").textContent = live ? fmtNum(live.stats.fps, 0) + " FPS" : "–";
  $("sFpsSub").textContent = live && live.stats.detect_fps ? "AI " + fmtNum(live.stats.detect_fps, 1) + "× per detik" : "";
  if (live && live.alerts_total !== undefined) {
    $("sExtraLabel").textContent = "Pelanggaran";
    $("sExtra").textContent = live.alerts_total;
  } else if (live) {
    $("sExtraLabel").textContent = "Terkirim ke GCS";
    $("sExtra").textContent = live.stats.video_sent;
  } else {
    $("sExtra").textContent = "–";
  }
  if (live && live.alert && live.alert.last_time && live.alert.last_time !== lastAlert) {
    if (lastAlert && soundOn) beep();
    lastAlert = live.alert.last_time;
    loadGallery(true);
  }
}

async function refreshLog() {
  try {
    const r = await api("/api/log");
    const text = r.lines.join("\n") || "Belum ada pesan.";
    $("logShort").textContent = r.lines.slice(-4).join("\n") || "Belum ada pesan.";
    const pre = $("logFull"), atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = text;
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (e) {}
}

/* ---------- foto ---------- */
function shotEl(i, withFoot) {
  const url = "/media/" + i.folder + "/" + encodeURIComponent(i.name);
  const fig = document.createElement("figure");
  fig.className = "shot" + (i.folder === "bukti" ? " is-evidence" : "");
  const open = document.createElement("button");
  open.className = "shot-open";
  open.setAttribute("aria-label", (i.video ? "Putar video " : "Lihat foto ") + i.name);
  if (i.video) {
    if (i.playable) {
      // Cuplikan gambar dari detik awal video
      const v = document.createElement("video");
      v.muted = true; v.preload = "metadata"; v.src = url + "#t=0.5";
      open.appendChild(v);
    }
    open.insertAdjacentHTML("beforeend", '<span class="play" aria-hidden="true">&#9654;</span>');
    open.onclick = () => openViewer(url, i.name, i.playable ? "video" : "old-video");
  } else {
    const img = document.createElement("img");
    img.loading = "lazy"; img.src = url; img.alt = "";
    open.appendChild(img);
    open.onclick = () => openViewer(url, i.name);
  }
  const when = new Date(i.mtime * 1000).toLocaleString("id-ID", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = i.folder === "bukti" ? "Bukti" : i.video ? "Video" : "Foto";
  fig.append(open, badge);
  if (!withFoot) {
    const t = document.createElement("span"); t.className = "shot-time"; t.textContent = when; fig.appendChild(t);
    return fig;
  }
  const foot = document.createElement("div");
  foot.className = "shot-foot";
  const label = document.createElement("span"); label.textContent = when;
  const acts = document.createElement("span"); acts.className = "actions-mini";
  const dl = document.createElement("button");
  dl.innerHTML = '<svg class="i"><use href="#i-download"/></svg>'; dl.title = "Unduh"; dl.setAttribute("aria-label", "Unduh " + i.name);
  dl.onclick = () => location.href = url + "?download=1";
  const del = document.createElement("button");
  del.className = "del"; del.innerHTML = '<svg class="i"><use href="#i-trash"/></svg>'; del.title = "Hapus"; del.setAttribute("aria-label", "Hapus " + i.name);
  del.onclick = () => delMedia(i.folder, i.name);
  acts.append(dl, del); foot.append(label, acts); fig.appendChild(foot);
  return fig;
}

function renderGallery() {
  const items = mediaItems.filter(i => filter === "all" || (filter === "bukti" ? i.folder === "bukti"
    : filter === "video" ? i.video : i.folder === "captures" && !i.video));
  const g = $("gallery");
  g.innerHTML = "";
  items.forEach(i => g.appendChild(shotEl(i, true)));
  $("galleryEmpty").hidden = items.length > 0;
}

async function loadGallery(force) {
  const r = await api("/api/media");
  const key = r.items.map(i => i.folder + i.name).join("|");
  if (!force && key === galleryKey) return;
  galleryKey = key;
  mediaItems = r.items;
  const strip = $("strip");
  strip.innerHTML = "";
  r.items.slice(0, 10).forEach(i => strip.appendChild(shotEl(i, false)));
  $("stripEmpty").hidden = r.items.length > 0;
  renderGallery();
}

async function delMedia(folder, name) {
  if (!confirm("Hapus " + name + "? File tidak bisa dikembalikan.")) return;
  const r = await api("/api/delete", {folder, name});
  showMsg(r.ok, r.message);
  loadGallery(true);
}

function openViewer(url, name, kind = "image") {
  const img = $("viewerImg"), vid = $("viewerVideo");
  img.hidden = kind !== "image";
  vid.hidden = kind !== "video";
  $("viewerNote").hidden = kind !== "old-video";
  if (kind === "image") { img.src = url; img.alt = name; }
  if (kind === "video") { vid.src = url; vid.setAttribute("aria-label", name); }
  else { vid.removeAttribute("src"); vid.load(); }
  $("viewerDownload").onclick = () => location.href = url + "?download=1";
  $("viewer").showModal();
  if (kind === "video") vid.play().catch(() => {});
}

showView(["live", "photos", "settings", "log", "about"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "live");
refresh(); refreshLog(); loadGallery(true);
setInterval(refresh, 1000);
setInterval(refreshLog, 2000);
setInterval(() => loadGallery(false), 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- Main

def shutdown(*_):
    log.info("Panel dimatikan, menghentikan detektor...")
    if recorder.active():
        recorder.stop()
    if detector.running():
        detector.stop()
    raise SystemExit(0)


def main():
    global PANEL_PORT
    parser = argparse.ArgumentParser(description="Panel kontrol web untuk detektor drone")
    parser.add_argument("--port", type=int, default=PANEL_PORT, help="port panel. Default: %(default)s")
    parser.add_argument("--open-browser", action="store_true", help="buka browser otomatis")
    args = parser.parse_args()
    PANEL_PORT = args.port

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), PanelHandler)
    except OSError as e:
        log.error("Port %d sudah dipakai (%s). Mungkin panel sudah berjalan: buka http://localhost:%d",
                  PANEL_PORT, e, PANEL_PORT)
        if args.open_browser:
            webbrowser.open(f"http://localhost:{PANEL_PORT}")
        sys.exit(1)
    server.daemon_threads = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Panel siap: http://localhost:%d  |  dari HP: http://%s:%d", PANEL_PORT, local_ip(), PANEL_PORT)
    log.info("Tutup jendela ini (atau Ctrl+C) untuk mematikan panel dan detektor.")

    if settings.get("autostart"):
        ok, msg = detector.start(settings["mode"], source_from_settings(settings))
        log.info("Start otomatis: %s", msg)
    if args.open_browser:
        threading.Timer(1.0, webbrowser.open, args=(f"http://localhost:{PANEL_PORT}",)).start()

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
