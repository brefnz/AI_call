# AI Outbound Calling & Voice Blasting — Gemini Live API

Implementasi MVP dari PRD, scope Phase 1–3 (Telephony, Gemini Voice, Context/Guardrail),
sekarang dilengkapi dashboard web (Blasting, Wallboard, Riwayat Call).

Gemini Live API dipakai sebagai **satu-satunya mesin STT + TTS** (audio-in/audio-out
realtime) — tidak ada Deepgram/gTTS/ElevenLabs terpisah.

## Struktur

```
config.py                 konfigurasi via .env
db/schema.sql              skema tabel (PRD #26)
db/database.py              akses DB async (SQLite, gampang diport ke MySQL/Postgres)
core/ari_client.py          client ARI custom (REST+WS), originate outbound + externalMedia
core/rtp_bridge.py          transport audio RTP <-> PCM mentah (jembatan ke Gemini)
core/audio_utils.py         resampling PCM antara Gemini dan RTP
core/gemini_live.py         sesi Gemini Live: audio in/out + tool calling (guardrail)
core/topic_config.py        Topic/Question + system instruction builder (PRD #7, #9)
core/conversation_state.py  state machine call-level + question-level (PRD #10)
core/call_session.py        orkestrasi 1 panggilan end-to-end
core/campaign_manager.py    scheduler, queue, concurrency, retry (PRD #3, #12, #13, #15)
core/datakelola_client.py   stub integrasi Datakelola (PRD #25) — endpoint placeholder
core/reporting.py           report per-call & dashboard campaign (PRD #23, #24)
scripts/seed_example.py     seed contoh topic+campaign+target (PRD #7, #33)
main.py                     entry point proses calling (scheduler + telephony)

api_server.py               backend HTTP (FastAPI) untuk dashboard — baca/tulis DB yang sama
dashboard.html               dashboard web: Blasting / Wallboard / Riwayat Call
start-all.ps1                 jalankan main.py + api_server sekaligus (Windows)
```

## Setup awal

```
python -m venv venv && source venv/bin/activate   # atau venv\Scripts\activate di Windows
pip install -r requirements.txt
pip install fastapi uvicorn                        # untuk dashboard (api_server.py)

cp .env.example .env
```

Isi `.env`:
- `ARI_URL` / `ARI_USER` / `ARI_PASS` / `ARI_APP`
- `ARI_OUTBOUND_ENDPOINT_TEMPLATE` (trunk PJSIP)
- `RTP_ADVERTISE_HOST` (IP server ini, harus reachable dari Asterisk)
- `RTP_CODEC=ulaw` dan `RTP_SAMPLE_RATE_HZ=8000` — **lihat catatan penting di
  bawah**, jangan dikembalikan ke `slin16`/`16000` tanpa alasan kuat
- `GEMINI_API_KEY`
- `LOG_LEVEL=INFO` (bisa dinaikkan ke `WARNING` kalau terminal terlalu ramai —
  lihat bagian "Menjalankan tanpa log membanjiri terminal" di bawah)

```
python scripts/seed_example.py     # opsional: buat 1 campaign+topic+target contoh
# atau langsung pakai dashboard (lihat bagian Dashboard di bawah) untuk bikin
# topic/campaign/target tanpa edit script
```

## Menjalankan (development)

### Cuma proses calling
```
python main.py
```
Begitu jalan, campaign manager polling setiap `SCHEDULER_POLL_INTERVAL_SEC` detik,
ambil target PENDING dalam jam operasional, originate via ARI, dan begitu
tersambung langsung membuka sesi Gemini Live + audio bridge.

### Proses calling + dashboard sekaligus (Windows)
```
powershell -ExecutionPolicy Bypass -File start-all.ps1
```
Ini membuka 2 window terpisah: `main.py` (log dibuang ke `app.log`, terminal
diam) dan `uvicorn api_server:app --reload --port 8090`. Setelah itu buka
`dashboard.html` di browser.

