"""
STT + TTS via Gemini (google-genai) sebagai backend audio untuk pipeline 9Router.

Dipakai ketika `NINEROUTER_STT_BACKEND=gemini` / `NINEROUTER_TTS_BACKEND=gemini`
(karena route `/audio/*` di 9Router belum aktif). LLM tetap lewat 9Router; modul
ini hanya menggantikan kaki STT & TTS.

Model yang terbukti jalan (diverifikasi live):
    - STT: `gemini-3.5-transcribe`  (transkrip ada di part.audio_transcription.text)
    - TTS: `gemini-3.8-flash-tts`   (generate_content response_modalities=["AUDIO"])

Kontrak audio:
    - transcribe(pcm16_16k) -> str  (input PCM16 @16kHz mono)
    - synthesize(text) -> bytes     (output PCM16 @ rtp_rate, siap send_pcm)
"""
import logging

from google import genai
from google.genai import types

from config import GeminiConfig
from core.audio_utils import Resampler, pcm16_to_wav, wav_bytes_to_pcm16

logger = logging.getLogger("gemini_audio")


class GeminiAudio:
    def __init__(self, cfg: GeminiConfig, rtp_rate: int = 8000):
        self.cfg = cfg
        self._rtp_rate = rtp_rate
        self._client = genai.Client(api_key=cfg.api_key)

    async def transcribe(self, pcm16_16k: bytes) -> str:
        """STT: PCM16@16kHz -> teks transkrip ('' bila tak ada suara)."""
        wav = pcm16_to_wav(pcm16_16k, 16000)
        content = types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=wav, mime_type="audio/wav")],
        )
        resp = await self._client.aio.models.generate_content(
            model=self.cfg.stt_model, contents=[content]
        )
        # gemini-3.5-transcribe menaruh transkrip di part.audio_transcription,
        # bukan di resp.text (resp.text bisa None + warning).
        if resp.candidates:
            for part in resp.candidates[0].content.parts:
                at = getattr(part, "audio_transcription", None)
                if at and getattr(at, "text", None):
                    return at.text.strip()
        return (resp.text or "").strip()

    async def synthesize(self, text: str) -> bytes:
        """TTS: teks -> PCM16 @ rtp_rate (siap dikirim ke Asterisk)."""
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                language_code=self.cfg.language_code,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.cfg.voice_name)
                ),
            ),
        )
        resp = await self._client.aio.models.generate_content(
            model=self.cfg.tts_model, contents=text, config=cfg
        )

        audio = None
        if resp.candidates:
            for part in resp.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    audio = part.inline_data.data
                    break
        if audio is None:
            logger.warning("TTS tidak menghasilkan audio untuk teks: %r", text)
            return b""

        pcm, rate = wav_bytes_to_pcm16(audio)
        if rate != self._rtp_rate:
            pcm = Resampler(rate, self._rtp_rate).process(pcm)
        return pcm
