# AI Outbound Calling & Voice Blasting — Gemini Live API

Implementasi MVP dari PRD, scope Phase 1–3 (Telephony, Gemini Voice, Context/Guardrail).
Gemini Live API dipakai sebagai **satu-satunya mesin STT + TTS** (audio-in/audio-out
realtime) — tidak ada Deepgram/gTTS/ElevenLabs terpisah.

## Struktur

```
config.py               konfigurasi via .env
db/schema.sql            skema tabel (PRD #26)
db/database.py            akses DB async (SQLite, gampang diport ke MySQL/Postgres)
core/ari_client.py        client ARI custom (REST+WS), originate outbound + externalMedia
core/rtp_bridge.py        transport audio RTP <-> PCM mentah (untuk jembatan ke Gemini)
core/audio_utils.py       resampling PCM 24kHz (output Gemini) -> 16kHz (Asterisk slin16)
core/gemini_live.py       sesi Gemini Live: audio in/out + tool calling (guardrail)
core/topic_config.py      Topic/Question + system instruction builder (PRD #7, #9)
core/conversation_state.py state machine call-level + question-level (PRD #10)
core/call_session.py      orkestrasi 1 panggilan end-to-end
core/campaign_manager.py  scheduler, queue, concurrency, retry (PRD #3, #12, #13, #15)
core/datakelola_client.py stub integrasi Datakelola (PRD #25) — endpoint placeholder
core/reporting.py         report per-call & dashboard campaign (PRD #23, #24)
scripts/seed_example.py   seed contoh topic+campaign+target (PRD #7, #33)
main.py                   entry point
```

## Cara jalan (development)

```bash
python -m venv venv && source venv/bin/activate   # atau venv\Scripts\activate di Windows
pip install -r requirements.txt

cp .env.example .env
# isi .env: ARI_URL/USER/PASS/APP, ARI_OUTBOUND_ENDPOINT_TEMPLATE (trunk PJSIP),
# RTP_ADVERTISE_HOST (IP server ini, harus reachable dari Asterisk), GEMINI_API_KEY

python scripts/seed_example.py     # buat 1 campaign + topic + target contoh
# edit phone_number di scripts/seed_example.py dulu ke nomor test yang valid

python main.py
```

Begitu `main.py` jalan, campaign manager polling setiap `SCHEDULER_POLL_INTERVAL_SEC`
detik, ambil target PENDING dalam jam operasional, originate via ARI, dan begitu
tersambung langsung membuka sesi Gemini Live + audio bridge.

## Hal yang SUDAH diimplementasikan

- Outbound originate via ARI custom client, masuk Stasis app saat tersambung.
- Bridge audio dua arah: Asterisk `externalMedia` (RTP, slin16 16kHz) <-> RTP
  endpoint lokal <-> Gemini Live API (PCM16 16kHz in / 24kHz out, di-resample).
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
  (PRD #23, #24).

## Hal yang SENGAJA disederhanakan / masih perlu kerja lanjutan

Ini bukan hal yang bisa "diselesaikan otomatis" — butuh iterasi + testing
terhadap Asterisk & trunk yang sebenarnya:

1. **RTP tanpa jitter buffer.** `core/rtp_bridge.py` parsing RTP minimal (RFC
   3550 dasar). Untuk jaringan yang tidak stabil, tambahkan jitter buffer &
   packet-loss handling.
2. **Payload type / symmetric RTP** diasumsikan sesuai perilaku umum ARI
   `externalMedia`, tapi HARUS divalidasi terhadap versi Asterisk yang dipakai
   (capture dengan `tcpdump`/Wireshark saat first test call).
3. **Transfer ke human agent** (`request_human_agent`) baru mencatat intent —
   redirect ARI ke agent manusia belum diimplementasikan (perlu tahu skema
   endpoint/extension agent yang dipakai FreePBX).
4. **Datakelola client** endpoint-nya placeholder — sesuaikan path/payload
   begitu dokumentasi API Datakelola tersedia.
5. State machine granular ASKING/LISTENING/PROCESSING/RESPONDING/VALIDATING
   di PRD #10 disederhanakan jadi call-level + question-level tracking, karena
   Gemini Live menangani turn-taking audio secara internal — app tidak
   punya visibilitas ke sub-state itu (dijelaskan di docstring
   `conversation_state.py`).
6. Belum ada retry/backoff eksplisit untuk `Gemini unavailable` (PRD #28) di
   luar exception generik — tambahkan reconnect logic kalau butuh SLA lebih
   ketat.
7. Belum ada dashboard web/API HTTP — `core/reporting.py` baru fungsi Python;
   tinggal dibungkus FastAPI/Flask kalau perlu endpoint HTTP.

## Model & versi Gemini Live

Kode ini pakai `google-genai` SDK (`client.aio.live.connect`). Nama model
(`GEMINI_LIVE_MODEL` di `.env`) dan detail parameter (voice, response
modalities) bisa berubah mengikuti rilis API Gemini Live — cek dokumentasi
resmi Google AI sebelum deploy produksi kalau ada error terkait config/model.