Kalau mau manual (2 terminal terpisah, semua OS):
```
# terminal 1
python main.py

# terminal 2
uvicorn api_server:app --reload --port 8090
```
Keduanya aman jalan bersamaan — baca/tulis ke SQLite yang sama, dilindungi
`asyncio.Lock` di `db/database.py`.

### Menjalankan tanpa log membanjiri terminal
```
python main.py *>> app.log
```
Terminal jadi diam, log tetap lengkap kesimpen di `app.log`. Pantau live di
terminal lain dengan:
```
Get-Content app.log -Wait -Tail 20     # PowerShell
tail -f app.log                         # Linux/macOS
```

## Dashboard

`dashboard.html` (buka langsung di browser, atau serve lewat
`python -m http.server`) + `api_server.py` (jalan di `localhost:8090` secara
default, bisa diubah dari field "API" di pojok kanan atas dashboard) — 3 tab:

1. **Blasting** — bikin Topic (skrip AI + daftar pertanyaan mandatory/opsional),
   bikin Campaign (pilih topic, rentang tanggal, jam operasional, concurrent
   limit, max retry), lalu masukin daftar nomor ke campaign (paste satu nomor
   per baris, data pelanggan tambahan opsional dalam JSON).
2. **Wallboard** — status real-time: total target, terhubung, tidak terangkat,
   gagal/sibuk, sedang berlangsung, menunggu, answer rate, rata-rata durasi.
   Auto-refresh tiap 5 detik, bisa difilter per campaign.
3. **Riwayat Call** — tabel semua call yang sudah dilakukan (nomor, campaign,
   status, outcome, durasi, hasil percakapan). Klik ikon mata untuk buka
   transcript lengkap (percakapan AI vs CALLER) + daftar jawaban yang tercatat
   lewat tool `record_answer`.

`api_server.py` murni membungkus `db/database.py` dan `core/reporting.py` yang
sudah ada — tidak mengubah skema atau alur `campaign_manager.py`/`call_session.py`
sama sekali. CORS dibuka lebar (`allow_origins=["*"]`) untuk kemudahan dev lokal;
tambahkan auth sebelum expose ke jaringan yang lebih luas dari laptop sendiri.

## CATATAN PENTING: codec RTP = ulaw, bukan slin16

Konfigurasi default sempat `RTP_CODEC=slin16` (16kHz, cocok langsung dengan
Gemini Live tanpa resample). Ini **diganti ke `ulaw`/8000Hz** setelah
investigasi bug di mana Asterisk sama sekali tidak meneruskan RTP dari
`externalMedia`/`UnicastRTP` balik ke channel PJSIP penelepon, meski bridge,
format, dan transcode-path semuanya terbukti benar di level channel
(`rtp set debug on` menunjukkan nol paket "Sent RTP" ke sisi telepon).
Root cause persisnya adalah kombinasi `simple_bridge` + UnicastRTP(slin16) +
transcode ke PJSIP(ulaw) di versi Asterisk yang dipakai — bukan bug di kode
Python. Memindahkan codec externalMedia ke `ulaw` (native format endpoint
PJSIP di server ini) menghilangkan kebutuhan transcode di bridge sama sekali
dan menyelesaikan masalah ini.

Konsekuensinya:
- `core/rtp_bridge.py` sekarang encode/decode ulaw↔PCM16 sendiri (pakai
  `audioop`, stdlib, tidak perlu dependency tambahan) — transparan untuk kode
  di atasnya.
- `core/call_session.py` menambahkan resampler kedua (`_resampler_in`,
  8kHz→16kHz) untuk audio arah caller→Gemini, karena Gemini Live selalu
  butuh PCM16@16kHz di input-nya terlepas dari sample rate RTP.
- Kalau server Asterisk lain (versi/konfigurasi beda) tidak punya masalah
  serupa, `slin16`/16000 bisa dicoba lagi — tapi validasi dulu dengan
  `asterisk -rx "rtp set debug on"` sebelum anggap "audio putus-putus"
  sebagai bug di tempat lain.
