"""
Pengganti `core/gemini_live.py` untuk integrasi 9Router (OpenAI-compatible gateway).

MENGAPA modul ini ada
----------------------
Project aslinya memakai Gemini Live API: satu sesi WebSocket audio-in/out
realtime (STT+TTS sekaligus) lewat `client.aio.live.connect()`. 9Router TIDAK
menyediakan padanan realtime-nya (hasil verifikasi):

    GET /v1/models                  -> OK (LLM text + STT + TTS)
    POST /v1/chat/completions       -> OK (dengan tools)
    POST /v1/audio/transcriptions   -> STT (kenari/whisper-large-v3-turbo)
    POST /v1/audio/speech           -> TTS (kokoro-tts / minimax-speech / mimo-tts)
    GET /v1/realtime                -> 404 (TIDAK ADA OpenAI Realtime)
    passthrough Gemini Live         -> TIDAK ADA

Jadi ini jalur pipeline TURN-BASED: STT -> LLM (tool calling) -> TTS, dengan
VAD energi untuk deteksi akhir bicara caller + barge-in. Ini perubahan
paradigma (bukan sekadar ganti endpoint): turn-taking tidak lagi dikelola
Gemini, tapi oleh aplikasi.

KONTRAK AUDIO (sama seperti sebelumnya, lihat config.py)
--------------------------------------------------------
- Caller -> AI : PCM16 mono @ sample_rate_rtp (ulaw = 8000 Hz), dari
  `RtpEndpoint.pcm_in_queue`. Diresample ke 16000 Hz untuk STT.
- AI -> caller : PCM16 mono @ sample_rate_rtp (8000 Hz), dikirim lewat
  `RtpEndpoint.send_pcm()`. Output TTS didecode dari WAV lalu diresample.

SEAM / DIFF di `core/call_session.py` (SUDAH diimplementasikan)
---------------------------------------------------------------
Agar tidak menyentuh jalur Gemini yang sudah jalan, integrasi dilakukan lewat
flag `VOICE_ENGINE=ninerouter` di `.env` (config.py: `settings.voice_engine`):

    async def _run_conversation(self) -> str:
        if settings.voice_engine == "ninerouter":
            return await self._run_conversation_pipeline()   # jalur baru
        ...  # loop Gemini Live yang lama tetap utuh

`_run_conversation_pipeline()` (core/call_session.py) melakukan setup telephony
(answer/bridge/externalMedia/RTP) yang identik dengan jalur Gemini, lalu:

    from core.ninerouter_voice import NineRouterVoice
    voice = NineRouterVoice(settings.ninerouter, self.topic)
    stop_event = asyncio.Event()   # set oleh watcher saat ChannelDestroyed

    await voice.run_conversation(
        audio_queue=self.rtp.pcm_in_queue,     # PCM16 @8k dari caller
        send_pcm=self.rtp.send_pcm,            # kirim PCM16 @8k ke Asterisk
        on_tool_call=self._execute_tool_call,  # (name, args) -> dict
        on_transcript=on_transcript,           # closure -> self.db.add_transcript
        kickoff_text=self._kickoff_text(),
        stop_event=stop_event,
        end_predicate=self._pipeline_end_predicate,
    )

Tool call dari chat completions berbentuk OpenAI tool_calls (bukan Gemini
FunctionDeclaration). Hasil tool di-append sebagai pesan `role=tool`, jadi
`_handle_tool_call` yang lama dipisah menjadi `_execute_tool_call(name, args)`
(mutasi state/DB murni, return dict) + wrapper `_handle_tool_call` yang tinggal
memanggil `gemini.send_tool_response`. Jalur pipeline memakai
`_execute_tool_call` langsung, jalur Gemini tetap lewat `_handle_tool_call`.

BLOKER YANG DIKETAHUI (bukan bug kode ini)
------------------------------------------
1. Saldo 9Router saat dicek = Rp 0 -> semua inference 402. Top-up dulu.
2. POST /v1/audio/speech untuk model TTS masih 400 ("provider does not support
   TTS via this route") -> konfirmasi route/model TTS yang aktif di 9Router.
3. Format output TTS diasumsikan WAV. Kalau router cuma keluarkan mp3/opus,
   butuh decoder tambahan (pydub/ffmpeg/soundfile) — stdlib `wave` hanya
   untuk WAV.
4. VAD energi sangat sensitif ke noise telepon; threshold (`NINEROUTER_VAD_*`)
   WAJIB di-tuning terhadap trunk yang sebenarnya (cek RMS saat silence vs
   bicara di log).
"""
import asyncio
import io
import json
import logging
import wave
from typing import Callable, Optional

