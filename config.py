"""
Konfigurasi aplikasi AI Outbound Calling & Voice Blasting.
Semua nilai diambil dari environment variable (lihat .env.example).
Tidak ada credential yang di-hardcode di source code (lihat PRD #31 Security & Privacy).
"""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AriConfig:
    base_url: str = os.getenv("ARI_URL", "http://127.0.0.1:8088")
    user: str = os.getenv("ARI_USER", "ai_voice_app")
    password: str = os.getenv("ARI_PASS", "")
    app: str = os.getenv("ARI_APP", "ai_voice_app")
    # Trunk/endpoint pattern dipakai untuk originate, mis. "PJSIP/{number}@my-trunk"
    outbound_endpoint_template: str = os.getenv(
        "ARI_OUTBOUND_ENDPOINT_TEMPLATE", "PJSIP/{number}@outbound-trunk"
    )
    caller_id: str = os.getenv("ARI_CALLER_ID", "AI Voice <000000000>")
    originate_timeout_sec: int = int(os.getenv("ARI_ORIGINATE_TIMEOUT_SEC", "30"))


@dataclass
class RtpBridgeConfig:
    # Host:port yang di-bind untuk menerima RTP dari Asterisk externalMedia.
    # Harus reachable dari server Asterisk (bukan 127.0.0.1 kalau Asterisk di server lain).
    listen_host: str = os.getenv("RTP_LISTEN_HOST", "0.0.0.0")
    listen_port_start: int = int(os.getenv("RTP_LISTEN_PORT_START", "40000"))
    listen_port_end: int = int(os.getenv("RTP_LISTEN_PORT_END", "40200"))
    # Host yang diiklankan ke Asterisk sebagai tujuan externalMedia (external_host).
    advertise_host: str = os.getenv("RTP_ADVERTISE_HOST", "127.0.0.1")
    # Format audio yang diminta ke Asterisk untuk channel externalMedia.
    # REVISI: dipindah dari slin16 ke ulaw. Investigasi TX=0 (audio AI tidak
    # sampai ke PJSIP/101) membuktikan RTP dasar Asterisk<->PJSIP sehat
    # ("channel originate ... Playback" terdengar normal), tapi kombinasi
    # bridge (simple_bridge) + UnicastRTP(slin16) + transcode ke PJSIP(ulaw)
    # membuat Asterisk tidak pernah mengirim RTP balik ke PJSIP ("rtp set
    # debug on" menunjukkan nol paket "Sent RTP" ke sisi telepon). ulaw =
    # native format PJSIP/101, jadi Asterisk tidak perlu transcode di bridge
    # sama sekali. Encode/decode ulaw<->PCM16 sekarang dilakukan di
    # rtp_bridge.py, transparan untuk kode di atasnya.
    #
    # Konsekuensi: sample_rate_hz jadi 8000 (standar ulaw/G.711), sedangkan
    # Gemini Live tetap butuh PCM16@16kHz untuk audio masuk (lihat
    # GeminiConfig.input_sample_rate_hz) -- makanya call_session.py sekarang
    # me-resample uplink audio caller dari 8kHz ke 16kHz sebelum dikirim ke
    # Gemini (resampler_out yang sudah ada dari 24kHz->rtp.sample_rate_hz
    # untuk arah sebaliknya otomatis ikut jadi 24kHz->8kHz, tidak perlu ubah
    # apa-apa di situ karena sudah parametrized dari config ini).
    codec: str = os.getenv("RTP_CODEC", "ulaw")
    sample_rate_hz: int = int(os.getenv("RTP_SAMPLE_RATE_HZ", "8000"))


@dataclass
class GeminiConfig:
    api_key: str = os.getenv("GEMINI_API_KEY", "")
    model: str = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.0-flash-live-001")
    language_code: str = os.getenv("GEMINI_LANGUAGE_CODE", "id-ID")
    voice_name: str = os.getenv("GEMINI_VOICE_NAME", "Puck")
    input_sample_rate_hz: int = 16000   # kontrak input Live API: PCM16 @16kHz mono
    output_sample_rate_hz: int = 24000  # kontrak output Live API: PCM16 @24kHz mono


