"""
Entry point AI Outbound Calling & Voice Blasting + Inbound receptionist (ext 800).

Jalankan:  python main.py
Prasyarat: .env sudah diisi (copy dari .env.example), Asterisk ARI user sudah
dibuat, dan minimal satu campaign+topic+question sudah di-seed ke DB
(lihat scripts/seed_example.py).
"""
import asyncio
import logging

from config import settings
from core.ari_client import AriClient
from core.campaign_manager import CampaignManager
from core.inbound_session import InboundManager
from core.topic_config import inbound_receptionist_topic
from db.database import Database


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def main():
    setup_logging()
    logger = logging.getLogger("main")

    db = Database(settings.db_path)
    ari = AriClient(settings.ari)
    await ari.connect()

    manager = CampaignManager(ari, db)
    InboundManager(
        ari, db,
        port_pool=manager.port_pool,          # pool RTP dibagi dengan campaign
        topic_factory=inbound_receptionist_topic,
    )

    logger.info("AI Voice Server siap. Outbound campaign + inbound (ext 800) aktif.")
    try:
        await manager.start()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        await ari.close()


if __name__ == "__main__":
    asyncio.run(main())
