import os
import cv2
import socket
import struct
import json
import time
import signal
import logging
import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GCS_IP = "10.42.0.1"
VIDEO_PORT = 5001
DETECTION_PORT = 5101
WIDTH = 640
HEIGHT = 480
JPEG_QUALITY = 60

DRONE_NAME = "companion-01"
DRONE_IP = "10.42.0.11"
CAMERA_INDEX = 0
MODEL_PATH = "yolo11n.pt"

# Sumber kamera: CAMERA_INDEX (kamera drone) secara default. Bisa diganti saat
# menjalankan dengan --source, mis. kamera HP lewat aplikasi IP Webcam:
#   python drone1_yolo_sender.py --source http://192.168.1.20:8080/video
CAMERA_SOURCE = CAMERA_INDEX

# Tampilan untuk handphone / browser (MJPEG lewat HTTP)
WEB_ENABLED = True
WEB_PORT = 8080
MAX_WEB_CLIENTS = 3

# Batas datagram UDP IPv4. Frame yang lebih besar dikompres ulang dengan
# kualitas lebih rendah supaya tidak gagal kirim (format paket tetap sama).
MAX_UDP_PAYLOAD = 65507
HEADER_FORMAT = "!II"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MIN_JPEG_QUALITY = 10
JPEG_QUALITY_STEP = 10

CAMERA_MAX_FAILS = 30        # frame gagal berturut-turut sebelum kamera dibuka ulang
CAMERA_RETRY_DELAY = 2.0     # detik
STATUS_LOG_INTERVAL = 5.0    # detik

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("drone1")

stop_event = threading.Event()


