"""
Wrapper database SQLite (async via thread executor) untuk seluruh entitas PRD #26.
Sengaja tetap tipis (bukan ORM) supaya mudah diganti ke MySQL/Postgres nanti.
"""
import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

from config import settings


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self):
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with sqlite3.connect(self.db_path) as conn:
            with open(schema_path, "r", encoding="utf-8") as f:
                conn.executescript(f.read())
            conn.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def execute(self, query: str, params: tuple = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_sync, query, params)

    def _execute_sync(self, query: str, params: tuple):
        with self._conn() as conn:
            conn.execute(query, params)

    async def fetchone(self, query: str, params: tuple = ()) -> Optional[dict]:
        async with self._lock:
            row = await asyncio.to_thread(self._fetchone_sync, query, params)
        return dict(row) if row else None

    def _fetchone_sync(self, query: str, params: tuple):
        with self._conn() as conn:
            cur = conn.execute(query, params)
            return cur.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        async with self._lock:
            rows = await asyncio.to_thread(self._fetchall_sync, query, params)
        return [dict(r) for r in rows]

    def _fetchall_sync(self, query: str, params: tuple):
        with self._conn() as conn:
            cur = conn.execute(query, params)
            return cur.fetchall()

    # ---- helper spesifik yang sering dipakai ----

    async def log_event(
        self,
        event: str,
        campaign_id: str = None,
        target_id: str = None,
        call_session_id: str = None,
        ari_call_id: str = None,
        gemini_session_id: str = None,
        status: str = None,
        error: str = None,
    ):
        await self.execute(
            """INSERT INTO call_event_log
               (campaign_id, target_id, call_session_id, ari_call_id, gemini_session_id,
                event, status, error, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (campaign_id, target_id, call_session_id, ari_call_id, gemini_session_id,
             event, status, error, now_iso()),
        )

    async def add_transcript(self, call_session_id: str, speaker: str, text: str):
        await self.execute(
            "INSERT INTO transcript (id, call_session_id, speaker, text, timestamp) VALUES (?,?,?,?,?)",
            (new_id("trs"), call_session_id, speaker, text, now_iso()),
        )

    async def record_answer(
        self,
        call_session_id: str,
        question_id: str,
        answer_text: str,
        answer_status: str,
        confidence: float = None,
    ):
        await self.execute(
            """INSERT INTO conversation_answer
               (id, call_session_id, question_id, answer_text, answer_status, confidence, answered_at)
               VALUES (?,?,?,?,?,?,?)""",
            (new_id("ans"), call_session_id, question_id, answer_text, answer_status,
             confidence, now_iso()),
        )