@dataclass
class NineRouterConfig:
    """Integrasi 9Router (OpenAI-compatible gateway) sebagai pengganti Gemini Live.

    Project aslinya pakai Gemini Live (audio realtime bidireksional). 9Router
    TIDAK punya realtime (lihat core/ninerouter_voice.py) — jadi ini jalur
    pipeline STT -> LLM -> TTS terpisah.
    """
    base_url: str = os.getenv("NINEROUTER_BASE_URL", "http://studiouidesk.ddns.net:20128/v1")
    api_key: str = os.getenv("NINEROUTER_API_KEY", "")
    model_llm: str = os.getenv("NINEROUTER_MODEL_LLM", "kenari/gpt-5-5")
    model_stt: str = os.getenv("NINEROUTER_MODEL_STT", "kenari/whisper-large-v3-turbo")
    model_tts: str = os.getenv("NINEROUTER_MODEL_TTS", "kenari/kokoro-tts")
    tts_voice: str = os.getenv("NINEROUTER_TTS_VOICE", "af_heart")
    language: str = os.getenv("NINEROUTER_LANGUAGE", "id")
    # Kontrak audio: caller masuk PCM16 @ sample_rate_rtp (ulaw=8000),
    # STT butuh PCM16 @ input_sample_rate_hz (16000).
    sample_rate_rtp: int = int(os.getenv("NINEROUTER_RTP_RATE", "8000"))
    input_sample_rate_hz: int = 16000
    # VAD energi (tuning dibutuhkan di lingkungan produksi)
    vad_threshold: int = int(os.getenv("NINEROUTER_VAD_THRESHOLD", "500"))
    vad_end_silence_ms: int = int(os.getenv("NINEROUTER_VAD_END_SILENCE_MS", "650"))


@dataclass
class DatakelolaConfig:
    base_url: str = os.getenv("DATAKELOLA_BASE_URL", "")
    api_key: str = os.getenv("DATAKELOLA_API_KEY", "")
    enabled: bool = _get_bool("DATAKELOLA_ENABLED", False)


@dataclass
class CampaignDefaults:
    default_concurrent_limit: int = int(os.getenv("DEFAULT_CONCURRENT_LIMIT", "5"))
    default_max_retry: int = int(os.getenv("DEFAULT_MAX_RETRY", "2"))
    retryable_outcomes: tuple = ("NO_ANSWER", "BUSY", "TEMPORARY_FAILURE")
    timezone: str = os.getenv("CAMPAIGN_TIMEZONE", "Asia/Jakarta")


@dataclass
class InboundConfig:
    enabled: bool = _get_bool("INBOUND_ENABLED", True)
    # File JSON: nama perusahaan, sapaan, daftar layanan/FAQ (knowledge base), daftar queue.
    profile_path: str = os.getenv("INBOUND_PROFILE_PATH", "./data/inbound_profile.json")
    max_concurrent: int = int(os.getenv("INBOUND_MAX_CONCURRENT", "10"))
    # Tujuan ARI `continue` saat transfer ke queue. Extension = nama queue.
    transfer_context: str = os.getenv("INBOUND_TRANSFER_CONTEXT", "queue-router")
    # Setelah AI memanggil transfer/end, tunggu AI selesai bicara (turn_complete).
    # Kalau tidak selesai dalam N detik, paksa lanjut supaya caller tidak menggantung.
    handoff_watchdog_sec: float = float(os.getenv("INBOUND_HANDOFF_WATCHDOG_SEC", "6"))
    # Jeda kecil setelah audio terakhir dikirim sebelum transfer/hangup (jitter buffer telepon).
    handoff_tail_sec: float = float(os.getenv("INBOUND_HANDOFF_TAIL_SEC", "0.6"))


@dataclass
class AppConfig:
    db_path: str = os.getenv("DB_PATH", "./db/ai_outbound.sqlite3")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    scheduler_poll_interval_sec: int = int(os.getenv("SCHEDULER_POLL_INTERVAL_SEC", "5"))
    # Mesin suara: "gemini" (Gemini Live realtime, default) atau "ninerouter"
    # (pipeline STT->LLM->TTS lewat 9Router, lihat core/ninerouter_voice.py).
    voice_engine: str = os.getenv("VOICE_ENGINE", "gemini")
    ari: AriConfig = field(default_factory=AriConfig)
    rtp: RtpBridgeConfig = field(default_factory=RtpBridgeConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    ninerouter: NineRouterConfig = field(default_factory=NineRouterConfig)
    datakelola: DatakelolaConfig = field(default_factory=DatakelolaConfig)
    campaign: CampaignDefaults = field(default_factory=CampaignDefaults)
    inbound: InboundConfig = field(default_factory=InboundConfig)


settings = AppConfig()