class SharedState:
    """Frame & deteksi terakhir, dibagi antara loop utama dan server web."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg = None
        self.seq = 0
        self.message = None
        self.started = time.time()
        self.stats = {
            "fps": 0.0,
            "camera_ok": False,
            "video_sent": 0,
            "video_send_errors": 0,
            "video_quality_reduced": 0,
            "video_skipped_too_large": 0,
            "detection_send_errors": 0,
            "web_clients": 0,
        }

    def publish(self, jpeg, message):
        with self.cond:
            self.jpeg = jpeg
            self.message = message
            self.seq += 1
            self.cond.notify_all()

    def wait_frame(self, last_seq, timeout=1.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or stop_event.is_set(), timeout)
            return self.jpeg, self.seq

    def snapshot(self):
        with self.cond:
            return {
                "drone": DRONE_NAME,
                "ip": DRONE_IP,
                "gcs_ip": GCS_IP,
                "uptime_s": round(time.time() - self.started, 1),
                "stats": dict(self.stats),
                "latest": self.message,
            }


state = SharedState()


# ---------------------------------------------------------------- Kamera

class StreamReader:
    """Membaca kamera jaringan (HP / IP camera) di thread terpisah dan hanya
    menyimpan frame terbaru, supaya video tidak makin tertinggal saat YOLO
    lebih lambat dari kamera."""

    def __init__(self, capture):
        self.capture = capture
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.last_seq = 0
        self.running = True
        self.thread = threading.Thread(target=self._run, name="stream-reader", daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            ok, frame = self.capture.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self.cond:
                self.frame = frame
                self.seq += 1
                self.cond.notify_all()

    def read(self, timeout=0.2):
        with self.cond:
            if not self.cond.wait_for(lambda: self.seq != self.last_seq, timeout):
                return False, None
            self.last_seq = self.seq
            return True, self.frame

    def get(self, prop):
        return self.capture.get(prop)

    def release(self):
        self.running = False
        self.thread.join(timeout=2)
        self.capture.release()


def _capture(source, is_stream):
    # Windows: DirectShow memberi frame pertama ±1 dtk (backend bawaan MSMF ±9 dtk).
    # Linux/drone tetap memakai backend bawaan.
    if not is_stream and os.name == "nt":
        camera = cv2.VideoCapture(source, cv2.CAP_DSHOW)
        if camera.isOpened():
            return camera
        camera.release()
    return cv2.VideoCapture(source)


def open_camera():
    is_stream = isinstance(CAMERA_SOURCE, str)
    while not stop_event.is_set():
        camera = _capture(CAMERA_SOURCE, is_stream)
        if camera.isOpened():
            if is_stream:
                camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            else:
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
            log.info("Kamera %s terbuka (%dx%d)", CAMERA_SOURCE,
                     int(camera.get(cv2.CAP_PROP_FRAME_WIDTH)),
                     int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            state.stats["camera_ok"] = True
            return StreamReader(camera) if is_stream else camera
        camera.release()
        state.stats["camera_ok"] = False
        log.warning("Kamera %s tidak bisa dibuka, coba lagi dalam %.0f detik",
                    CAMERA_SOURCE, CAMERA_RETRY_DELAY)
        stop_event.wait(CAMERA_RETRY_DELAY)
    return None


def fit_frame(frame):
    """Kecilkan frame agar muat dalam WIDTH x HEIGHT (rasio tetap), mis. dari HP 1920x1080."""
    h, w = frame.shape[:2]
    scale = min(WIDTH / w, HEIGHT / h)
    if scale >= 1:
        return frame
    return cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------- Video ke GCS

def encode_jpeg(image, quality):
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return encoded.tobytes() if ok else None


def fit_for_udp(image, jpeg):
    """Kembalikan JPEG yang muat dalam satu datagram UDP, atau None."""
    limit = MAX_UDP_PAYLOAD - HEADER_SIZE
    if jpeg is not None and len(jpeg) <= limit:
        return jpeg
    quality = JPEG_QUALITY - JPEG_QUALITY_STEP
    while quality >= MIN_JPEG_QUALITY:
        data = encode_jpeg(image, quality)
        if data is not None and len(data) <= limit:
            state.stats["video_quality_reduced"] += 1
            return data
        quality -= JPEG_QUALITY_STEP
    return None


def send_video(sock, frame_id, image, jpeg):
    data = fit_for_udp(image, jpeg)
    if data is None:
        state.stats["video_skipped_too_large"] += 1
        return
    header = struct.pack(HEADER_FORMAT, frame_id & 0xFFFFFFFF, len(data))
    try:
        sock.sendto(header + data, (GCS_IP, VIDEO_PORT))
        state.stats["video_sent"] += 1
    except OSError:
        # Wi-Fi putus / GCS belum siap: lewati frame ini, jangan hentikan drone
        state.stats["video_send_errors"] += 1


def send_detections(sock, message):
    try:
        sock.sendto(json.dumps(message).encode(), (GCS_IP, DETECTION_PORT))
    except OSError:
        state.stats["detection_send_errors"] += 1


# ---------------------------------------------------------------- Server web (HP)

INDEX_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Drone Live</title>
<style>
  body { margin: 0; background: #111; color: #eee; font-family: system-ui, sans-serif; }
  header { padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; }
  h1 { font-size: 16px; margin: 0; }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: #c33; display: inline-block; margin-right: 6px; }
  #dot.on { background: #3c3; }
  img { width: 100%; display: block; background: #000; min-height: 200px; }
  section { padding: 10px 14px; font-size: 14px; }
  table { width: 100%; border-collapse: collapse; }
  td, th { text-align: left; padding: 4px 2px; border-bottom: 1px solid #333; }
  .muted { color: #999; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1><span id="dot"></span><span id="name">Drone</span></h1>
  <span class="muted" id="fps">-</span>
</header>
<img id="video" src="/stream.mjpg" alt="Video drone">
<section>
  <div class="muted" id="frame">Menunggu data...</div>
  <table>
    <thead><tr><th>Objek</th><th>Conf</th><th>BBox</th></tr></thead>
    <tbody id="det"></tbody>
  </table>
  <p class="muted" id="stats"></p>
</section>
<script>
const video = document.getElementById("video");
video.onerror = () => setTimeout(() => { video.src = "/stream.mjpg?t=" + Date.now(); }, 2000);

async function poll() {
  try {
    const r = await fetch("/status.json", { cache: "no-store" });
    const s = await r.json();
    document.getElementById("dot").className = s.stats.camera_ok ? "on" : "";
    document.getElementById("name").textContent = s.drone + " (" + s.ip + ")";
    document.getElementById("fps").textContent = s.stats.fps.toFixed(1) + " FPS";
    const m = s.latest;
    const rows = [];
    if (m) {
      document.getElementById("frame").textContent =
        "Frame " + m.frame_id + " - " + new Date(m.timestamp * 1000).toLocaleTimeString() +
        " - " + m.detections.length + " deteksi";
      for (const d of m.detections) {
        rows.push("<tr><td>" + d["class"] + "</td><td>" + d.confidence.toFixed(2) +
                  "</td><td>" + d.bbox.join(", ") + "</td></tr>");
      }
    }
    document.getElementById("det").innerHTML = rows.join("");
    document.getElementById("stats").textContent =
      "Ke GCS " + s.gcs_ip + ": terkirim " + s.stats.video_sent +
      ", gagal " + s.stats.video_send_errors +
      ", kualitas diturunkan " + s.stats.video_quality_reduced +
      " | uptime " + Math.round(s.uptime_s) + " s";
  } catch (e) {
    document.getElementById("dot").className = "";
    document.getElementById("frame").textContent = "Koneksi ke drone terputus...";
  }
}
setInterval(poll, 500);
poll();
</script>
</body>
</html>
"""

