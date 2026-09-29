"""
Panggilan INBOUND: ext 800 -> Stasis(ai_voice_app,inbound).

Memakai ulang CallSession (answer -> bridge -> externalMedia -> Gemini Live).
Bedanya dengan outbound:
  - tidak ada originate, channel sudah ada dari StasisStart
  - tool `request_human_agent` = transfer: set AI_SUMMARY lalu continue ke
    dialplan [queue-router] dengan ekstensi = nama queue
  - kalau queue gagal/timeout, dialplan memanggil
    Stasis(ai_voice_app,inbound,fallback,<queue>) -> ditangani _handle_fallback

Simpan sebagai core/inbound_session.py
"""
import asyncio
import inspect
import logging
import re
from datetime import datetime

from core.call_session import CallSession
from core.conversation_state import CallState
from db.database import now_iso

logger = logging.getLogger("inbound")

ALLOWED_QUEUES = {"support", "campaign", "sales"}
DEFAULT_QUEUE = "support"
TRANSFER_MAX_WAIT_SEC = 15  # batas tunggu AI selesai bicara setelah request_human_agent


def _clean(text: str, limit: int = 200) -> str:
    """Whitelist karakter supaya aman dipakai di variabel dialplan."""
    text = re.sub(r"[^\w\s.\-]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


class InboundSession(CallSession):
    def __init__(self, ari, db, port_pool, channel_id: str, caller_number: str, topic):
        super().__init__(
            ari, db, port_pool,
            target={"id": None, "phone_number": caller_number},
            topic=topic,
        )
        self.channel_id = channel_id
        self.caller_number = caller_number
        self._transfer_queue: str | None = None
        self._transfer_at: float | None = None
        self._summary = ""

    # ---------------- entrypoint ----------------

    async def run(self) -> dict:
        self.started_at = now_iso()
        self.connected_at = self.started_at
        self._telephony_connected = True

        try:
            await self.db.execute(
                """INSERT INTO call_session (id, campaign_target_id, started_at, connected_at, status)
                   VALUES (?,?,?,?,?)""",
                (self.session_id, None, self.started_at, self.connected_at, CallState.CONNECTED.value),
            )
        except Exception:
            # Kemungkinan campaign_target_id NOT NULL di schema -> lihat catatan
            logger.exception("Gagal INSERT call_session inbound, session=%s", self.session_id)
        await self.db.log_event(
            "CALL_CONNECTED", call_session_id=self.session_id, ari_call_id=self.channel_id
        )

        try:
            outcome = await self._run_conversation()
        except Exception:
            logger.exception("Error selama percakapan inbound, session=%s", self.session_id)
            outcome = "FAILED"

        transferred = bool(self._transfer_queue) and self._telephony_connected
        if transferred:
            await self._cleanup_telephony(hangup_main=False)
            await self._transfer()
        else:
            await self._cleanup_telephony()

        ok = outcome == "SUCCESS" or transferred
        status = CallState.COMPLETED.value if ok else CallState.INCOMPLETE.value
        return await self._finalize(outcome=outcome, status=status)

    # ---------------- override hook CallSession ----------------

    def _kickoff_text(self) -> str:
        return (
            "Ada panggilan masuk. Sapa penelepon sebagai receptionist virtual, tanyakan keperluannya. "
            "Kalau perlu bicara dengan orang, panggil request_human_agent dengan queue "
            "'support', 'sales', atau 'campaign' dan isi summary singkat keperluan penelepon."
        )

    async def _execute_tool_call(self, name: str, args: dict) -> dict:
        result = await super()._execute_tool_call(name, args)
        if name == "request_human_agent":
            args = args or {}
            queue = str(args.get("queue", DEFAULT_QUEUE)).strip().lower()
            self._transfer_queue = queue if queue in ALLOWED_QUEUES else DEFAULT_QUEUE
            self._summary = _clean(args.get("summary") or args.get("reason") or "")
            self._transfer_at = asyncio.get_event_loop().time()
        return result

    def _pipeline_end_predicate(self) -> bool:
        if not self._telephony_connected:
            return True
        if self._transfer_queue:
            # farewell serah-terima sudah selesai diucapkan -> hentikan untuk transfer
            return True
        return self.state.end_requested

    def _should_stop(self, turn_event) -> bool:
        if not self._telephony_connected:
            return True
        if self._transfer_queue:
            # biarkan AI selesai mengucapkan kalimat serah-terima
            waited = asyncio.get_event_loop().time() - (self._transfer_at or 0)
            return bool(turn_event.turn_complete) or waited > TRANSFER_MAX_WAIT_SEC
        return self.state.end_requested

    # ---------------- transfer & finalize ----------------

    async def _transfer(self):
        try:
            await self.ari.set_channel_variable(self.channel_id, "AI_SUMMARY", self._summary or "-")
            await self.ari.continue_in_dialplan(self.channel_id, "queue-router", self._transfer_queue, 1)
            logger.info("Transfer ke queue=%s session=%s", self._transfer_queue, self.session_id)
        except Exception:
            logger.exception("Transfer gagal, session=%s", self.session_id)
            try:
                await self.ari.hangup_channel(self.channel_id)
            except Exception:
                pass

    async def _finalize(self, outcome: str, status: str, error: str = None) -> dict:
        ended_at = now_iso()
        duration = int(
            (datetime.fromisoformat(ended_at) - datetime.fromisoformat(self.connected_at)).total_seconds()
        )
        await self.db.execute(
            "UPDATE call_session SET ended_at=?, duration=?, status=?, outcome=? WHERE id=?",
            (ended_at, duration, status, outcome, self.session_id),
        )
        await self.db.log_event(
            "CALL_COMPLETED", call_session_id=self.session_id, status=status, error=error
        )
        return {"session_id": self.session_id, "status": status, "outcome": outcome, "duration": duration}


class InboundManager:
    """Satu handler StasisStart/ChannelDestroyed untuk semua panggilan inbound."""

    def __init__(self, ari, db, port_pool, topic_factory):
        self.ari = ari
        self.db = db
        self.port_pool = port_pool
        self.topic_factory = topic_factory  # callable (sync/async) -> Topic receptionist
        self.sessions: dict[str, InboundSession] = {}
        ari.on_event("StasisStart", self._on_stasis_start)
        ari.on_event("ChannelDestroyed", self._on_channel_destroyed)

    def _on_stasis_start(self, event: dict):
        args = event.get("args") or []
        if not args or args[0] != "inbound":
            return  # outbound campaign / externalMedia diurus modul lain
        channel = event["channel"]
        if len(args) >= 3 and args[1] == "fallback":
            asyncio.create_task(self._handle_fallback(channel["id"], args[2]))
        else:
            asyncio.create_task(self._run_session(channel))

    def _on_channel_destroyed(self, event: dict):
        session = self.sessions.get(event.get("channel", {}).get("id"))
        if session:
            session._on_channel_destroyed(event)

    async def _run_session(self, channel: dict):
        channel_id = channel["id"]
        caller = channel.get("caller", {}).get("number", "")
        logger.info("Inbound call channel=%s caller=%s", channel_id, caller)
        try:
            topic = self.topic_factory()
            if inspect.isawaitable(topic):
                topic = await topic
            session = InboundSession(self.ari, self.db, self.port_pool, channel_id, caller, topic)
            self.sessions[channel_id] = session
            result = await session.run()
            logger.info("Inbound selesai: %s", result)
        except Exception:
            logger.exception("Inbound session error channel=%s", channel_id)
            try:
                await self.ari.hangup_channel(channel_id)
            except Exception:
                pass
        finally:
            self.sessions.pop(channel_id, None)

    async def _handle_fallback(self, channel_id: str, queue: str):
        logger.info("Fallback: queue=%s tidak menjawab, channel=%s", queue, channel_id)
        try:
            # Ganti dengan rekaman sendiri, mis. "sound:custom/agen-tidak-tersedia"
            await self.ari.play_sound(channel_id, "sound:vm-goodbye")
            await asyncio.sleep(4)
        except Exception:
            logger.exception("Fallback playback gagal")
        finally:
            try:
                await self.ari.hangup_channel(channel_id)
            except Exception:
                pass
