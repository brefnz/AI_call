"""Round-trip smoke test hybrid: LLM 9Router + STT/TTS Gemini.

Jalan:
    NINEROUTER_API_KEY=... GEMINI_API_KEY=... python scripts/smoke_hybrid.py
"""
import asyncio
import os

from config import settings
from core.ninerouter_voice import NineRouterVoice


async def main():
    voice = NineRouterVoice(settings.ninerouter)

    # 1) TTS via backend (default gemini)
    print("== TTS synthesize ==")
    pcm = await voice.synthesize("Halo, ini tes suara.")
    print(f"OK: {len(pcm)} bytes PCM16 @ {settings.ninerouter.sample_rate_rtp} Hz")

    # 2) STT: TTS output -> transcribe (harus non-empty)
    print("\n== STT transcribe (dari output TTS) ==")
    # naikkan 8k -> 16k untuk STT (sederhana: linear upsample 2x)
    import audioop
    pcm16k = audioop.ratecv(pcm, 2, 1, settings.ninerouter.sample_rate_rtp, 16000, None)[0]
    text = await voice.transcribe(pcm16k)
    print(f"OK transkrip: {text!r}")

    # 3) LLM via 9Router
    print("\n== LLM chat (9Router) ==")
    voice._messages = [{"role": "user", "content": "Sebutkan angka 1 sampai 3."}]
    resp = await voice.chat()
    print("OK:", resp)


if __name__ == "__main__":
    asyncio.run(main())
