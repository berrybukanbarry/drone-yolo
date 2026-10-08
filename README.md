# Drone 1 – YOLO Sender & Deteksi Rokok/Vape

Folder ini berisi dua program deteksi berbasis YOLO yang dikendalikan lewat **Panel Kontrol**
di browser (bisa dibuka dari laptop maupun HP):

1. **Kirim ke GCS** (`drone1_yolo_sender.py`): berjalan di komputer pendamping drone
   (`companion-01`, `10.42.0.11`). Program ini mendeteksi objek dengan YOLO11n, lalu
   mengirim video dan hasil deteksi ke Ground Control Station (GCS) `10.42.0.1` lewat UDP.
2. **Deteksi AI (dipastikan Gemini)** (`smoking_detector.py --engine ai`, mode bawaan): model lokal
   menandai hal yang mencurigakan, lalu **Google Gemini** memastikan apakah orang itu benar-benar
   merokok, memakai vape, atau mengembuskan asap. Alarm palsu (tangan di mulut, HP, earbuds) tersaring.
   Pemakaian Gemini dibatasi ketat agar tetap di kuota gratis (lihat bagian 5).
3. **Deteksi rokok & vape, model lokal saja** (`smoking_detector.py`): tanpa internet. Saat ada
   pelanggaran, program menyimpan foto bukti dan mengirim peringatan ke GCS, halaman web, Telegram,
   dan WhatsApp.

```
                              ┌─► UDP 5001  → GCS: video JPEG             (Kirim ke GCS)
                              ├─► UDP 5101  → GCS: JSON deteksi           (Kirim ke GCS)
Kamera (drone / HP) ─► YOLO ──┼─► UDP 5201  → GCS: peringatan rokok/vape  (Deteksi rokok)
                              ├─► Telegram / WhatsApp                     (Deteksi rokok)
                              └─► HTTP 8080 → video langsung di browser

Panel Kontrol (HTTP 8000) ─► Start / Stop / Restart / Capture / Rekam / Galeri / Log
```

## Isi folder

| File | Keterangan |
|---|---|
| `Mulai Panel.bat` | **Windows: klik dua kali untuk membuka panel** |
| `start_panel.sh` | Linux: menyalakan panel |
| `drone-panel.service` | Linux: panel menyala otomatis saat drone boot |
| `control_panel.py` | Program panel kontrol (port 8000) |
| `drone1_yolo_sender.py` | Program kirim video + deteksi ke GCS |
| `smoking_detector.py` | Program deteksi rokok & vape |
| `train_smoking_vape.ipynb` | Notebook Google Colab untuk melatih model rokok/vape |
| `gcs_alert_receiver.py` | Penerima peringatan rokok/vape di laptop GCS |
| `alert_config.example.json` | Contoh pengaturan: salin menjadi `alert_config.json`, lalu isi key Anda (bagian 1b) |
| `requirements.txt` | Daftar library Python |
| `yolo11n.pt` | Model YOLO umum (diunduh otomatis bila tidak ada) |
| `smoking_vape.pt` | Model rokok/vape, hasil dari notebook (belum ada sebelum dilatih) |

Dibuat otomatis saat dipakai: `captures/` (foto/video capture), `bukti/` (foto bukti
pelanggaran + `log.jsonl`), `panel_settings.json` (pengaturan panel terakhir),
`ai_usage.json` (jumlah pemakaian Gemini hari ini). Dibuat sendiri oleh pengguna: `alert_config.json`
(berisi key rahasia, tidak ada di GitHub).

---

## 1. Instalasi (sekali saja)

### Linux (drone atau laptop teman), dari GitHub

```bash
git clone https://github.com/bryanfrisco/drone-yolo.git
cd drone-yolo
sudo apt install python3-venv libgl1 libglib2.0-0     # libgl1 wajib untuk OpenCV
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x start_panel.sh
sudo usermod -aG video $USER                          # izin kamera; lalu logout & login lagi
cp alert_config.example.json alert_config.json        # lalu isi key Anda (bagian 1b)
./start_panel.sh --open-browser
```

Repo ini **private**: teman harus diundang dulu (GitHub → repo → Settings → Collaborators),
lalu login GitHub saat `git clone` meminta.

Versi terbaru nanti cukup diambil dengan `git pull` di folder yang sama.

