"""Deteksi orang merokok / vape di area larangan (seluruh layar kamera = area larangan).

Model orang  : yolo11n.pt (COCO, kelas person)
Model rokok  : smoking_vape.pt (kelas: rokok, vape, asap)

Engine:
  yolo  - pelanggaran langsung dari smoking_vape.pt (rokok/vape di dalam kotak orang, asap di
          sekitar kepala), harus terlihat di >= confirm_frames dari window_frames frame terakhir.
  ai    - smoking_vape.pt hanya menandai KANDIDAT; foto dikirim ke Gemini (Google AI Studio) yang
          memastikan apakah orang itu benar merokok/vape/mengembuskan asap. Pemakaian Gemini dibatasi
          ketat agar tetap di kuota gratis (lihat GeminiGuard). Tanpa key/internet/kuota -> kembali
          ke deteksi lokal dengan ambang lebih ketat.
  --demo - "cell phone" dianggap rokok, untuk menguji alur peringatan.

Video ditampilkan dari thread kamera di FPS kamera; deteksi berjalan di thread lain secepat CPU
mampu, sehingga video tetap halus walau AI lebih lambat.

Contoh:
  python smoking_detector.py --engine ai
  python smoking_detector.py --source http://192.168.2.199:8080/video
  python smoking_detector.py --demo
"""
import re
import json
import time
import uuid
import base64
import queue
import signal
import socket
import logging
import argparse
import threading
import collections
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date
from pathlib import Path
from http.server import ThreadingHTTPServer

import os

import cv2
import numpy as np

import drone1_yolo_sender as base
from gps_location import DEFAULT_GPS_CONFIG, GpsReader, format_coords, maps_link

BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = BASE_DIR / "bukti"
CONFIG_FILE = BASE_DIR / "alert_config.json"
AI_USAGE_FILE = BASE_DIR / "ai_usage.json"
SMOKING_MODEL = "smoking_vape.pt"

PERSON_CLASS = 0
DEMO_CLASS = 67            # "cell phone" di COCO, pengganti rokok saat --demo
PERSON_CONF = 0.4
OBJECT_CONF = 0.35
PERSON_IMGSZ = 320         # orang berukuran besar di frame -> resolusi kecil cukup (±40 ms di CPU)
OBJECT_IMGSZ = 512         # rokok/vape kecil -> resolusi lebih besar (±70 ms di CPU)
PERSON_MARGIN = 0.15       # kotak orang diperbesar 15% saat mencocokkan rokok/vape
SMOKE_CLASS = "asap"
SMOKE_SIDE_MARGIN = 0.5    # asap boleh melayang 50% lebar orang ke kiri/kanan
SMOKE_ABOVE = 0.4          # ... dan 40% tinggi orang di atas kepala
SMOKE_BELOW = 0.6          # ... sampai 60% tinggi orang dari atas (kepala & badan atas)
ALERT_BANNER_S = 10        # lama banner peringatan tampil (detik)
EVIDENCE_JPEG_QUALITY = 85
STREAM_MAX_FPS = 30        # batas kirim video ke browser (kamera umumnya 30 FPS)

DEFAULT_CONFIG = {
    "camera_name": "kamera-01",
    "cooldown_s": 30,
    "confirm_frames": 3,
    "window_frames": 5,
    "gcs_alert": {"enabled": True, "port": 5201},
    # send_location: setelah foto, kirim juga pin lokasi (bisa dibuka di Google Maps dari Telegram)
    "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "send_location": True},
    "gps": dict(DEFAULT_GPS_CONFIG),
    "whatsapp_callmebot": {"enabled": False, "phone": "", "apikey": ""},
    "ai": {
        "enabled": True,
        "gemini_api_key": "",
        # Hanya model Flash-Lite (paling murah/ringan); dicoba berurutan saat start
        "models": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"],
        "max_per_minute": 8,       # batas keras lokal, di bawah batas kuota gratis
        "max_per_day": 250,
        "min_interval_s": 4,       # jeda minimal antar panggilan
        "periodic_check_s": 60,    # pemeriksaan rutin bila ada orang tapi tidak ada kandidat
        "min_confidence": 0.6,     # keyakinan Gemini minimal untuk dianggap pelanggaran
        "candidate_conf": 0.25,    # ambang kandidat dari smoking_vape.pt (sengaja sensitif)
    },
}

COLOR_PERSON = (80, 200, 80)
COLOR_VIOLATION = (40, 40, 230)
COLOR_OBJECT = (40, 200, 230)

log = logging.getLogger("rokok")


def load_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            # utf-8-sig: tetap terbaca walau file disimpan dengan BOM (mis. oleh Notepad)
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            log.error("alert_config.json tidak valid (%s), memakai pengaturan bawaan", e)
            return config
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


# ---------------------------------------------------------------- Status bersama

class AlertState:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.last_time = 0.0
        self.history = collections.deque(maxlen=30)
        self.latest = None
        self.fps = 0.0           # FPS video (kamera -> browser)
        self.detect_fps = 0.0    # berapa kali deteksi per detik
        self.started = time.time()
        self.phase = "loading"   # loading -> camera -> running (ditampilkan di panel)
        self.models_ready = False

    def add(self, record):
        with self.lock:
            self.total += 1
            self.last_time = record["timestamp"]
            self.history.append(record)

    def update_result(self, record, channel, result):
        with self.lock:
            record["sent"][channel] = result


alerts = AlertState()
config = DEFAULT_CONFIG
engine = "yolo"
demo_mode = False
gemini = None
gps = None


