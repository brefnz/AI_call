"""
InboundManager: mendaftarkan handler ARI SEKALI (bukan per sesi), lalu membuat
InboundSession untuk tiap channel yang masuk lewat dialplan:

    exten => 700,1,Stasis(ai_voice_app,inbound)

Hanya StasisStart dengan args[0] == "inbound" yang diproses. StasisStart untuk
channel outbound dan channel externalMedia tidak punya args itu, jadi diabaikan.
"""
import asyncio
import logging

from config import settings
from core.ari_client import AriClient
from core.inbound_config import InboundProfile
from core.inbound_session import InboundSession
from core.rtp_bridge import RtpPortPool
from db.database import Database

logger = logging.getLogger("inbound_manager")


class InboundManager:
    def __init__(self, ari: AriClient, db: Database, port_pool: RtpPortPool, profile: InboundProfile):
        self.ari = ari
        self.db = db
        self.port_pool = port_pool
        self.profile = profile
        self.sessions: dict[str, InboundSession] = {}    # channel_id -> sesi aktif
        self._tasks: set[asyncio.Task] = set()

    def start(self):
        self.ari.on_event("StasisStart", self._on_stasis_start)
        self.ari.on_event("ChannelDestroyed", self._on_channel_destroyed)
        logger.info(
            "Inbound aktif: %d layanan, queue=%s, max_concurrent=%d",
            len(self.profile.services), self.profile.queue_names, settings.inbound.max_concurrent,
        )

    async def _on_stasis_start(self, event: dict):
        args = event.get("args") or []
        if not args or args[0] != "inbound":
            return
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return

        if len(self.sessions) >= settings.inbound.max_concurrent:
            logger.warning("Inbound ditolak (penuh): channel=%s", channel_id)
            try:
                await self.ari.hangup_channel(channel_id, reason="busy")
            except Exception:
                logger.exception("Gagal menolak channel inbound %s", channel_id)
            return

        session = InboundSession(self.ari, self.db, self.port_pool, channel, self.profile, args)
        self.sessions[channel_id] = session
        task = asyncio.create_task(self._run(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "Inbound baru: channel=%s caller=%s fallback=%s session=%s",
            channel_id, session.caller_number, session.is_fallback, session.session_id,
        )

    def _on_channel_destroyed(self, event: dict):
        session = self.sessions.get((event.get("channel") or {}).get("id"))
        if session:
            session._on_channel_destroyed(event)

    async def _run(self, session: InboundSession):
        try:
            result = await session.run()
            logger.info("Inbound selesai: %s", result)
        except Exception:
            logger.exception("Sesi inbound crash: %s", session.session_id)
        finally:
            # Jangan hapus kalau channel yang sama sudah punya sesi baru (kasus fallback dari queue).
            if self.sessions.get(session.channel_id) is session:
                del self.sessions[session.channel_id]
