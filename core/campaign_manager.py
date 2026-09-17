"""
Campaign manager: polling scheduler yang mengambil target siap-panggil,
menghormati jam operasional & timezone, mengatur concurrency per campaign,
dan menjadwalkan ulang (retry) sesuai konfigurasi.
"""
import asyncio
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from config import settings
from core.ari_client import AriClient
from core.call_session import CallSession
from core.rtp_bridge import RtpPortPool
from core.topic_config import Question, Topic
from db.database import Database, now_iso

logger = logging.getLogger("campaign_manager")


class CampaignManager:
    def __init__(self, ari: AriClient, db: Database):
        self.ari = ari
        self.db = db
        self.port_pool = RtpPortPool(settings.rtp)
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._running = False
        self._tz = ZoneInfo(settings.campaign.timezone)

    async def start(self):
        self._running = True
        logger.info("Campaign manager started (poll interval=%ss)", settings.scheduler_poll_interval_sec)
        while self._running:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("Error saat polling campaign")
            await asyncio.sleep(settings.scheduler_poll_interval_sec)

    def stop(self):
        self._running = False

    async def _poll_once(self):
        campaigns = await self.db.fetchall("SELECT * FROM campaign WHERE status='ACTIVE'")
        for campaign in campaigns:
            if not self._within_call_window(campaign):
                continue
            sem = self._semaphores.setdefault(
                campaign["id"], asyncio.Semaphore(campaign["concurrent_limit"])
            )
            if sem.locked() and sem._value == 0:  # noqa: SLF001 — cek cepat tanpa reserve
                continue
            targets = await self.db.fetchall(
                """SELECT * FROM campaign_target
                   WHERE campaign_id=? AND status='PENDING'
                     AND (scheduled_at IS NULL OR scheduled_at <= ?)
                   ORDER BY scheduled_at IS NULL, scheduled_at LIMIT ?""",
                (campaign["id"], now_iso(), campaign["concurrent_limit"]),
            )
            for target in targets:
                if sem._value == 0:  # noqa: SLF001
                    break
                asyncio.create_task(self._dispatch_call(campaign, target, sem))

    def _within_call_window(self, campaign: dict) -> bool:
        now = datetime.now(self._tz)
        today = now.date().isoformat()
        if not (campaign["start_date"] <= today <= campaign["end_date"]):
            return False
        start_t = dtime.fromisoformat(campaign["call_start_time"])
        end_t = dtime.fromisoformat(campaign["call_end_time"])
        return start_t <= now.time() <= end_t

    async def _dispatch_call(self, campaign: dict, target: dict, sem: asyncio.Semaphore):
        async with sem:
            await self.db.execute(
                "UPDATE campaign_target SET status='CALLING', called_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), target["id"]),
            )
            topic = await self._load_topic(campaign["topic_id"])
            session = CallSession(self.ari, self.db, self.port_pool, target, topic)
            result = await session.run()
            await self._handle_retry_if_needed(campaign, target, result)

    async def _handle_retry_if_needed(self, campaign: dict, target: dict, result: dict):
        if result["status"] not in settings.campaign.retryable_outcomes:
            return
        retry_count = target["retry_count"] + 1
        if retry_count > campaign["max_retry"]:
            await self.db.execute(
                "UPDATE campaign_target SET status=?, outcome=? WHERE id=?",
                (f"FINAL_{result['status']}", result["outcome"], target["id"]),
            )
            return
        await self.db.execute(
            """UPDATE campaign_target
               SET status='PENDING', retry_count=?, scheduled_at=?, updated_at=?
               WHERE id=?""",
            (retry_count, now_iso(), now_iso(), target["id"]),
        )
        logger.info("Target %s dijadwalkan retry ke-%s", target["id"], retry_count)

    async def _load_topic(self, topic_id: str) -> Topic:
        row = await self.db.fetchone("SELECT * FROM topic WHERE id=?", (topic_id,))
        q_rows = await self.db.fetchall(
            "SELECT * FROM topic_question WHERE topic_id=? AND status='ACTIVE'", (topic_id,)
        )
        questions = [
            Question(
                id=q["id"], text=q["question_text"], order=q["question_order"],
                is_mandatory=bool(q["is_mandatory"]), answer_type=q["answer_type"],
                validation_rule=q["validation_rule"], next_question=q["next_question"],
            )
            for q in q_rows
        ]
        return Topic(
            id=row["id"], name=row["name"], objective=row["objective"],
            questions=questions, custom_instruction=row["system_instruction"] or "",
        )