import aiohttp

try:
    import audioop
except ImportError:  # Python 3.13+
    import audioop_lts as audioop  # type: ignore

from config import NineRouterConfig
from core.audio_utils import Resampler
from core.topic_config import Topic, build_system_instruction

logger = logging.getLogger("ninerouter_voice")


# --------------------------------------------------------------------------
# Tool declarations dalam format OpenAI (chat completions `tools`).
# Deskripsi disalin dari TOOL_DECLARATIONS di gemini_live.py.
# --------------------------------------------------------------------------
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "record_answer",
            "description": (
                "Catat jawaban penelepon untuk satu pertanyaan campaign. WAJIB dipanggil "
                "setiap kali penelepon merespons sebuah pertanyaan, apa pun hasilnya."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "string", "description": "ID pertanyaan, mis. 'Q1'"},
                    "answer_text": {"type": "string", "description": "Ringkasan jawaban penelepon"},
                    "status": {
                        "type": "string",
                        "enum": ["VALID", "INVALID", "UNCLEAR", "SKIPPED", "REFUSED"],
                    },
                },
                "required": ["question_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_agent",
            "description": "Panggil jika penelepon minta bicara dengan manusia/agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "queue": {
                        "type": "string",
                        "enum": ["support", "sales", "campaign"],
                        "description": "Queue tujuan (hanya dipakai mode inbound).",
                    },
                    "summary": {"type": "string", "description": "Ringkasan keperluan penelepon."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "Panggil ketika percakapan siap diakhiri.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Ringkasan singkat hasil percakapan"},
                },
                "required": ["summary"],
            },
        },
    },
]


# --------------------------------------------------------------------------
# Helpers audio (stdlib wave, no dependency tambahan)
# --------------------------------------------------------------------------
def pcm16_to_wav(pcm: bytes, rate: int) -> bytes:
    """Bungkus PCM16LE mono jadi WAV (untuk upload ke /audio/transcriptions)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_bytes_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Decode WAV -> (PCM16LE mono, sample_rate). Naikkan 8-bit & stereo -> mono."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    pcm = raw
    if width == 1:                      # 8-bit unsigned -> 16-bit signed
        pcm = audioop.bias(pcm, 1, -128)
        pcm = audioop.lin2lin(pcm, 1, 2)
    elif width != 2:
        raise ValueError(f"sample width {width} byte belum didukung")
    if nch == 2:                        # stereo -> mono
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif nch != 1:
        raise ValueError(f"channel {nch} belum didukung")
    return pcm, rate


# --------------------------------------------------------------------------
# VAD energi sederhana (bukan ML). Tuning wajib terhadap trunk produksi.
# --------------------------------------------------------------------------
class UtteranceDetector:
    """Deteksi ujaran caller dari stream PCM16 (16000 Hz) memakai RMS frame."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        threshold: int = 500,
        min_speech_ms: int = 300,
        end_silence_ms: int = 650,
    ):
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2  # 2 byte/sample
        self.threshold = threshold
        self.min_speech_frames = max(1, int(min_speech_ms / frame_ms))
        self.end_silence_frames = max(1, int(end_silence_ms / frame_ms))

        self._all = bytearray()      # seluruh audio masuk (untuk take())
        self._vad_buf = bytearray()  # buffer frame utuh untuk VAD
        self._offset = 0             # posisi _all yang sudah di-take
        self._speaking = False
        self._speech_frames = 0
        self._silence_frames = 0

    @property
    def speaking(self) -> bool:
        """True kalau sedang/baru terdeteksi suara (dipakai barge-in)."""
        return self._speaking

    def feed(self, pcm: bytes) -> bool:
        """Push PCM16. Return True bila satu ujaran penuh selesai terdeteksi."""
        self._all += pcm
        self._vad_buf += pcm
        while len(self._vad_buf) >= self.frame_bytes:
            frame = bytes(self._vad_buf[: self.frame_bytes])
            del self._vad_buf[: self.frame_bytes]
            rms = audioop.rms(frame, 2)
            if rms >= self.threshold:
                self._speaking = True
                self._speech_frames += 1
                self._silence_frames = 0
            elif self._speaking:
                self._silence_frames += 1
                if (
                    self._silence_frames >= self.end_silence_frames
                    and self._speech_frames >= self.min_speech_frames
                ):
                    self._reset_vad()
                    return True
        return False

    def take(self) -> bytes:
        """Ambil seluruh audio sejak take() terakhir, lalu reset VAD."""
        out = bytes(self._all[self._offset :])
        self._offset = len(self._all)
        self._reset_vad()
        return out

    def _reset_vad(self):
        self._speaking = False
        self._speech_frames = 0
        self._silence_frames = 0


