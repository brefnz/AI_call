"""Smoke-test pipeline 9Router (STT/LLM/TTS) tanpa Asterisk.

Jalan:
    NINEROUTER_API_KEY=sk-xxx python scripts/smoke_ninerouter.py

Mengetes 3 endpoint yang dipakai core/ninerouter_voice.py:
    - chat()      -> POST /v1/chat/completions (LLM text)
    - synthesize()-> POST /v1/audio/speech (TTS -> WAV)
    - transcribe()-> POST /v1/audio/transcriptions (STT)
"""
import asyncio
import os

os.environ.setdefault("NINEROUTER_API_KEY", os.getenv("NINEROUTER_API_KEY", ""))

from config import settings
from core.ninerouter_voice import NineRouterVoice, pcm16_to_wav


async def main():
    voice = NineRouterVoice(settings.ninerouter)

    print("== 1) LLM chat ==")
    try:
        voice._messages = [{"role": "user", "content": "Sebutkan angka 1 sampai 3."}]
        resp = await voice.chat()
        print("OK:", resp)
    except Exception as e:
        print("ERR:", repr(e))

    print("\n== 2) TTS synthesize ==")
    try:
        pcm = await voice.synthesize("Halo, ini tes suara.")
        print(f"OK: {len(pcm)} bytes PCM16 @ {settings.ninerouter.sample_rate_rtp} Hz")
    except Exception as e:
        print("ERR:", repr(e))

    print("\n== 3) STT transcribe ==")
    try:
        # 0.5 detik silence PCM16@16k (harusnya hasil transkrip kosong)
        silence = b"\x00" * (16000 * 2 * 1)
        text = await voice.transcribe(silence)
        print(f"OK: transkrip={text!r}")
    except Exception as e:
        print("ERR:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