class Overlay:
    """Hasil deteksi terakhir yang digambar ulang di setiap frame video."""

    def __init__(self):
        self.lock = threading.Lock()
        self.persons = []
        self.detections = []
        self.confirmed = None    # (teks, bbox, sampai_kapan) dari konfirmasi Gemini

    def set(self, persons, detections):
        with self.lock:
            self.persons, self.detections = persons, detections

    def confirm(self, text, bbox):
        with self.lock:
            self.confirmed = (text, bbox, time.time() + ALERT_BANNER_S)

    def get(self):
        with self.lock:
            confirmed = self.confirmed if self.confirmed and self.confirmed[2] > time.time() else None
            return self.persons, self.detections, confirmed


overlay = Overlay()


def status_snapshot():
    with alerts.lock:
        last = alerts.history[-1] if alerts.history else None
        return {
            "drone": config["camera_name"],
            "ip": base.local_ip(),
            "mode": "demo" if demo_mode else ("ai" if engine == "ai" else "smoking"),
            "phase": alerts.phase,
            "models_ready": alerts.models_ready,
            "uptime_s": round(time.time() - alerts.started, 1),
            "stats": {
                "fps": alerts.fps,
                "detect_fps": alerts.detect_fps,
                "camera_ok": base.state.stats["camera_ok"],
                "web_clients": base.state.stats["web_clients"],
            },
            "latest": alerts.latest,
            "alerts_total": alerts.total,
            "alert": {
                "active": alerts.last_time > 0 and time.time() - alerts.last_time < ALERT_BANNER_S,
                "last_time": alerts.last_time,
                "last": last,
            },
            "ai": gemini.guard.status() if gemini else None,
            "gps": gps.status() if gps else None,
        }


# ---------------------------------------------------------------- Gemini (dengan pengaman biaya)

BILLING_WORDS = ("billing", "payment", "prepay", "credit", "paid tier", "upgrade", "pay-as-you-go")

GEMINI_PROMPT = (
    "Ini foto dari kamera pengawas area dilarang merokok. Nilai orang pada foto:\n"
    "- merokok: sedang mengisap rokok (rokok di mulut/bibir, atau baru diisap)\n"
    "- vape: sedang mengisap vape/rokok elektrik\n"
    "- asap: sedang mengembuskan asap/uap dari mulut atau hidung\n"
    "- memegang: memegang atau membawa rokok/vape yang terlihat jelas di tangan, walau tidak sedang diisap "
    "(rokok boleh belum menyala)\n"
    "Jangan tertipu: HP, earbuds, pulpen, sedotan minuman, makanan, botol, atau tangan menutup mulut BUKAN "
    "rokok/vape. Bedakan rokok (batang putih/cokelat, sering berfilter oranye) dari pulpen dan sedotan dari "
    "bentuk dan cara dipegang. Jawab true hanya bila bukti visualnya jelas. "
    "Isi 'alasan' dengan satu kalimat bahasa Indonesia.")

GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "merokok": {"type": "BOOLEAN"},
        "vape": {"type": "BOOLEAN"},
        "asap": {"type": "BOOLEAN"},
        "memegang": {"type": "BOOLEAN"},
        "keyakinan": {"type": "NUMBER"},
        "alasan": {"type": "STRING"},
    },
    "required": ["merokok", "vape", "asap", "memegang", "keyakinan", "alasan"],
}

# Jenis pelanggaran dari jawaban Gemini -> kelas di foto bukti / log / Telegram
VIOLATION_CLASSES = {"merokok": "rokok", "vape": "vape", "asap": "asap", "memegang": "memegang"}
CLASS_NAMES = {"rokok": "merokok", "vape": "memakai vape", "asap": "asap (ngepul)",
               "memegang": "memegang rokok/vape"}