- `_loopback_test` di `RtpEndpoint`/`call_session.py` adalah flag DEBUG
  SEMENTARA (echo audio caller balik apa adanya) yang dipakai untuk
  membuktikan Asterisk mau meneruskan RTP sama sekali. **Harus tetap `False`**
  di operasional normal — kalau `True`, audio AI jadi putus-putus karena
  echo task dan audio Gemini berebut `_send_lock` yang sama.

## Hal yang SUDAH diimplementasikan

- Outbound originate via ARI custom client, masuk Stasis app saat tersambung.
- Bridge audio dua arah: Asterisk `externalMedia` (RTP, ulaw 8kHz — lihat
  catatan codec di atas) <-> RTP endpoint lokal <-> Gemini Live API
  (PCM16 16kHz in / 24kHz out, di-resample kedua arah).
- Gemini Live session dengan system instruction dari Topic Configuration
  (Bahasa Indonesia, mandatory/optional questions, aturan guardrail PRD #9).
- **Application-layer guardrail** (PRD #17): jawaban HANYA dicatat lewat tool
  call `record_answer` yang wajib dipanggil Gemini — bukan diasumsikan dari
  teks bebas. Ada juga `request_human_agent` dan `end_conversation`.
- Question/mandatory tracking, transcript logging, call outcome & conversation
  quality (PRD #10, #16, #21, #22).
- Scheduler dengan jam operasional + timezone Asia/Jakarta, concurrency per
  campaign, retry dengan max_retry (PRD #12, #13, #15).
- Skema DB lengkap sesuai PRD #26, report per-call & dashboard campaign
  (PRD #23, #24) — sekarang juga bisa diakses lewat dashboard web, bukan cuma
  fungsi Python.
- Dashboard web (Blasting/Wallboard/Riwayat Call) via `api_server.py` +
  `dashboard.html`.

## Hal yang SENGAJA disederhanakan / masih perlu kerja lanjutan

Ini bukan hal yang bisa "diselesaikan otomatis" — butuh iterasi + testing
terhadap Asterisk & trunk yang sebenarnya:

1. **RTP jitter buffer** sudah ada versi sederhana (`SimpleJitterBuffer` di
   `core/rtp_bridge.py`, susun ulang berdasarkan sequence number + isi
   silence untuk paket hilang) — untuk jaringan yang sangat tidak stabil,
   mungkin masih perlu tuning `jitter_ms`.
2. **Transfer ke human agent** (`request_human_agent`) baru mencatat intent —
   redirect ARI ke agent manusia belum diimplementasikan (perlu tahu skema
   endpoint/extension agent yang dipakai FreePBX).
3. **Datakelola client** endpoint-nya placeholder — sesuaikan path/payload
   begitu dokumentasi API Datakelola tersedia.
4. State machine granular ASKING/LISTENING/PROCESSING/RESPONDING/VALIDATING
   di PRD #10 disederhanakan jadi call-level + question-level tracking, karena
   Gemini Live menangani turn-taking audio secara internal — app tidak
   punya visibilitas ke sub-state itu (dijelaskan di docstring
   `conversation_state.py`).
5. Belum ada retry/backoff eksplisit untuk `Gemini unavailable` (PRD #28) di
   luar exception generik — tambahkan reconnect logic kalau butuh SLA lebih
   ketat.
6. Dashboard (`api_server.py`) belum ada auth — cukup untuk dev lokal, perlu
   ditambahkan (API key/session) sebelum diakses dari luar laptop sendiri.
7. Dashboard belum expose edit/delete Topic dan edit pertanyaan yang sudah
   dibuat (baru create) — kalau perlu, tambahkan endpoint `PUT /api/topics/{id}`
   dan UI-nya.

## Model & versi Gemini Live

Kode ini pakai `google-genai` SDK (`client.aio.live.connect`). Nama model
(`GEMINI_LIVE_MODEL` di `.env`) dan detail parameter (voice, response
modalities) bisa berubah mengikuti rilis API Gemini Live — cek dokumentasi
resmi Google AI sebelum deploy produksi kalau ada error terkait config/model.
