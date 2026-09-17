"""
Tes telephony MURNI: originate outbound call via ARI, answer, mainkan sound
bawaan Asterisk, lalu hangup. TIDAK menyentuh Gemini/RTP sama sekali — tujuannya
cuma memastikan ARI_URL/USER/PASS/APP dan ARI_OUTBOUND_ENDPOINT_TEMPLATE benar,
serta channel benar-benar masuk ke Stasis app saat dijawab.

Jalankan:
    python scripts/test_call_ari_only.py 08123456789

Yang perlu dicek SUKSES:
  1. Log "Originate terkirim, channel id=..."
  2. Log "StasisStart diterima" (berarti channel masuk ke app ARI kita)
  3. Terdengar suara "demo-congrats" di telepon tujuan
  4. Log "Hangup terkirim" lalu proses keluar bersih (exit code 0)

Kalau macet di step 1: cek ARI_URL/USER/PASS (test manual: curl -u user:pass
http://ARI_HOST:8088/ari/asterisk/info).
Kalau macet di step 2 (originate sukses tapi StasisStart gak pernah muncul):
biasanya endpoint/trunk salah format, atau app name di ARI_APP beda dengan
yang di-set di dialplan/originate. Cek `asterisk -rvvv` -> `core show channels`
saat originate dikirim untuk lihat channel-nya nyangkut di mana.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.ari_client import AriClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_call_ari_only")


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_call_ari_only.py <nomor_tujuan>")
        sys.exit(1)
    phone_number = sys.argv[1]

    ari = AriClient(settings.ari)
    stasis_event = asyncio.Event()
    ended_event = asyncio.Event()
    state = {"channel_id": None}

    async def on_stasis_start(event: dict):
        if event.get("channel", {}).get("id") == state["channel_id"]:
            logger.info("StasisStart diterima untuk channel %s", state["channel_id"])
            stasis_event.set()

    async def on_channel_destroyed(event: dict):
        if event.get("channel", {}).get("id") == state["channel_id"]:
            cause = event.get("cause")
            cause_txt = event.get("cause_txt")
            logger.info("ChannelDestroyed: cause=%s (%s)", cause, cause_txt)
            ended_event.set()

    ari.on_event("StasisStart", on_stasis_start)
    ari.on_event("ChannelDestroyed", on_channel_destroyed)

    logger.info("Menghubungkan ke ARI %s (app=%s)...", settings.ari.base_url, settings.ari.app)
    await ari.connect()
    await asyncio.sleep(1)  # beri waktu websocket event-stream connect dulu

    endpoint = settings.ari.outbound_endpoint_template.format(number=phone_number)
    logger.info("Originate ke endpoint: %s", endpoint)
    try:
        channel = await ari.originate(
            endpoint=endpoint,
            caller_id=settings.ari.caller_id,
            timeout_sec=settings.ari.originate_timeout_sec,
        )
    except Exception:
        logger.exception("Originate GAGAL — cek ARI_URL/USER/PASS dan endpoint template di .env")
        await ari.close()
        sys.exit(1)

    state["channel_id"] = channel["id"]
    logger.info("Originate terkirim, channel id=%s", channel["id"])

    done, _ = await asyncio.wait(
        [asyncio.create_task(stasis_event.wait()), asyncio.create_task(ended_event.wait())],
        timeout=settings.ari.originate_timeout_sec + 10,
        return_when=asyncio.FIRST_COMPLETED,
    )

    if ended_event.is_set():
        logger.warning("Channel berakhir sebelum StasisStart (no answer/busy/rejected/failed).")
        await ari.close()
        return

    if not stasis_event.is_set():
        logger.warning("Timeout menunggu StasisStart — channel mungkin nyangkut di dialplan biasa, "
                        "bukan masuk ke Stasis app. Cek ARI_APP dan cara originate di trunk.")
        await ari.close()
        return

    try:
        logger.info("Menjawab channel...")
        await ari.answer_channel(channel["id"])

        logger.info("Memutar sound tes (sound:demo-congrats)...")
        await ari.play_sound(channel["id"], "sound:demo-congrats")

        await asyncio.sleep(8)  # kasih waktu sound selesai diputar

        logger.info("Hangup...")
        await ari.hangup_channel(channel["id"])
        logger.info("Tes selesai — kalau kamu dengar suara tadi, telephony path OK.")
    except Exception:
        logger.exception("Error setelah channel connected")
    finally:
        await ari.close()


if __name__ == "__main__":
    asyncio.run(main())