### Windows (laptop untuk uji coba)

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
copy alert_config.example.json alert_config.json        # lalu isi key Anda (bagian 1b)
```

> Tanpa `uv`: `py -3.12 -m venv .venv` lalu `.venv\Scripts\pip install -r requirements.txt`.

---

## 1b. Panduan pengguna: menyiapkan key (sekali saja)

Key dan token **tidak ada di GitHub** karena bersifat rahasia. Setiap orang yang memakai program ini
membuatnya sendiri, lalu menyimpannya di file **`alert_config.json`** di perangkatnya masing-masing.

| Key | Untuk apa | Wajib? | Biaya |
|---|---|---|---|
| **API key Gemini** | Mode **Deteksi AI**: memastikan orang benar-benar merokok/vape | Disarankan. Tanpa key, mode AI memakai model lokal saja | Gratis (tier gratis) |
| **Token & chat_id Telegram** | Foto bukti pelanggaran dikirim ke Telegram | Tidak | Gratis |
| **API key CallMeBot** | Teks peringatan dikirim ke WhatsApp | Tidak | Gratis (pemakaian pribadi) |

### Langkah 0: Buat file `alert_config.json`

Salin file contoh di folder program:

```bash
cp alert_config.example.json alert_config.json        # Linux
```
```powershell
copy alert_config.example.json alert_config.json      # Windows
```

Buka `alert_config.json` dengan editor teks (VS Code, Notepad, `nano`). Isinya seperti ini:

```json
{
  "camera_name": "kamera-01",
  "telegram": {
    "enabled": false,
    "bot_token": "ISI_TOKEN_DARI_BOTFATHER",
    "chat_id": "ISI_CHAT_ID"
  },
  "whatsapp_callmebot": { "enabled": false, "phone": "+628xxxxxxxxxx", "apikey": "ISI_APIKEY_DARI_CALLMEBOT" },
  "ai": {
    "enabled": true,
    "gemini_api_key": "ISI_API_KEY_DARI_AISTUDIO_GOOGLE",
    ...
  }
}
```

Ganti tulisan `ISI_...` dengan key Anda sesuai langkah di bawah. Tanda kutip `"..."` dan koma harus tetap ada.

### Langkah 1: API key Gemini (Google AI Studio)

1. Buka **[aistudio.google.com](https://aistudio.google.com)**, lalu login dengan akun Google.
2. Klik **Get API key** (menu kiri) → **Create API key**.
   Bila diminta memilih project, pilih project yang ada atau buat baru. **Jangan** menambahkan kartu
   kredit/billing.
3. Salin key yang muncul (biasanya diawali `AIza...` atau `AQ.`).
4. Tempel di `alert_config.json`:
   ```json
   "ai": {
     "enabled": true,
     "gemini_api_key": "TEMPEL_KEY_ANDA_DI_SINI",
   ```
5. **Pastikan tetap gratis:** di AI Studio buka **API keys** (atau **Usage & Billing**). Project Anda harus
   berstatus **Free tier** dan **tidak ada billing yang tersambung**. Selama tidak ada billing, Google
   tidak bisa menagih; program juga membatasi pemakaian sendiri (lihat bagian 5).

### Langkah 2: Token & chat_id Telegram (opsional)

1. Di aplikasi Telegram, cari **@BotFather** (centang biru) → **Start**.
2. Kirim `/newbot`, lalu isi:
   - **nama bot**, bebas, contoh `Peringatan Rokok Kelompok 3`
   - **username bot**, harus diakhiri `bot`, contoh `rokok_kelompok3_bot`
3. BotFather membalas dengan **token**, contoh `7412345678:AAH3kd9_xxxxxxxxxxxxxxxx`. Salin.
4. Buka bot baru Anda (klik link `t.me/...` dari BotFather), tekan **Start**, lalu kirim pesan apa saja,
   misalnya `halo`.
   Agar peringatan masuk ke **grup**: tambahkan bot ke grup, lalu kirim pesan di grup itu.
5. Buka alamat ini di browser (ganti `<TOKEN>` dengan token Anda):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Cari bagian `"chat":{"id":123456789, ...`. Angka itu adalah **chat_id**
   (chat_id grup diawali tanda minus, contoh `-1001234567890`).
   Bila hasilnya `"result":[]`, kirim pesan lagi ke bot lalu refresh.
6. Isi di `alert_config.json`:
   ```json
   "telegram": {
     "enabled": true,
     "bot_token": "7412345678:AAH3kd9_xxxxxxxxxxxxxxxx",
     "chat_id": "123456789"
   },
   ```

### Langkah 3: WhatsApp lewat CallMeBot (opsional, teks saja)

1. Ikuti petunjuk di **[callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/)**:
   simpan nomor CallMeBot di kontak, lalu kirim pesan WhatsApp `I allow callmebot to send me messages`.
2. Anda menerima **apikey** lewat WhatsApp.
3. Isi `phone` (format `+628...`), `apikey`, dan ubah `"enabled": true`.

CallMeBot hanya bisa mengirim ke nomor Anda sendiri dan tidak bisa mengirim foto. Untuk foto bukti,
pakai Telegram. API resmi WhatsApp (Meta) berbayar.

### Langkah 4: Terapkan dan cek

1. Simpan `alert_config.json`.
2. Di panel, tekan **Mulai ulang** (pengaturan baru dibaca saat detektor dinyalakan).
3. Cek:
   - **Gemini:** menu **Tentang** → baris *Gemini* harus menunjukkan **Aktif (gemini-…-flash-lite)**.
     Bila tertulis "Belum ada API key" atau "API key tidak valid", periksa lagi Langkah 1.
   - **Telegram:** pilih **Mode → Demo rokok**, **Mulai ulang**, lalu pegang HP di depan kamera
     (mode demo menganggap HP sebagai rokok). Foto bukti bertanda `[DEMO]` harus masuk ke Telegram.
     Bila tidak, buka **Log Detektor** dan cari `Peringatan telegram: gagal: ...`.

| Pesan di log | Artinya |
|---|---|
| `HTTP Error 401` (Telegram) | Token salah. Salin ulang dari BotFather. |
| `chat not found` (Telegram) | chat_id salah, atau belum menekan **Start** di bot. |
| `Gemini dimatikan sampai besok: API key tidak valid` | Key Gemini salah atau sudah dihapus. Buat key baru. |
| `urlopen error` | Perangkat tidak tersambung ke internet. |

> **Jaga kerahasiaan `alert_config.json`.** File ini sudah diabaikan oleh Git (`.gitignore`), jadi tidak
> akan ikut ter-upload. Jangan kirim isinya lewat grup atau chat umum. Bila bocor: Telegram →
> @BotFather `/revoke`; Gemini → hapus key di AI Studio lalu buat yang baru.

---

## 2. Cara paling mudah: Panel Kontrol

### Membuka panel

| Perangkat | Cara |
|---|---|
| **Windows** | Klik dua kali **`Mulai Panel.bat`**. Browser terbuka otomatis ke `http://localhost:8000`. Jangan tutup jendela hitamnya selama panel dipakai. |
| **Linux** | Jalankan `./start_panel.sh`, lalu buka `http://localhost:8000` |
| **HP** | Buka `http://<IP-perangkat>:8000`. Alamatnya tertulis di panel, di bagian **"Buka dari HP"**. |

Untuk mematikan: tutup jendela panel (Windows) atau tekan Ctrl+C. Detektor ikut berhenti.

### Tombol di panel

| Tombol | Fungsi |
|---|---|
| **Start** | Menyalakan detektor dengan mode & kamera yang dipilih |
| **Stop** | Menghentikan detektor (kamera dilepas) |
| **Restart** | Stop lalu Start, untuk menerapkan perubahan mode/kamera |
| **Capture foto** | Menyimpan gambar saat ini ke `captures/FOTO_....jpg` |
| **Rekam video / Stop rekam** | Merekam video ke `captures/VIDEO_....mp4` |
| **Layar penuh** | Video langsung memenuhi layar |
| **Mode** | *Kirim ke GCS* · *Deteksi rokok & vape* · *Demo rokok* |
| **Sumber kamera** | Kamera 0 (drone/webcam) · Kamera 1 · Kamera HP (isi URL) |
| **Tes kamera HP** | Mengecek apakah URL kamera HP bisa dihubungi |
| **Start otomatis** | Detektor langsung menyala setiap kali panel dinyalakan |
| **Aktifkan suara peringatan** | Bunyi "beep" saat ada pelanggaran rokok/vape (browser butuh klik ini dulu) |
| **Galeri** | Foto/video capture dan foto **BUKTI** pelanggaran. Klik untuk melihat atau mengunduh, ✕ untuk menghapus. |
| **Log** | Pesan terbaru dari detektor (untuk mencari penyebab error) |

Lampu status: **hijau** = menyala, **abu-abu** = mati, **merah** = error (lihat Log).

### Auto-start di drone (tanpa layar, tanpa terminal)

1. Edit `drone-panel.service`: ganti `NAMA_USER` dan `/home/NAMA_USER/Yolo` sesuai drone.
2. Pasang:
   ```bash
   sudo cp drone-panel.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now drone-panel
   ```
3. Buka panel dari HP/laptop, pilih mode dan kamera, lalu centang **Start otomatis saat panel dinyalakan**.

Sejak itu, setiap drone dinyalakan, panel dan detektor langsung berjalan.
Cek status: `systemctl status drone-panel`. Log: `journalctl -u drone-panel -f`.

### Firewall (agar panel bisa dibuka dari HP)

- Linux (ufw): `sudo ufw allow 8000/tcp && sudo ufw allow 8080/tcp`
- Windows (PowerShell **sebagai Administrator**, sekali saja):
  ```powershell
  New-NetFirewallRule -DisplayName "Panel Drone" -Direction Inbound -Protocol TCP -LocalPort 8000,8080 -Action Allow -Profile Private
  ```

---

## 3. Mode "Kirim ke GCS" (drone)

Tanpa panel, program bisa dijalankan langsung:

```bash
python drone1_yolo_sender.py
```

Tanpa opsi, program memakai kamera `0` dan mengirim ke GCS `10.42.0.1` (port 5001 dan 5101).
Tanda program sudah berjalan normal:

```
[INFO] Kamera 0 terbuka (640x480)
[INFO] FPS 14.0 | deteksi 1 | video terkirim 142, gagal 0, kualitas turun 0, dilewati 0 | ...
```

Opsi:

```
--source SOURCE      indeks kamera (0, 1, ...) atau URL kamera HP/IP camera. Default: 0
--web-port PORT      port tampilan browser/HP. Default: 8080
--no-web             matikan tampilan browser/HP
```

---

## 4. Memakai kamera HP (tanpa drone)

Kamera HP dipakai sebagai pengganti kamera drone, dan YOLO berjalan di laptop.

**Di HP (Android):**
1. Install **IP Webcam** (Pavel Khlebovich) dari Play Store.
2. (Disarankan) **Video preferences → Video resolution → 640x480**.
3. Gulir ke paling bawah, tekan **Start server**, lalu izinkan akses kamera.
4. Catat alamat di bawah layar, misalnya `http://192.168.2.199:8080`.
5. Biarkan aplikasi tetap berjalan (boleh tekan Home).

> iPhone: gunakan aplikasi IP camera yang menyediakan stream **MJPEG via HTTP**.

**Di panel:** **Sumber kamera → Kamera HP**, isi URL dengan `/video` di akhir
(contoh `http://192.168.2.199:8080/video`), tekan **Tes kamera HP**, lalu **Start**.

HP dan laptop harus berada di Wi-Fi yang sama. IP HP bisa berubah setelah Wi-Fi tersambung ulang.

> Saat uji coba di luar jaringan drone, pengiriman ke GCS `10.42.0.1` tetap berjalan
> tetapi tidak sampai ke mana pun. Ini normal.

---

## Memilih kamera (termasuk di laptop teman)

| Pilihan di panel | Artinya |
|---|---|
| **Kamera 0** | Webcam bawaan laptop, atau kamera utama drone. **Pilih ini di laptop teman.** |
| **Kamera 1** | Webcam USB tambahan, bila ada |
| **Kamera HP (IP Webcam)** | Kamera ponsel lewat Wi-Fi (bagian 4) |

Tidak yakin? Buka **Pengaturan → Cari kamera** (detektor dalam keadaan mati). Panel mencoba Kamera 0–3,
menampilkan yang berfungsi beserta resolusinya, lalu mengisi pilihan Kamera secara otomatis.

Di Windows kamera dibuka lewat DirectShow sehingga gambar pertama muncul ±1 detik
(backend bawaan Windows ±9 detik). Di Linux memakai backend bawaan (V4L2).

---

## 5. Deteksi AI dengan Gemini (mode bawaan)

### Cara kerja

1. **Video tampil langsung** dari kamera (±25–30 FPS), terpisah dari proses AI, jadi video tidak
   ikut melambat walau AI sedang bekerja. Video sudah tampil selagi model AI disiapkan.
2. YOLO mencari orang, dan model lokal `smoking_vape.pt` menandai **kandidat** rokok/vape/asap.
   Kandidat ini sering keliru (tangan di mulut, HP, earbuds), jadi **tidak langsung jadi peringatan**
   dan tidak digambar di video.
3. Bila kandidat muncul berulang (2 dari 5 frame), atau setiap 60 detik saat ada orang, potongan foto
   kepala dan badan atas dikirim ke **Gemini** (Google AI Studio). Gemini menjawab merokok/vape/asap,
   keyakinan, dan alasan dalam bahasa Indonesia.
4. Hanya bila Gemini menjawab **ya** dengan keyakinan ≥ 0,6, pelanggaran dicatat: foto bukti
   (dengan alasan AI), Telegram, GCS, dan banner merah di panel.

### Jenis pelanggaran

| Jenis | Kapan dihitung | Tertulis di peringatan |
|---|---|---|
| **Rokok** | Sedang mengisap rokok (rokok di mulut/bibir) | "merokok" |
| **Vape** | Sedang mengisap vape/rokok elektrik | "memakai vape" |
| **Asap** | Mengembuskan asap/uap dari mulut atau hidung | "asap (ngepul)" |
| **Memegang** | Membawa/memegang rokok atau vape yang terlihat jelas, walau belum diisap atau belum menyala | "memegang rokok/vape" |

Bila orang memegang **dan** mengisap, yang dicatat jenis yang lebih berat (rokok/vape/asap).
Benda pengecoh (HP, earbuds, pulpen, sedotan, tusuk sate, tangan menutup mulut) tidak dihitung.
Satu kali pemeriksaan Gemini butuh ±2–8 detik; setelah peringatan ada jeda `cooldown_s` (30 detik)
sebelum peringatan berikutnya.
5. Tanpa internet, tanpa API key, atau kuota habis, detektor otomatis memakai model lokal saja dengan
   ambang lebih ketat. Panel menampilkan status Gemini di **Terlihat Sekarang** dan **Tentang**.

### Membuat API key Gemini (gratis)

Ikuti **bagian 1b, Langkah 1**.

### Pengaman agar tidak pernah berbayar

Gemini API **hanya menagih bila project Google Anda tersambung ke akun billing** (kartu kredit/pembayaran).
Pastikan di AI Studio (menu API keys / Usage) project Anda berstatus **Free tier** dan **jangan pernah
mengaktifkan billing**. Di tier gratis, melewati kuota hanya menghasilkan penolakan, bukan tagihan.

Program menambahkan pengaman berlapis (bisa diubah di `alert_config.json` → `"ai"`):

| Pengaman | Bawaan |
|---|---|
| Hanya model Flash-Lite (paling ringan) | `gemini-3.5-flash-lite`, cadangan `gemini-flash-lite-latest` |
| Batas panggilan per menit | `max_per_minute`: 8 |
| Batas panggilan per hari (disimpan di `ai_usage.json`) | `max_per_day`: 250 |
| Jeda minimal antar panggilan | `min_interval_s`: 4 detik |
| Foto diperkecil sebelum dikirim | ±512 px, ±1.200 token per pemeriksaan |
| Kuota per menit habis (HTTP 429) | Gemini dijeda sesuai saran Google |
| Kuota harian habis, pesan *billing/payment/prepay/credit*, akses ditolak, key tidak valid | **Gemini dimatikan sampai besok**, deteksi lokal dipakai |

Setiap percobaan dihitung **sebelum** dikirim, jadi batasnya tetap berlaku walau permintaan gagal.

> **Privasi:** di tier gratis, Google dapat memakai foto yang dikirim untuk meningkatkan produknya.
> Foto orang yang mencurigakan akan terkirim ke Google.

---

## 5b. Deteksi rokok & vape dengan model lokal saja

### Cara kerja

1. **Model orang** (`yolo11n.pt`) mencari orang, dan **model rokok/vape** (`smoking_vape.pt`) mencari
   tiga hal: **rokok**, **vape**, dan **asap** (kepulan/ngepul).
2. Yang dihitung **pelanggaran**:
   - **rokok/vape** yang berada di dalam kotak orang (diperbesar 15%). Rokok di atas meja atau di poster tidak dihitung.
   - **asap** di sekitar kepala/badan atas orang (boleh melayang ke samping atau di atas kepala).
     Jadi orang yang menyembunyikan rokok/vape tetap ketahuan saat mengepulkan asap.
     Asap di dekat kaki atau jauh dari orang tidak dihitung.
3. Pelanggaran harus terlihat di **3 dari 5 frame** berturut-turut, supaya tidak ada alarm palsu dari satu frame.
4. Setelah peringatan, ada jeda (**cooldown**) 30 detik supaya pesan tidak dikirim berulang-ulang.
5. Seluruh layar kamera dianggap area larangan. Gunakan **kamera diam** (CCTV, HP di tripod,
   webcam) yang mengarah ke area tersebut. Rokok terlalu kecil untuk dilihat dari drone yang terbang tinggi.

Warna kotak di video: **hijau** = orang, **merah** = pelanggaran, **kuning** = rokok/vape tanpa orang.

### Langkah 1: Coba dulu dengan mode Demo (tanpa model khusus)

Di panel pilih **Mode → Demo rokok**, lalu Start. Pada mode ini, **HP (cell phone) dianggap rokok**.
Pegang HP di depan kamera, dan seluruh alur peringatan bisa dicoba: banner merah, suara, foto bukti,
GCS, Telegram, dan WhatsApp. Semua pesan diberi tanda `[DEMO]`.

### Langkah 2: Latih model `smoking_vape.pt` di Google Colab (gratis)

1. Buka [colab.research.google.com](https://colab.research.google.com) → **File → Upload notebook** → pilih `train_smoking_vape.ipynb`.
2. **Runtime → Change runtime type → T4 GPU**.
3. Buat akun gratis di [roboflow.com](https://roboflow.com), lalu salin API key dari **Settings → API Keys**.
4. **Runtime → Run all**, lalu tempel API key saat diminta.
5. Di langkah 6 notebook, unggah **.zip contoh negatif** bila ada (lihat di bawah), atau tekan Cancel.
6. Tunggu ±1–2 jam. Periksa nilai **mAP50** per kelas: di atas 0.6 sudah layak dicoba.
7. File `smoking_vape.pt` otomatis terunduh. Taruh di folder ini.
8. Di panel pilih **Mode → Deteksi rokok & vape**, lalu Start.

**Meningkatkan deteksi asap (sangat disarankan).** Data asap yang diembuskan masih sedikit di internet
(hanya ada di dataset kedua). Cara menambahnya:
1. Rekam video pendek orang yang mengepulkan asap rokok/vape, sebaiknya dari kamera dan lokasi yang akan dipakai.
2. Unggah ke project baru di [roboflow.com](https://roboflow.com) (gratis), lalu gambar kotak di setiap kepulan dengan label `asap`.
3. **Generate** sebuah versi dataset, lalu tambahkan `("workspace-anda", "nama-project")` ke `DATASETS` di notebook.

Jangan memakai dataset **asap kebakaran**, karena bentuknya berbeda dan akan menambah alarm palsu.

**Mengurangi alarm palsu (contoh negatif).** Benda seperti kotak earbuds, HP, pulpen, atau uap kopi
bisa salah dikira rokok, vape, atau asap. Potret benda-benda itu 30–100 kali dengan tombol **Capture foto**
di panel, jadikan satu `.zip` dari isi folder `captures/`, lalu unggah di langkah 6 notebook.
Model akan belajar bahwa benda itu **bukan** pelanggaran.

Dataset yang dipakai (publik, dari Roboflow Universe, ±13.000 foto). Isi labelnya sudah diperiksa
secara visual, dan label yang namanya menyesatkan dipetakan ulang lewat `CLASS_OVERRIDES` di notebook.

| Dataset | Dipakai untuk | Lisensi |
|---|---|---|
| [takoyati/cigarette-vape-detection](https://universe.roboflow.com/takoyati/cigarette-vape-detection) v10 | rokok, vape | CC BY 4.0 |
| [livestreamdetection/smoking-vape-detection](https://universe.roboflow.com/livestreamdetection/smoking-vape-detection) v3 | asap (`smoking`), vape (`vaping`) | CC BY 4.0 |
| [tiara-fb7pp/vaping-ulrul](https://universe.roboflow.com/tiara-fb7pp/vaping-ulrul) v7 | asap (kepulan vape), vape | CC BY 4.0 |
| [spd/smoking-person-detection](https://universe.roboflow.com/spd/smoking-person-detection-h0a2x) v13 | asap rokok tipis, rokok, vape | Public Domain |
| [project-i6bzi/smoking-person-detection](https://universe.roboflow.com/project-i6bzi/smoking-person-detection-ec7ec) v1 | rokok (label `smoke` = batang rokok) | CC BY 4.0 |

Lisensi CC BY 4.0 mewajibkan mencantumkan sumber (seperti tabel di atas) bila model dipakai atau dipublikasikan.

### Langkah 3: Atur peringatan (opsional)

Salin `alert_config.example.json` menjadi **`alert_config.json`**, lalu isi:

| Kunci | Arti |
|---|---|
| `camera_name` | Nama kamera di pesan peringatan |
| `cooldown_s` | Jeda minimal antar peringatan (detik) |
| `confirm_frames` / `window_frames` | Pelanggaran harus terlihat di X dari Y frame |
| `gcs_alert` | Kirim ke GCS `10.42.0.1` port 5201 (aktif secara default) |
| `telegram` | Kirim **foto** bukti ke Telegram |
| `whatsapp_callmebot` | Kirim **teks** peringatan ke WhatsApp |

Restart detektor setelah mengubah file ini. Bila tidak ada `alert_config.json`, yang aktif hanya
foto bukti, web, dan GCS.

Cara mendapatkan token Telegram dan apikey WhatsApp ada di **bagian 1b**.

### Langkah 4: Terima peringatan di laptop GCS

```bash
python3 gcs_alert_receiver.py
```

Program ini menampilkan setiap peringatan (objek, kamera, waktu, nama foto bukti) dan menyimpannya
ke `gcs_alerts.jsonl`.

### Tanpa panel

```bash
python smoking_detector.py --source http://192.168.2.199:8080/video
python smoking_detector.py --demo
```

Opsi tambahan: `--model FILE`, `--conf 0.35` (ambang keyakinan), `--no-person-check`
(hitung rokok/vape walau orang tidak terlihat, misalnya kamera sangat dekat ke wajah).

---

## 6. Alamat & port

| Port | Arah | Isi |
|---|---|---|
| 8000 (TCP) | browser → perangkat | Panel Kontrol |
| 8080 (TCP) | browser → perangkat | Video langsung `/stream.mjpg`, `/status.json`, `/snapshot.jpg`; mode rokok juga `/alerts.json`, `/bukti/<file>` |
| 5001 (UDP) | perangkat → GCS | Video JPEG (mode Kirim ke GCS) |
| 5101 (UDP) | perangkat → GCS | JSON deteksi (mode Kirim ke GCS) |
| 5201 (UDP) | perangkat → GCS | JSON peringatan rokok/vape |

---

## 7. Format data ke GCS (untuk penerima)

### Video, UDP port 5001

Satu datagram per frame:

| Byte | Isi |
|---|---|
| 0–3 | `frame_id` (uint32, big-endian) |
| 4–7 | panjang data JPEG (uint32, big-endian) |
| 8– | data JPEG |

```python
frame_id, length = struct.unpack("!II", packet[:8])
frame = cv2.imdecode(np.frombuffer(packet[8:8 + length], np.uint8), cv2.IMREAD_COLOR)
```

Bila JPEG melebihi batas UDP (~64 KB), kualitas JPEG diturunkan otomatis (60 → 50 → … → 10).
Formatnya tetap sama.

### Deteksi, UDP port 5101

```json
{
  "drone": "companion-01",
  "ip": "10.42.0.11",
  "frame_id": 12,
  "timestamp": 1727856000.12,
  "detections": [
    {"class": "person", "confidence": 0.873, "bbox": [x1, y1, x2, y2]}
  ]
}
```

- `frame_id` sama dengan header video, sehingga deteksi bisa dicocokkan dengan frame-nya.
- `timestamp` memakai jam drone. Sinkronkan jam drone dan GCS (NTP/chrony) bila ingin menghitung latensi.

### Peringatan rokok/vape, UDP port 5201

```json
{
  "type": "smoking_alert",
  "camera": "kamera-01",
  "ip": "192.168.2.178",
  "timestamp": 1727856000.12,
  "time": "2026-10-02T11:40:06",
  "frame_id": 4,
  "objects": [{"class": "rokok", "confidence": 0.8, "bbox": [180, 200, 220, 240]}],
  "persons": 1,
  "photo": "20261002_114006_rokok.jpg",
  "demo": false,
  "sent": {}
}
```

`photo` adalah nama file di folder `bukti/` pada perangkat kamera. File itu bisa dibuka di
`http://<IP-perangkat>:8080/bukti/<photo>`.

---

## 8. Pengaturan di dalam `drone1_yolo_sender.py`

| Konstanta | Default | Keterangan |
|---|---|---|
| `GCS_IP` | `10.42.0.1` | Alamat GCS |
| `VIDEO_PORT` / `DETECTION_PORT` | `5001` / `5101` | Port UDP ke GCS |
| `WIDTH` × `HEIGHT` | `640` × `480` | Resolusi kamera |
| `JPEG_QUALITY` | `60` | Kualitas awal JPEG |
| `DRONE_NAME` / `DRONE_IP` | `companion-01` / `10.42.0.11` | Identitas drone di JSON |
| `CAMERA_INDEX` | `0` | Kamera default |
| `WEB_ENABLED` / `WEB_PORT` | `True` / `8080` | Tampilan browser/HP |
| `MAX_WEB_CLIENTS` | `3` | Batas penonton video (panel + rekam juga dihitung) |
| `CAMERA_MAX_FAILS` | `30` | Frame gagal berturut-turut sebelum kamera dibuka ulang |

Jika Wi-Fi drone lemah, kurangi beban dengan `MAX_WEB_CLIENTS = 1` atau `WEB_ENABLED = False`.

---

## 9. Fitur ketahanan

- **Kamera terputus**: kamera dibuka ulang otomatis, program tidak berhenti.
- **Wi-Fi/GCS tidak terjangkau**: frame dilewati dan dihitung "gagal", program tetap jalan.
- **Frame terlalu besar untuk UDP**: kualitas JPEG diturunkan otomatis.
- **Kamera jaringan (HP)**: selalu memakai frame terbaru agar video tidak makin tertinggal.
- **Peringatan gagal terkirim** (internet mati, token salah): dicatat di log dan `bukti/log.jsonl`, deteksi tetap jalan.
- **Ctrl+C / Stop / `systemctl stop`**: kamera, socket, dan server web ditutup.

---

## 10. Masalah umum

| Gejala | Solusi |
|---|---|
| `Mulai Panel.bat` langsung tertutup / error | Folder `.venv` belum ada. Lakukan Instalasi (bagian 1). |
| `Port 8000 sudah dipakai` | Panel sudah berjalan di jendela lain. Buka saja `http://localhost:8000`. |
| Lampu merah "Error" di panel | Baca bagian **Log** di panel. Biasanya kamera tidak ditemukan atau model belum ada. |
| Mode "Deteksi rokok & vape" tidak bisa dipilih | `smoking_vape.pt` belum ada. Latih dulu (bagian 5), atau pakai mode Demo. |
| `Kamera ... tidak bisa dibuka` terus-menerus | Cek kabel kamera / pilihan Kamera 0/1. Untuk HP: tekan **Tes kamera HP**, pastikan server IP Webcam sudah di-Start, Wi-Fi sama, dan ada `/video`. |
| Panel terbuka di laptop tapi tidak di HP | Firewall (bagian 2), atau router memakai *client isolation*. |
| Video di panel tidak muncul tapi status Menyala | Penonton video sudah 3 (`MAX_WEB_CLIENTS`). Tutup tab lain atau hentikan rekaman. |
| Video terlambat / patah-patah | Turunkan resolusi di IP Webcam ke 640x480, dan kurangi penonton. |
| Telegram/WhatsApp tidak masuk | Lihat Log (`Peringatan telegram: gagal: ...`). Cek token/chat_id/apikey dan koneksi internet. |
| "Gemini dimatikan sampai besok" | Kuota gratis habis atau ada tanda berbayar. Deteksi lokal tetap jalan. Besok otomatis aktif lagi. Bila alasannya "API key tidak valid", buat key baru di AI Studio. |
| "Gemini tidak aktif ... memakai deteksi lokal" | Tidak ada internet atau batas harian lokal (`max_per_day`) tercapai. |
| Video tampil tapi kamera laptop teman tidak terbaca (Linux) | `sudo apt install libgl1 libglib2.0-0`, `sudo usermod -aG video $USER` lalu login ulang, dan coba **Cari kamera**. |
| Terlalu banyak alarm palsu | Naikkan `--conf` (mis. 0.5) atau `confirm_frames`. Tambah data latih di notebook. |
| Rokok tidak terdeteksi | Dekatkan kamera ke area. Turunkan `--conf` (mis. 0.25). Latih ulang dengan data yang mirip kondisi kamera. |
| FPS rendah | YOLO berjalan di CPU. Mode rokok memakai 2 model, jadi lebih lambat. Kurangi beban lain, atau gunakan perangkat dengan GPU. |

---

## 11. Saat menyerahkan (handover) folder ini

Cara utama: repo GitHub **private** `bryanfrisco/drone-yolo` (bagian 1). Anggota kelompok diundang
sebagai collaborator, lalu `git clone` / `git pull`.

Setiap anggota membuat `alert_config.json` sendiri dengan key masing-masing (bagian 1b).
Bila kelompok ingin memakai bot Telegram yang sama, bagikan token dan chat_id lewat chat pribadi,
bukan lewat GitHub.

Yang **tidak** ikut (diatur `.gitignore`):

- `alert_config.json`: berisi token dan API key pribadi.
- `.venv/`: khusus perangkat masing-masing dan berukuran besar. Buat ulang dengan Instalasi.
- `captures/`, `bukti/`, `panel_settings.json`, `ai_usage.json`: data pemakaian dan foto wajah.
- `graphify-out/`, `__pycache__/`