class GeminiGuard:
    """Memanggil Gemini dengan batas ketat agar tidak pernah keluar dari kuota gratis.

    - batas lokal per menit & per hari (disimpan di ai_usage.json), setiap percobaan dihitung
    - 429 -> jeda sesuai retryDelay (berlipat bila berulang); kuota harian habis -> mati sampai besok
    - pesan berbau billing/pembayaran, 403, atau API key tidak valid -> mati sampai besok
    """

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = (cfg.get("gemini_api_key") or "").strip()
        self.lock = threading.Lock()
        self.model = None
        self.minute = collections.deque()
        self.last_call = 0.0
        self.backoff_until = 0.0
        self.backoff_step = 0
        self.message = ""
        self.offline = False
        self.last_verdict = None
        usage = self._load_usage()
        self.day = usage.get("date", "")
        self.calls_today = usage.get("calls", 0)
        self.disabled_day = usage.get("disabled_day", "")
        self.disabled_reason = usage.get("disabled_reason", "")
        self._roll_day()
        if not cfg.get("enabled", True):
            self.message = "Gemini dimatikan di alert_config.json"
        elif not self.key:
            self.message = "Belum ada API key Gemini (isi ai.gemini_api_key di alert_config.json)"

    # -- penyimpanan pemakaian harian
    def _load_usage(self):
        try:
            return json.loads(AI_USAGE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_usage(self):
        data = {"date": self.day, "calls": self.calls_today,
                "disabled_day": self.disabled_day, "disabled_reason": self.disabled_reason}
        try:
            AI_USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _roll_day(self):
        today = date.today().isoformat()
        if self.day != today:
            self.day, self.calls_today = today, 0
            if self.disabled_day and self.disabled_day != today:
                self.disabled_day, self.disabled_reason = "", ""
            self._save_usage()

    def _disable(self, reason):
        self.disabled_day = date.today().isoformat()
        self.disabled_reason = reason
        self.message = f"Gemini dimatikan sampai besok: {reason}"
        self._save_usage()
        log.warning(self.message)

    # -- status & izin
    def active(self):
        with self.lock:
            self._roll_day()
            return bool(self.cfg.get("enabled", True) and self.key and not self.disabled_day)

    def available(self):
        """Gemini bisa diandalkan saat ini (aktif, kuota lokal belum habis, ada internet).
        Bila False, detektor memakai deteksi lokal dengan ambang ketat."""
        with self.lock:
            self._roll_day()
            return (self.cfg.get("enabled", True) and bool(self.key) and not self.disabled_day
                    and self.calls_today < int(self.cfg["max_per_day"]) and not self.offline)

    def can_call(self):
        with self.lock:
            self._roll_day()
            now = time.time()
            while self.minute and now - self.minute[0] > 60:
                self.minute.popleft()
            return (self.cfg.get("enabled", True) and bool(self.key) and not self.disabled_day
                    and now >= self.backoff_until
                    and self.calls_today < int(self.cfg["max_per_day"])
                    and len(self.minute) < int(self.cfg["max_per_minute"])
                    and now - self.last_call >= float(self.cfg["min_interval_s"]))

    def status(self):
        with self.lock:
            self._roll_day()
            if self.disabled_day:
                text = f"Dimatikan sampai besok: {self.disabled_reason}"
            elif not self.key or not self.cfg.get("enabled", True):
                text = self.message
            elif self.calls_today >= int(self.cfg["max_per_day"]):
                text = "Batas harian lokal tercapai, memakai deteksi lokal sampai besok"
            elif time.time() < self.backoff_until:
                text = f"Dijeda {int(self.backoff_until - time.time())} dtk: {self.message}"
            else:
                text = "Aktif" + (f" ({self.model})" if self.model else "")
            return {"model": self.model, "calls_today": self.calls_today,
                    "max_per_day": int(self.cfg["max_per_day"]), "status": text,
                    "active": bool(self.key and not self.disabled_day and self.cfg.get("enabled", True)),
                    "last_verdict": self.last_verdict}

    # -- pemanggilan
    def _post(self, model, parts):
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 200,
                                     "responseMimeType": "application/json",
                                     "responseSchema": GEMINI_SCHEMA}}
        req = urllib.request.Request(self.URL.format(model), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
        with self.lock:
            # Hitung SEBELUM dikirim agar batas tetap ketat walau permintaan gagal
            self.calls_today += 1
            self.minute.append(time.time())
            self.last_call = time.time()
            self._save_usage()
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())

    def _handle_http_error(self, e):
        try:
            err = json.loads(e.read() or b"{}").get("error", {})
        except ValueError:
            err = {}
        msg = err.get("message", str(e))
        low = msg.lower()
        with self.lock:
            if any(w in low for w in BILLING_WORDS):
                self._disable("ada tanda berbayar dari Google (" + msg[:120] + ")")
            elif e.code == 429:
                details = json.dumps(err.get("details", []))
                if "PerDay" in details or "per day" in low:
                    self._disable("kuota harian gratis habis")
                else:
                    m = re.search(r'"retryDelay":\s*"(\d+)', details)
                    delay = max(int(m.group(1)) if m else 30, 30) * (2 ** self.backoff_step)
                    self.backoff_step = min(self.backoff_step + 1, 4)
                    self.backoff_until = time.time() + delay
                    self.message = "batas kuota per menit tercapai"
                    log.warning("Gemini dijeda %d detik (429)", delay)
            elif e.code in (401, 403):
                self._disable("akses ditolak (" + msg[:120] + ")")
            elif e.code == 400 and "api key" in low:
                self._disable("API key tidak valid")
            else:
                self.backoff_until = time.time() + 30
                self.message = f"HTTP {e.code}: {msg[:120]}"
        return e.code

    def select_model(self):
        """Pilih model Flash-Lite pertama yang menjawab di project ini (1 panggilan kecil)."""
        for model in self.cfg.get("models", []):
            if not self.can_call() and not self._wait_turn(15):
                return False
            try:
                self._post(model, [{"text": 'Balas JSON: {"merokok":false,"vape":false,"asap":false,"memegang":false,'
                                            '"keyakinan":0,"alasan":"tes"}'}])
                with self.lock:
                    self.model = model
                    self.backoff_step = 0
                    self.offline = False
                    self.message = ""
                log.info("Gemini siap memakai model %s (pemakaian hari ini %d/%s)",
                         model, self.calls_today, self.cfg["max_per_day"])
                return True
            except urllib.error.HTTPError as e:
                code = self._handle_http_error(e)
                if code not in (404, 400) or not self.active():
                    return False
                log.info("Model %s tidak tersedia, mencoba berikutnya", model)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                with self.lock:
                    self.backoff_until = time.time() + 30
                    self.offline = True
                    self.message = f"tidak ada internet ({e.__class__.__name__})"
                return False
        return False

    def _wait_turn(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not base.stop_event.is_set():
            if self.can_call():
                return True
            time.sleep(0.5)
        return False

    def classify(self, jpeg):
        """Kembalikan dict jawaban Gemini, atau None bila gagal/dibatasi."""
        if not self.model and not self.select_model():
            return None
        if not self.can_call():
            return None
        parts = [{"text": GEMINI_PROMPT},
                 {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]
        try:
            data = self._post(self.model, parts)
        except urllib.error.HTTPError as e:
            if self._handle_http_error(e) == 404:
                with self.lock:
                    self.model = None
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            with self.lock:
                self.backoff_until = time.time() + 30
                self.offline = True
                self.message = f"tidak ada internet ({e.__class__.__name__})"
            return None
        with self.lock:
            self.backoff_step = 0
            self.offline = False
            self.message = ""
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            verdict = json.loads(text)
        except (KeyError, IndexError, ValueError):
            verdict = {"merokok": False, "vape": False, "asap": False, "memegang": False, "keyakinan": 0.0,
                       "alasan": "Jawaban AI tidak terbaca"}
        verdict["tokens"] = data.get("usageMetadata", {}).get("totalTokenCount")
        verdict["time"] = time.time()
        with self.lock:
            self.last_verdict = verdict
        return verdict


class GeminiWorker:
    """Thread pemeriksa: menerima paling banyak 1 permintaan antre agar deteksi tidak tersendat."""

    def __init__(self, guard):
        self.guard = guard
        self.requests = queue.Queue(maxsize=1)
        self.results = queue.Queue()
        self.busy = False
        threading.Thread(target=self._run, name="gemini", daemon=True).start()

    def submit(self, job):
        try:
            self.requests.put_nowait(job)
            return True
        except queue.Full:
            return False

    def idle(self):
        return not self.busy and self.requests.empty()

    def _run(self):
        if self.guard.active():
            self.guard.select_model()
        while not base.stop_event.is_set():
            try:
                job = self.requests.get(timeout=0.5)
            except queue.Empty:
                continue
            self.busy = True
            try:
                verdict = self.guard.classify(job["jpeg"])
                if verdict is not None:
                    self.results.put((job, verdict))
            finally:
                self.busy = False


def crop_for_ai(frame, bbox):
    """Potongan kepala & badan atas orang (atau seluruh frame), diperkecil agar token sedikit."""
    img = frame
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        w, h = x2 - x1, y2 - y1
        x1, x2 = max(0, int(x1 - w * 0.15)), min(frame.shape[1], int(x2 + w * 0.15))
        y1, y2 = max(0, int(y1 - h * 0.1)), min(frame.shape[0], int(y1 + h * 0.75))
        if x2 - x1 > 80 and y2 - y1 > 80:
            img = frame[y1:y2, x1:x2]
    scale = 512 / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return enc.tobytes() if ok else None


# ---------------------------------------------------------------- Deteksi

def box_xyxy(box):
    return [int(v) for v in box.xyxy[0]]


def detect(frame, person_model, smoke_model, object_conf):
    """Kembalikan (daftar kotak orang, daftar objek (label, conf, bbox))."""
    persons, objects = [], []
    if demo_mode:
        result = person_model(frame, imgsz=OBJECT_IMGSZ, verbose=False, classes=[PERSON_CLASS, DEMO_CLASS],
                              conf=min(PERSON_CONF, object_conf))[0]
        for b in result.boxes:
            cls, conf = int(b.cls[0]), float(b.conf[0])
            if cls == PERSON_CLASS and conf >= PERSON_CONF:
                persons.append(box_xyxy(b))
            elif cls == DEMO_CLASS and conf >= object_conf:
                objects.append(("rokok", conf, box_xyxy(b)))
        return persons, objects

    result = person_model(frame, imgsz=PERSON_IMGSZ, verbose=False, classes=[PERSON_CLASS], conf=PERSON_CONF)[0]
    persons = [box_xyxy(b) for b in result.boxes]
    result = smoke_model(frame, imgsz=OBJECT_IMGSZ, verbose=False, conf=object_conf)[0]
    for b in result.boxes:
        objects.append((str(smoke_model.names[int(b.cls[0])]), float(b.conf[0]), box_xyxy(b)))
    return persons, objects


def owner_person(label, bbox, persons):
    """Kotak orang yang 'memiliki' objek ini (rokok/vape di dalam badan, asap di sekitar kepala)."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for p in persons:
        x1, y1, x2, y2 = p
        w, h = x2 - x1, y2 - y1
        if label == SMOKE_CLASS:
            # Asap: di sekitar kepala & badan atas, boleh melayang ke samping/atas
            area = (x1 - w * SMOKE_SIDE_MARGIN, y1 - h * SMOKE_ABOVE,
                    x2 + w * SMOKE_SIDE_MARGIN, y1 + h * SMOKE_BELOW)
        else:
            area = (x1 - w * PERSON_MARGIN, y1 - h * PERSON_MARGIN,
                    x2 + w * PERSON_MARGIN, y2 + h * PERSON_MARGIN)
        if area[0] <= cx <= area[2] and area[1] <= cy <= area[3]:
            return p
    return None


def near_person(label, bbox, persons):
    return owner_person(label, bbox, persons) is not None


def draw_box(img, bbox, color, text, thickness):
    x1, y1, x2, y2 = bbox
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    ty = max(y1, th + 6)
    cv2.rectangle(img, (x1, ty - th - 6), (x1 + tw + 6, ty), color, -1)
    cv2.putText(img, text, (x1 + 3, ty - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def annotate(frame, persons, detections, alert_active, confirmed=None, reason=None):
    img = frame.copy()
    for p in persons:
        draw_box(img, p, COLOR_PERSON, "orang", 1)
    for d in detections:
        if d["violation"]:
            draw_box(img, d["bbox"], COLOR_VIOLATION, f"{d['class'].upper()} {d['confidence']:.2f}", 3)
        else:
            draw_box(img, d["bbox"], COLOR_OBJECT, f"{d['class']}? {d['confidence']:.2f}", 1)
    if confirmed:
        draw_box(img, confirmed[1], COLOR_VIOLATION, confirmed[0], 3)
    if alert_active:
        cv2.rectangle(img, (0, 0), (img.shape[1], 34), COLOR_VIOLATION, -1)
        text = "PERINGATAN: MEROKOK / VAPE" + (" [DEMO]" if demo_mode else "")
        cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    if reason:
        h = img.shape[0]
        cv2.rectangle(img, (0, h - 28), (img.shape[1], h), (20, 20, 20), -1)
        text = ("AI: " + reason)[:90]
        cv2.putText(img, text, (8, h - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------- Kamera (thread terpisah)

class CameraThread:
    """Membaca kamera secepat mungkin, menerbitkan video ke browser dengan kotak deteksi terakhir,
    dan menyimpan frame terbaru untuk thread deteksi."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        camera = base.open_camera()
        fails = 0
        count, t0 = 0, time.time()
        last_pub = 0.0
        try:
            while camera is not None and not base.stop_event.is_set():
                ok, frame = camera.read()
                if not ok:
                    fails += 1
                    if fails >= base.CAMERA_MAX_FAILS:
                        log.warning("Kamera tidak mengirim frame, membuka ulang...")
                        base.state.stats["camera_ok"] = False
                        camera.release()
                        camera = base.open_camera()
                        fails = 0
                    else:
                        time.sleep(0.02)
                    continue
                fails = 0
                frame = base.fit_frame(frame)
                with self.cond:
                    self.frame = frame
                    self.seq += 1
                    self.cond.notify_all()
                if alerts.phase == "camera":
                    alerts.phase = "running" if alerts.models_ready else "loading"
                now = time.time()
                # Toleransi 20%: frame kamera 30 FPS (33 ms) tidak boleh terbuang karena jitter
                if now - last_pub >= 0.8 / STREAM_MAX_FPS:
                    last_pub = now
                    self._publish(frame, now)
                    count += 1
                if now - t0 >= 2.0:
                    alerts.fps = count / (now - t0)
                    count, t0 = 0, now
        finally:
            if camera is not None:
                camera.release()

    def _publish(self, frame, now):
        persons, detections, confirmed = overlay.get()
        active = alerts.last_time > 0 and now - alerts.last_time < ALERT_BANNER_S
        jpeg = base.encode_jpeg(annotate(frame, persons, detections, active, confirmed), base.JPEG_QUALITY)
        if jpeg is not None:
            with alerts.lock:
                message = alerts.latest
            base.state.publish(jpeg, message)

    def wait_new(self, last_seq, timeout=1.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or base.stop_event.is_set(), timeout)
            return self.frame, self.seq


# ---------------------------------------------------------------- Peringatan

def send_gcs_alert(message):
    port = config["gcs_alert"]["port"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(json.dumps(message).encode(), (base.GCS_IP, port))
    finally:
        sock.close()
    return f"terkirim ke {base.GCS_IP}:{port}"


def send_telegram(caption, jpeg):
    tg = config["telegram"]
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", str(tg["chat_id"])), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="bukti.jpg"\r\n'
                 f"Content-Type: image/jpeg\r\n\r\n".encode() + jpeg + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tg['bot_token']}/sendPhoto", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        reply = json.loads(r.read())
    if not reply.get("ok"):
        raise RuntimeError(reply.get("description", "gagal"))
    return "terkirim"


def send_telegram_location(loc):
    tg = config["telegram"]
    data = urllib.parse.urlencode({"chat_id": str(tg["chat_id"]), "latitude": loc["lat"],
                                   "longitude": loc["lon"]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tg['bot_token']}/sendLocation", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        reply = json.loads(r.read())
    if not reply.get("ok"):
        raise RuntimeError(reply.get("description", "gagal"))


def draw_location(img, loc):
    """Cetak koordinat di pojok kanan atas foto bukti (di bawah banner peringatan)."""
    text = ("GPS " + format_coords(loc)) if loc else "GPS: tidak ada sinyal"
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    x, y = img.shape[1] - tw - 12, 40
    cv2.rectangle(img, (x - 6, y), (img.shape[1], y + th + 12), (20, 20, 20), -1)
    cv2.putText(img, text, (x, y + th + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def location_text(loc):
    if not loc:
        return "\nKoordinat: tidak tersedia (GPS belum dapat sinyal)"
    text = f"\nKoordinat: {format_coords(loc)}"
    if loc.get("alt") is not None:
        text += f" (ketinggian {loc['alt']:.0f} m)"
    if loc.get("accuracy") is not None:
        text += f" ±{loc['accuracy']:.0f} m"
    text += f"\nSumber GPS: {loc['source']}\nMaps: {maps_link(loc)}"
    return text


def send_whatsapp(text):
    wa = config["whatsapp_callmebot"]
    query = urllib.parse.urlencode({"phone": wa["phone"], "text": text, "apikey": wa["apikey"]})
    with urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{query}", timeout=20) as r:
        r.read()
    return "terkirim"


def alert_worker(jobs):
    while True:
        job = jobs.get()
        if job is None:
            return
        record, image = job
        loc = record.get("location")
        if config["gps"].get("enabled"):
            image = draw_location(image, loc)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, EVIDENCE_JPEG_QUALITY])
        jpeg = encoded.tobytes() if ok else b""

        EVIDENCE_DIR.mkdir(exist_ok=True)
        (EVIDENCE_DIR / record["photo"]).write_bytes(jpeg)

        names = ", ".join(sorted({CLASS_NAMES.get(o["class"], o["class"]) for o in record["objects"]}))
        when = datetime.fromtimestamp(record["timestamp"]).strftime("%d-%m-%Y %H:%M:%S")
        text = (f"{'[DEMO] ' if demo_mode else ''}PERINGATAN: terdeteksi {names} di area larangan.\n"
                f"Kamera: {config['camera_name']}\nWaktu: {when}")
        if config["gps"].get("enabled"):
            text += location_text(loc)
        if record.get("reason"):
            text += f"\nAlasan AI: {record['reason']}"
        if record.get("verified_by"):
            text += f"\nDiperiksa oleh: {record['verified_by']}"

        def telegram():
            result = send_telegram(text, jpeg)
            if loc and tg.get("send_location", True):
                try:
                    send_telegram_location(loc)
                    result += " + lokasi"
                except Exception as e:
                    result += f" (pin lokasi gagal: {e})"
            return result

        channels = []
        if config["gcs_alert"].get("enabled"):
            channels.append(("gcs", lambda: send_gcs_alert(dict(record, type="smoking_alert"))))
        tg = config["telegram"]
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            channels.append(("telegram", telegram))
        wa = config["whatsapp_callmebot"]
        if wa.get("enabled") and wa.get("phone") and wa.get("apikey"):
            channels.append(("whatsapp", lambda: send_whatsapp(text)))

        for name, send in channels:
            try:
                result = send()
            except Exception as e:  # jaringan / token salah: catat, jangan hentikan detektor
                result = f"gagal: {e}"
            alerts.update_result(record, name, result)
            log.info("Peringatan %s: %s", name, result)

        with open(EVIDENCE_DIR / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def make_record(now, frame_id, objects, persons, reason=None, verified_by=None):
    classes = sorted({o["class"] for o in objects})
    location = gps.get() if gps else None
    if location:
        location.pop("time", None)
        location["maps"] = maps_link(location)
    return {
        "camera": config["camera_name"],
        "ip": base.local_ip(),
        "timestamp": now,
        "time": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "frame_id": frame_id,
        "objects": objects,
        "persons": len(persons),
        "photo": datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S") + "_" + "_".join(classes) + ".jpg",
        "demo": demo_mode,
        "reason": reason,
        "verified_by": verified_by,
        "location": location,   # koordinat saat terdeteksi: lat, lon, alt, accuracy, source, maps
        "sent": {},
    }


# ---------------------------------------------------------------- Server web

PAGE_HTML = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deteksi Rokok</title>
<style>
 body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif}
 header{padding:10px 14px;display:flex;justify-content:space-between}
 h1{font-size:16px;margin:0} img.live{width:100%;display:block;background:#000;min-height:200px}
 #banner{display:none;background:#d33;color:#fff;font-weight:700;padding:12px 14px}
 section{padding:10px 14px;font-size:14px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
 .grid a img{width:100%;border-radius:6px;display:block} .muted{color:#999;font-size:12px}
</style></head><body>
<header><h1 id="name">Deteksi Rokok</h1><span class="muted" id="fps">-</span></header>
<div id="banner">PERINGATAN: terdeteksi merokok / vape!</div>
<img class="live" src="/stream.mjpg" alt="Video" onerror="setTimeout(()=>this.src='/stream.mjpg?t='+Date.now(),2000)">
<section><b>Pelanggaran: <span id="total">0</span></b>
<p class="muted">Panel lengkap (Start/Stop/Capture): buka port 8000 di perangkat ini.</p>
<div class="grid" id="grid"></div></section>
<script>
async function poll(){
 try{
  const s=await (await fetch('/status.json',{cache:'no-store'})).json();
  document.getElementById('name').textContent=s.drone+(s.mode==='demo'?' (DEMO)':'');
  document.getElementById('fps').textContent=s.stats.fps.toFixed(1)+' FPS';
  document.getElementById('total').textContent=s.alerts_total;
  document.getElementById('banner').style.display=s.alert.active?'block':'none';
  const a=await (await fetch('/alerts.json',{cache:'no-store'})).json();
  document.getElementById('grid').innerHTML=a.alerts.slice().reverse().map(r=>
   '<a href="/bukti/'+r.photo+'" target="_blank"><img src="/bukti/'+r.photo+'"><span class="muted">'+
   new Date(r.timestamp*1000).toLocaleTimeString()+' - '+r.objects.map(o=>o['class']).join(', ')+'</span></a>').join('');
 }catch(e){}
}
setInterval(poll,1000);poll();
</script></body></html>"""


class SmokingWebHandler(base.WebHandler):
    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
        elif path == "/status.json":
            self._send(200, "application/json", json.dumps(status_snapshot()).encode())
        elif path == "/alerts.json":
            with alerts.lock:
                body = json.dumps({"alerts": list(alerts.history)}).encode()
            self._send(200, "application/json", body)
        elif path.startswith("/bukti/"):
            name = path[len("/bukti/"):]
            file = EVIDENCE_DIR / name
            if name and name == Path(name).name and file.is_file():
                self._send(200, "image/jpeg", file.read_bytes())
            else:
                self._send(404, "text/plain", b"Tidak ditemukan")
        else:
            super().do_GET()


def start_web_server(port):
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), SmokingWebHandler)
    except OSError as e:
        log.error("Server web gagal dibuka di port %d (%s). Lanjut tanpa tampilan web.", port, e)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Tampilan web: http://%s:%d", base.local_ip(), port)
    return server


# ---------------------------------------------------------------- Main

def parse_args():
    parser = argparse.ArgumentParser(description="Deteksi orang merokok / vape di area larangan")
    parser.add_argument("--source", default=str(base.CAMERA_INDEX),
                        help="indeks kamera atau URL kamera HP (…/video). Default: %(default)s")
    parser.add_argument("--engine", choices=("yolo", "ai"), default="yolo",
                        help="yolo = model lokal saja; ai = model lokal + pemastian Gemini. Default: %(default)s")
    parser.add_argument("--model", default=SMOKING_MODEL, help="model rokok/vape. Default: %(default)s")
    parser.add_argument("--conf", type=float, default=OBJECT_CONF,
                        help="ambang keyakinan rokok/vape (0-1). Default: %(default)s")
    parser.add_argument("--web-port", type=int, default=base.WEB_PORT, help="port tampilan web. Default: %(default)s")
    parser.add_argument("--no-web", action="store_true", help="matikan tampilan web")
    parser.add_argument("--no-person-check", action="store_true",
                        help="hitung rokok/vape walau tidak terlihat orang (mis. kamera sangat dekat)")
    parser.add_argument("--demo", action="store_true",
                        help="mode uji: HP (cell phone) dianggap rokok, tidak perlu smoking_vape.pt")
    parser.add_argument("--threads", type=int, default=0,
                        help="jumlah thread CPU untuk deteksi (0 = otomatis). Default: %(default)s")
    return parser.parse_args()


def load_models(args):
    # ultralytics/torch dimuat di sini (±5 dtk) setelah kamera dinyalakan, jadi video muncul lebih dulu
    import torch
    from ultralytics import YOLO
    # Bawaan torch (semua inti) terbukti paling cepat; --threads hanya untuk perangkat yang perlu dibatasi
    if args.threads:
        torch.set_num_threads(args.threads)
    log.info("Deteksi memakai %d thread CPU", torch.get_num_threads())
    person_model = YOLO(base.MODEL_PATH)
    smoke_model = None
    if demo_mode:
        log.info("MODE DEMO: benda 'cell phone' dianggap rokok")
    else:
        smoke_model = YOLO(args.model)
        log.info("Kelas model rokok/vape: %s", list(smoke_model.names.values()))
    return person_model, smoke_model


def main():
    global config, demo_mode, engine, gemini, gps
    args = parse_args()
    demo_mode = args.demo
    engine = "yolo" if demo_mode else args.engine
    config = load_config()
    base.CAMERA_SOURCE = int(args.source) if args.source.isdigit() else args.source
    gps = GpsReader(config["gps"], camera_source=base.CAMERA_SOURCE, stop_event=base.stop_event).start()

    signal.signal(signal.SIGINT, base.request_stop)
    signal.signal(signal.SIGTERM, base.request_stop)

    if not demo_mode and not (BASE_DIR / args.model).exists() and not Path(args.model).exists():
        log.error("Model %s tidak ditemukan. Latih dulu dengan train_smoking_vape.ipynb, "
                  "atau jalankan dengan --demo untuk uji coba.", args.model)
        raise SystemExit(2)

    # Server web & kamera dinyalakan dulu: video sudah tampil selagi model AI disiapkan
    jobs = queue.Queue()
    worker = threading.Thread(target=alert_worker, args=(jobs,), name="alert", daemon=True)
    worker.start()
    web_server = None if args.no_web else start_web_server(args.web_port)
    alerts.phase = "camera"
    camera = CameraThread()
    camera.start()

    log.info("Menyiapkan model AI (kamera sudah menyala)...")
    t0 = time.time()
    person_model, smoke_model = load_models(args)
    # Pemanasan: inferensi pertama memuat banyak pustaka torch
    detect(np.zeros((base.HEIGHT, base.WIDTH, 3), np.uint8), person_model, smoke_model, args.conf)
    alerts.models_ready = True
    if alerts.phase == "loading":
        alerts.phase = "running"
    log.info("Model siap dalam %.1f detik", time.time() - t0)

    ai_cfg = config["ai"]
    use_ai = engine == "ai"
    if use_ai:
        gemini = GeminiWorker(GeminiGuard(ai_cfg))
    object_conf = float(ai_cfg["candidate_conf"]) if use_ai else args.conf

    channels = ["foto bukti", "web"]
    if config["gcs_alert"].get("enabled"):
        channels.append(f"GCS {base.GCS_IP}:{config['gcs_alert']['port']}")
    if config["telegram"].get("enabled"):
        channels.append("Telegram")
    if config["whatsapp_callmebot"].get("enabled"):
        channels.append("WhatsApp")
    log.info("Engine: %s | peringatan aktif: %s | cooldown %ss", engine, ", ".join(channels), config["cooldown_s"])
    if use_ai:
        log.info("Gemini: %s | batas %s/menit, %s/hari", gemini.guard.status()["status"],
                 ai_cfg["max_per_minute"], ai_cfg["max_per_day"])

    window = collections.deque(maxlen=int(config["window_frames"]))
    last_alert = 0.0
    last_ai_check = 0.0
    frame_id = 0
    fps_count = 0
    fps_start = last_log = time.time()
    last_seq = 0

    try:
        while not base.stop_event.is_set():
            frame, seq = camera.wait_new(last_seq)
            if frame is None or seq == last_seq:
                continue
            last_seq = seq

            persons, objects = detect(frame, person_model, smoke_model, object_conf)
            now = time.time()
            detections = []
            for label, conf, bbox in objects:
                owner = None if args.no_person_check else owner_person(label, bbox, persons)
                violation = args.no_person_check or owner is not None
                if use_ai:
                    # Mode AI: objek dari model lokal hanya kandidat, Gemini yang memutuskan
                    detections.append({"class": label, "confidence": round(conf, 3), "bbox": bbox,
                                       "violation": False, "candidate": violation, "_owner": owner})
                else:
                    detections.append({"class": label, "confidence": round(conf, 3), "bbox": bbox,
                                       "violation": violation})

            if use_ai:
                candidates = [d for d in detections if d["candidate"]]
                window.append(bool(candidates))
                ai_on = gemini.guard.active()
                suspicious = sum(window) >= 2
                periodic = persons and now - last_ai_check >= float(ai_cfg["periodic_check_s"])
                if ai_on and (suspicious or periodic) and gemini.idle() and gemini.guard.can_call() \
                        and now - last_alert >= float(config["cooldown_s"]):
                    target = max(candidates, key=lambda d: d["confidence"]) if candidates else None
                    owner = target["_owner"] if target else (max(persons, key=lambda p: (p[2] - p[0]) * (p[3] - p[1])))
                    jpeg = crop_for_ai(frame, owner)
                    if jpeg and gemini.submit({"jpeg": jpeg, "frame": frame.copy(), "person": owner,
                                               "persons": list(persons), "frame_id": frame_id,
                                               "candidates": candidates, "time": now}):
                        last_ai_check = now
                        log.info("Dikirim ke Gemini untuk dipastikan (%s)",
                                 "kandidat " + target["class"] if target else "pemeriksaan rutin")
                # Hasil Gemini
                while True:
                    try:
                        job, verdict = gemini.results.get_nowait()
                    except queue.Empty:
                        break
                    # Mengisap/mengembuskan lebih berat dari sekadar memegang: catat yang aktif saja bila ada
                    found = [k for k in ("merokok", "vape", "asap") if verdict.get(k)]
                    if not found and verdict.get("memegang"):
                        found = ["memegang"]
                    yes = found and float(verdict.get("keyakinan", 0)) >= float(ai_cfg["min_confidence"])
                    log.info("Gemini: %s (%.2f) - %s", ", ".join(found) if found else "tidak ada",
                             float(verdict.get("keyakinan", 0)), verdict.get("alasan", ""))
                    if yes and time.time() - last_alert >= float(config["cooldown_s"]):
                        bbox = job["person"] or [0, 0, job["frame"].shape[1], job["frame"].shape[0]]
                        objs = [{"class": VIOLATION_CLASSES[k], "confidence": round(float(verdict["keyakinan"]), 2),
                                 "bbox": bbox} for k in found]
                        last_alert = time.time()
                        label = "DIKONFIRMASI AI: " + ", ".join(CLASS_NAMES[VIOLATION_CLASSES[k]].upper() for k in found)
                        overlay.confirm(label, bbox)
                        record = make_record(last_alert, job["frame_id"], objs, job["persons"],
                                             reason=verdict.get("alasan"),
                                             verified_by=f"Gemini ({gemini.guard.model})")
                        image = annotate(job["frame"], job["persons"], [], True, (label, bbox, 0),
                                         reason=verdict.get("alasan"))
                        alerts.add(record)
                        jobs.put((record, image))
                        log.warning("PELANGGARAN (dikonfirmasi Gemini): %s", label)
                # Tanpa Gemini (tidak ada key / kuota / internet): deteksi lokal dengan ambang ketat
                if not gemini.guard.available():
                    strict = [d for d in candidates if d["confidence"] >= 0.5]
                    if strict and sum(window) >= max(4, int(config["confirm_frames"])) \
                            and now - last_alert >= float(config["cooldown_s"]):
                        for d in strict:
                            d["violation"] = True
                        last_alert = now
                        objs = [{k: d[k] for k in ("class", "confidence", "bbox")} for d in strict]
                        record = make_record(now, frame_id, objs, persons,
                                             verified_by="model lokal (Gemini tidak aktif)")
                        alerts.add(record)
                        jobs.put((record, annotate(frame, persons, strict, True)))
                        log.warning("PELANGGARAN (lokal, tanpa Gemini): %s",
                                    ", ".join(f"{d['class']} {d['confidence']:.2f}" for d in strict))
                public = [{k: v for k, v in d.items() if not k.startswith("_")} for d in detections]
            else:
                violations = [d for d in detections if d["violation"]]
                window.append(bool(violations))
                confirmed = sum(window) >= int(config["confirm_frames"])
                if confirmed and violations and now - last_alert >= float(config["cooldown_s"]):
                    last_alert = now
                    objs = [{k: d[k] for k in ("class", "confidence", "bbox")} for d in violations]
                    record = make_record(now, frame_id, objs, persons)
                    alerts.add(record)
                    jobs.put((record, annotate(frame, persons, detections, True)))
                    log.warning("PELANGGARAN: %s (orang: %d)",
                                ", ".join(f"{d['class']} {d['confidence']:.2f}" for d in violations), len(persons))
                public = detections

            # Mode AI: kandidat lokal tidak digambar di video (sering keliru); hanya yang dipastikan
            overlay.set(persons, [d for d in public if d["violation"]] if use_ai else public)
            with alerts.lock:
                alerts.latest = {"drone": config["camera_name"], "frame_id": frame_id, "timestamp": now,
                                 "detections": public, "persons": len(persons)}

            frame_id += 1
            fps_count += 1
            if now - last_log >= base.STATUS_LOG_INTERVAL:
                alerts.detect_fps = fps_count / (now - fps_start)
                log.info("Video %.1f FPS | deteksi %.1f/dtk | orang %d | objek %d | total pelanggaran %d",
                         alerts.fps, alerts.detect_fps, len(persons), len(detections), alerts.total)
                fps_count = 0
                fps_start = last_log = now
    finally:
        base.stop_event.set()
        jobs.put(None)
        worker.join(timeout=25)
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        log.info("Selesai.")


if __name__ == "__main__":
    main()
