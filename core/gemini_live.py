"""
Wrapper sesi Gemini Live API.

Gemini Live API menangani STT dan TTS SEKALIGUS dalam satu sesi realtime
audio-in/audio-out (lihat PRD Arsitektur Sistem) — tidak ada panggilan
Deepgram/gTTS/ElevenLabs terpisah. Modul ini hanya:
  1. Mengirim chunk PCM16@16kHz dari caller ke Gemini (audio input).
  2. Menerima audio balasan PCM16@24kHz dari Gemini (audio output).
  3. Menerima & menjawab tool call (record_answer, request_human_agent,
     end_conversation) yang jadi lapisan Application Validation (PRD #17) —
     jawaban HANYA dianggap tercatat kalau lewat tool call ini, bukan dari
     asumsi terhadap teks respons Gemini.

Referensi API: `google-genai` SDK, `client.aio.live.connect(...)`.

CATATAN PENTING (gemini-3.1-flash-live-preview):
  Model 3.1 Flash Live MEMBATASI `send_client_content` hanya untuk
  seeding initial history (dan itu pun harus `history_config` diaktifkan
  dengan `initial_history_in_client_content=True`). Kalau dipakai untuk
  trigger turn biasa tanpa itu, server menutup koneksi dengan error
  close-code 1007 (invalid argument). Semua pengiriman teks/audio untuk
  memicu turn (termasuk sapaan pembuka) HARUS lewat `send_realtime_input`.
  Referensi: https://github.com/google/adk-python/issues/5018
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

from google import genai
from google.genai import types

from config import GeminiConfig
from core.topic_config import Topic, build_system_instruction

logger = logging.getLogger("gemini_live")


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="record_answer",
        description=(
            "Catat jawaban penelepon untuk satu pertanyaan campaign. WAJIB dipanggil "
            "setiap kali penelepon merespons sebuah pertanyaan, apa pun hasilnya."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "question_id": types.Schema(type=types.Type.STRING, description="ID pertanyaan, mis. 'Q1'"),
                "answer_text": types.Schema(type=types.Type.STRING, description="Ringkasan jawaban penelepon"),
                "status": types.Schema(
                    type=types.Type.STRING,
                    enum=["VALID", "INVALID", "UNCLEAR", "SKIPPED", "REFUSED"],
                    description="VALID jika jawaban relevan & jelas, UNCLEAR jika ambigu, "
                                "REFUSED jika penelepon menolak menjawab, SKIPPED jika dilewati.",
                ),
            },
            required=["question_id", "status"],
        ),
    ),
    types.FunctionDeclaration(
        name="request_human_agent",
        description="Panggil ini jika penelepon secara eksplisit minta bicara dengan manusia/agent.",
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
    types.FunctionDeclaration(
        name="end_conversation",
        description="Panggil ini ketika percakapan siap diakhiri (tujuan tercapai atau tidak bisa dilanjutkan).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "summary": types.Schema(type=types.Type.STRING, description="Ringkasan singkat hasil percakapan"),
            },
            required=["summary"],
        ),
    ),
]


@dataclass
class GeminiTurnEvent:
    audio_pcm: Optional[bytes] = None          # chunk audio 24kHz PCM16 dari Gemini
    transcript_text: Optional[str] = None       # transkrip teks (input atau output) jika tersedia
    transcript_speaker: Optional[str] = None    # "AI" atau "CALLER"
    tool_call: Optional[dict] = None            # {"name": ..., "args": {...}, "id": ...}
    turn_complete: bool = False


class GeminiLiveSession:
    def __init__(
        self,
        cfg: GeminiConfig,
        topic: Optional[Topic] = None,
        system_instruction: Optional[str] = None,
        tool_declarations: Optional[list] = None,
    ):
        # Mode outbound: cukup kirim `topic` (instruction & tool dari campaign).
        # Mode inbound: kirim `system_instruction` + `tool_declarations` sendiri.
        self.cfg = cfg
        self.topic = topic
        self._system_instruction = system_instruction
        self._tool_declarations = tool_declarations
        self._client = genai.Client(api_key=cfg.api_key)
        self._session_ctx = None
        self._session = None

    async def __aenter__(self):
        system_instruction = self._system_instruction or build_system_instruction(self.topic)
        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                role="system", parts=[types.Part(text=system_instruction)]
            ),
            speech_config=types.SpeechConfig(
                language_code=self.cfg.language_code,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.cfg.voice_name)
                ),
            ),
            tools=[types.Tool(function_declarations=self._tool_declarations or TOOL_DECLARATIONS)],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        self._session_ctx = self._client.aio.live.connect(model=self.cfg.model, config=live_config)
        self._session = await self._session_ctx.__aenter__()
        logger.info("Gemini Live session opened (model=%s, lang=%s)", self.cfg.model, self.cfg.language_code)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session_ctx:
            await self._session_ctx.__aexit__(exc_type, exc, tb)

    async def send_audio(self, pcm16_16k: bytes):
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16_16k, mime_type=f"audio/pcm;rate={self.cfg.input_sample_rate_hz}")
        )

    async def send_text(self, text: str):
        """Dipakai untuk kickoff pembukaan percakapan (mis. instruksi 'mulai sapa penelepon').

        PENTING: pakai send_realtime_input, BUKAN send_client_content.
        Untuk gemini-3.1-flash-live-preview, send_client_content hanya
        diperbolehkan untuk seeding initial history (butuh history_config
        initial_history_in_client_content=True) dan tidak memicu turn model.
        Kalau dipakai untuk trigger turn biasa tanpa itu, server menutup
        koneksi dengan close-code 1007 (invalid argument) -- inilah yang
        menyebabkan sesi mati beberapa detik setelah dibuka.
        """
        await self._session.send_realtime_input(text=text)

    async def send_tool_response(self, call_id: str, name: str, result: dict):
        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(id=call_id, name=name, response=result)
            ]
        )

    async def events(self) -> AsyncIterator[GeminiTurnEvent]:
        """Iterasi event dari Gemini: audio chunk, transkrip, tool call, turn_complete.

        PENTING: `self._session.receive()` dari SDK google-genai adalah generator
        PER-TURN, bukan generator seumur hidup sesi -- generator ini otomatis habis
        (StopAsyncIteration) begitu satu turn selesai (turn_complete=True diterima
        dari server). Kalau cuma dipanggil sekali di luar loop, `events()` ikut
        habis setelah turn pertama, `async for` di pemanggil (call_session.py)
        ikut berhenti, `async with GeminiLiveSession(...)` exit, dan channel
        langsung di-hangup -- padahal percakapan seharusnya masih lanjut menunggu
        giliran bicara caller berikutnya.

        Makanya di sini `receive()` dipanggil ulang tiap kali generator turn
        sebelumnya habis (loop luar `while True`), supaya sesi terus mendengarkan
        turn demi turn selama koneksi WebSocket ke Gemini masih hidup. Kondisi
        berhenti percakapan tetap sepenuhnya dikontrol oleh pemanggil lewat
        `state.end_requested` / `_telephony_connected` di call_session.py -- loop
        di sini cuma berhenti kalau `self._session.receive()` benar-benar raise
        exception (koneksi Gemini putus/error), yang lalu naik ke `except
        Exception` di `_run_conversation()` dan ditangani sebagai outcome FAILED.
        """
        while True:
            async for message in self._session.receive():
                server_content = getattr(message, "server_content", None)
                tool_call = getattr(message, "tool_call", None)

                if tool_call and tool_call.function_calls:
                    for fc in tool_call.function_calls:
                        yield GeminiTurnEvent(
                            tool_call={"id": fc.id, "name": fc.name, "args": dict(fc.args or {})}
                        )
                    continue

                if server_content is None:
                    continue

                if getattr(server_content, "input_transcription", None):
                    yield GeminiTurnEvent(
                        transcript_text=server_content.input_transcription.text,
                        transcript_speaker="CALLER",
                    )
                if getattr(server_content, "output_transcription", None):
                    yield GeminiTurnEvent(
                        transcript_text=server_content.output_transcription.text,
                        transcript_speaker="AI",
                    )

                model_turn = getattr(server_content, "model_turn", None)
                if model_turn:
                    for part in model_turn.parts:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and inline_data.data:
                            audio = inline_data.data

                            logger.info(
                                "GEMINI AUDIO RAW: len=%d first32=%s",
                                len(audio),
                                audio[:32].hex(" ")
                            )

                            yield GeminiTurnEvent(audio_pcm=audio)

                if getattr(server_content, "turn_complete", False):
                    yield GeminiTurnEvent(turn_complete=True)
            # `receive()` habis di sini artinya satu turn selesai (bukan sesi
            # berakhir) -- lanjut ke iterasi `while True` berikutnya untuk
            # memanggil `receive()` lagi dan menunggu turn berikutnya.