web_slots = threading.BoundedSemaphore(MAX_WEB_CLIENTS)


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode())
        elif path == "/stream.mjpg":
            self._stream()
        elif path == "/status.json":
            self._send(200, "application/json", json.dumps(state.snapshot()).encode())
        elif path == "/snapshot.jpg":
            jpeg = state.jpeg
            if jpeg is None:
                self._send(503, "text/plain", b"Belum ada frame")
            else:
                self._send(200, "image/jpeg", jpeg)
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        if not web_slots.acquire(blocking=False):
            self._send(503, "text/plain", b"Terlalu banyak penonton")
            return
        state.stats["web_clients"] += 1
        log.info("Penonton web terhubung: %s", self.client_address[0])
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_seq = -1
            while not stop_event.is_set():
                jpeg, seq = state.wait_frame(last_seq)
                if jpeg is None or seq == last_seq:
                    continue
                last_seq = seq
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(jpeg))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (ConnectionError, TimeoutError):
            pass
        finally:
            state.stats["web_clients"] -= 1
            web_slots.release()
            log.info("Penonton web terputus: %s", self.client_address[0])

    def log_message(self, fmt, *args):
        log.debug("web %s - " + fmt, self.client_address[0], *args)


def start_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    except OSError as e:
        log.error("Server web gagal dibuka di port %d (%s). Lanjut tanpa tampilan HP.", WEB_PORT, e)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Tampilan HP/browser: http://%s:%d (IP perangkat ini: %s)", DRONE_IP, WEB_PORT, local_ip())
    return server


def local_ip():
    # connect() pada UDP tidak mengirim paket, hanya memilih interface keluar
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "tidak diketahui"
    finally:
        s.close()


# ---------------------------------------------------------------- Loop utama

def request_stop(signum, _frame):
    log.info("Sinyal %s diterima, berhenti...", signal.Signals(signum).name)
    stop_event.set()


def parse_args():
    parser = argparse.ArgumentParser(description="Kirim video + deteksi YOLO dari drone ke GCS")
    parser.add_argument("--source", default=str(CAMERA_SOURCE),
                        help="indeks kamera (0, 1, ...) atau URL kamera HP/IP camera "
                             "(mis. http://192.168.1.20:8080/video). Default: %(default)s")
    parser.add_argument("--web-port", type=int, default=WEB_PORT,
                        help="port tampilan browser/HP. Default: %(default)s")
    parser.add_argument("--no-web", action="store_true", help="matikan tampilan browser/HP")
    return parser.parse_args()


def main():
    global CAMERA_SOURCE, WEB_PORT, WEB_ENABLED
    args = parse_args()
    CAMERA_SOURCE = int(args.source) if args.source.isdigit() else args.source
    WEB_PORT = args.web_port
    WEB_ENABLED = WEB_ENABLED and not args.no_web

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    log.info("Memuat model %s", MODEL_PATH)
    # Dimuat di sini (bukan di atas file) agar modul ini cepat diimpor oleh smoking_detector.py
    from ultralytics import YOLO
    model = YOLO(MODEL_PATH)
    video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    detection_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    web_server = start_web_server() if WEB_ENABLED else None
    camera = None

    log.info("Kirim video ke %s:%d, deteksi ke %s:%d", GCS_IP, VIDEO_PORT, GCS_IP, DETECTION_PORT)

    frame_id = 0
    fails = 0
    fps_count = 0
    fps_start = last_log = time.time()

    try:
        camera = open_camera()
        while camera is not None and not stop_event.is_set():
            ret, frame = camera.read()
            if not ret:
                fails += 1
                if fails >= CAMERA_MAX_FAILS:
                    log.warning("Kamera tidak mengirim frame, membuka ulang...")
                    state.stats["camera_ok"] = False
                    camera.release()
                    camera = open_camera()
                    fails = 0
                else:
                    time.sleep(0.05)
                continue
            fails = 0
            frame = fit_frame(frame)

            results = model(frame, verbose=False)
            annotated = results[0].plot()

            # Kirim Video
            jpeg = encode_jpeg(annotated, JPEG_QUALITY)
            send_video(video_sock, frame_id, annotated, jpeg)

            # Kirim Metadata Deteksi
            detections = []
            for box in results[0].boxes:
                cls = int(box.cls[0])
                confidence = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                detections.append({
                    "class": model.names[cls],
                    "confidence": round(confidence, 3),
                    "bbox": [x1, y1, x2, y2]
                })

            message = {
                "drone": DRONE_NAME,
                "ip": DRONE_IP,
                "frame_id": frame_id,
                "timestamp": time.time(),
                "detections": detections
            }
            send_detections(detection_sock, message)

            if jpeg is not None:
                state.publish(jpeg, message)

            frame_id += 1
            fps_count += 1

            now = time.time()
            if now - last_log >= STATUS_LOG_INTERVAL:
                state.stats["fps"] = fps_count / (now - fps_start)
                s = state.stats
                log.info("FPS %.1f | deteksi %d | video terkirim %d, gagal %d, kualitas turun %d, dilewati %d"
                         " | deteksi gagal %d | penonton web %d",
                         s["fps"], len(detections), s["video_sent"], s["video_send_errors"],
                         s["video_quality_reduced"], s["video_skipped_too_large"],
                         s["detection_send_errors"], s["web_clients"])
                fps_count = 0
                fps_start = last_log = now
    finally:
        stop_event.set()
        if camera is not None:
            camera.release()
        video_sock.close()
        detection_sock.close()
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        log.info("Selesai.")


if __name__ == "__main__":
    main()