# --------------------------------------------------------------------------
# Sesi pipeline STT -> LLM -> TTS
# --------------------------------------------------------------------------
class NineRouterVoice:
    def __init__(
        self,
        cfg: NineRouterConfig,
        topic: Optional[Topic] = None,
        system_instruction: Optional[str] = None,
    ):
        self.cfg = cfg
        self.topic = topic
        self._system_instruction = system_instruction
        if not self._system_instruction and topic:
            self._system_instruction = build_system_instruction(topic)

        # caller: 8k (ulaw) -> 16k (STT). TTS: rate dari WAV -> 8k (dibuat on-the-fly).
        self._resampler_in = Resampler(cfg.sample_rate_rtp, cfg.input_sample_rate_hz)

        self._messages: list[dict] = []
        if self._system_instruction:
            self._messages.append({"role": "system", "content": self._system_instruction})

    # ---- HTTP helpers ----
    def _headers(self, json_body: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self.cfg.api_key}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def _post_json(self, path: str, payload: dict) -> dict:
        url = f"{self.cfg.base_url}{path}"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=self._headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"{path} -> HTTP {resp.status}: {body[:400]}")
                return await resp.json()

    async def transcribe(self, pcm16_16k: bytes) -> str:
        """STT: /v1/audio/transcriptions (multipart). Return teks (bisa '')."""
        wav_bytes = pcm16_to_wav(pcm16_16k, self.cfg.input_sample_rate_hz)
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", self.cfg.model_stt)
        form.add_field("language", self.cfg.language)
        form.add_field("response_format", "json")

        url = f"{self.cfg.base_url}/audio/transcriptions"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=form, headers=self._headers(json_body=False)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"STT -> HTTP {resp.status}: {body[:400]}")
                data = await resp.json()
        return (data.get("text") or "").strip()

    async def chat(self) -> dict:
        """LLM: /v1/chat/completions. Return {"content", "tool_calls"}."""
        data = await self._post_json(
            "/chat/completions",
            {
                "model": self.cfg.model_llm,
                "messages": self._messages,
                "tools": OPENAI_TOOLS,
                "tool_choice": "auto",
            },
        )
        msg = data["choices"][0]["message"]
        return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls")}

    async def synthesize(self, text: str) -> bytes:
        """TTS: /v1/audio/speech. Return PCM16 @ sample_rate_rtp (siap send_pcm)."""
        url = f"{self.cfg.base_url}/audio/speech"
        payload = {
            "model": self.cfg.model_tts,
            "input": text,
            "voice": self.cfg.tts_voice,
            "response_format": "wav",
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=self._headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"TTS -> HTTP {resp.status}: {body[:400]}")
                raw = await resp.read()

        pcm, rate = wav_bytes_to_pcm16(raw)
        if rate != self.cfg.sample_rate_rtp:
            pcm = Resampler(rate, self.cfg.sample_rate_rtp).process(pcm)
        return pcm

    # ---- audio in/out ----
    async def _drain_bargein(self, detector: UtteranceDetector, audio_queue: asyncio.Queue) -> bool:
        """Kosongkan queue non-blocking sambil feed VAD. True = caller mulai bicara."""
        while True:
            try:
                pcm8k = audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return False
            detector.feed(self._resampler_in.process(pcm8k))
            if detector.speaking:
                return True

    async def _play(
        self,
        pcm8k: bytes,
        detector: UtteranceDetector,
        audio_queue: asyncio.Queue,
        stop_event: asyncio.Event,
        send_pcm: Callable,
    ) -> bool:
        """Kirim audio AI per ~20ms sambil cek barge-in. True = caller menyela."""
        chunk = int(self.cfg.sample_rate_rtp * 0.02) * 2  # 20ms PCM16
        for i in range(0, len(pcm8k), chunk):
            if stop_event.is_set():
                return False
            if await self._drain_bargein(detector, audio_queue):
                logger.info("Barge-in: caller menyela, hentikan playback")
                return True
            await send_pcm(pcm8k[i : i + chunk])
            await asyncio.sleep(0.02)
        return False

    async def _listen(
        self,
        detector: UtteranceDetector,
        audio_queue: asyncio.Queue,
        stop_event: asyncio.Event,
        idle_timeout: float = 15.0,
    ) -> Optional[bytes]:
        """Tunggu ujaran caller (VAD). Return PCM16@16k utuh, atau None bila hangup/timeout."""
        try:
            while not stop_event.is_set():
                pcm8k = await asyncio.wait_for(audio_queue.get(), timeout=idle_timeout)
                pcm16k = self._resampler_in.process(pcm8k)
                if detector.feed(pcm16k):
                    return detector.take()
        except asyncio.TimeoutError:
            return None
        return None

    # ---- loop utama ----
    async def run_conversation(
        self,
        audio_queue: asyncio.Queue,
        send_pcm: Callable,
        on_tool_call: Callable,
        on_transcript: Callable,
        kickoff_text: str,
        stop_event: asyncio.Event,
        end_predicate: Callable,
    ):
        """Orkestrasi percakapan end-to-end (turn-based).

        on_tool_call(name: str, args: dict) -> dict : eksekusi record_answer /
            request_human_agent / end_conversation, return hasil (dict).
        on_transcript(speaker: str, text: str) -> None : simpan transcript.
        """
        detector = UtteranceDetector(
            sample_rate=self.cfg.input_sample_rate_hz,
            threshold=self.cfg.vad_threshold,
            end_silence_ms=self.cfg.vad_end_silence_ms,
        )

        # 1) Sapaan pembuka: LLM dipicu dengan instruksi kickoff.
        self._messages.append({"role": "user", "content": kickoff_text})
        try:
            greeting = await self._generate_until_speech(
                detector, audio_queue, stop_event, send_pcm, on_tool_call, on_transcript
            )
        except RuntimeError as e:
            logger.exception("Gagal pada sapaan pembuka: %s", e)
            return

        # 2) Loop dengar -> STT -> LLM -> TTS.
        while not stop_event.is_set() and not end_predicate():
            pcm16k = await self._listen(detector, audio_queue, stop_event)
            if stop_event.is_set():
                break
            if not pcm16k:
                continue  # timeout idle -> dengar lagi (bisa dibatasi max idle di sini)

            text = await self.transcribe(pcm16k)
            if text:
                await on_transcript("CALLER", text)
                self._messages.append({"role": "user", "content": text})

            try:
                await self._generate_until_speech(
                    detector, audio_queue, stop_event, send_pcm, on_tool_call, on_transcript
                )
            except RuntimeError as e:
                logger.exception("Gagal generate respons: %s", e)
                break

    async def _generate_until_speech(
        self,
        detector, audio_queue, stop_event, send_pcm, on_tool_call, on_transcript,
    ):
        """Jalankan chat (+tool loop) sampai LLM menghasilkan teks yang di-TTS-kan."""
        while not stop_event.is_set():
            resp = await self.chat()

            if resp.get("tool_calls"):
                self._messages.append(
                    {"role": "assistant", "content": None, "tool_calls": resp["tool_calls"]}
                )
                for tc in resp["tool_calls"]:
                    fn = tc["function"]
                    name = fn["name"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await on_tool_call(name, args)
                    self._messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)}
                    )
                continue  # minta LLM lanjut setelah hasil tool

            if resp.get("content"):
                content = resp["content"].strip()
                self._messages.append({"role": "assistant", "content": content})
                await on_transcript("AI", content)
                pcm8k = await self.synthesize(content)
                await self._play(pcm8k, detector, audio_queue, stop_event, send_pcm)
            return
