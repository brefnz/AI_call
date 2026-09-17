"""
Klien integrasi Datakelola (PRD #25). Endpoint di bawah adalah PLACEHOLDER —
sesuaikan path/payload dengan dokumentasi API Datakelola yang sebenarnya begitu
tersedia. Struktur fungsi sudah mengikuti kebutuhan minimal PRD:

  GET  /api/blasting
  GET  /api/blasting/{id}/targets
  GET  /api/topics/{id}
  POST /api/blasting/{id}/results
  POST /api/calls/{id}/status
  POST /api/calls/{id}/conversation
"""
import logging

import aiohttp

from config import DatakelolaConfig

logger = logging.getLogger("datakelola_client")


class DatakelolaClient:
    def __init__(self, cfg: DatakelolaConfig):
        self.cfg = cfg

    def _headers(self):
        return {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}

    async def _get(self, path: str) -> dict:
        async with aiohttp.ClientSession(headers=self._headers()) as s:
            async with s.get(f"{self.cfg.base_url}{path}") as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post(self, path: str, payload: dict) -> dict:
        async with aiohttp.ClientSession(headers=self._headers()) as s:
            async with s.post(f"{self.cfg.base_url}{path}", json=payload) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def list_blasting(self) -> list[dict]:
        if not self.cfg.enabled:
            logger.debug("Datakelola disabled — skip list_blasting")
            return []
        return (await self._get("/api/blasting")).get("data", [])

    async def get_blasting_targets(self, blasting_id: str) -> list[dict]:
        if not self.cfg.enabled:
            return []
        return (await self._get(f"/api/blasting/{blasting_id}/targets")).get("data", [])

    async def get_topic(self, topic_id: str) -> dict:
        if not self.cfg.enabled:
            return {}
        return (await self._get(f"/api/topics/{topic_id}")).get("data", {})

    async def send_result(self, blasting_id: str, result_payload: dict):
        if not self.cfg.enabled:
            logger.debug("Datakelola disabled — skip send_result")
            return
        await self._post(f"/api/blasting/{blasting_id}/results", result_payload)

    async def send_call_status(self, call_id: str, status_payload: dict):
        if not self.cfg.enabled:
            return
        await self._post(f"/api/calls/{call_id}/status", status_payload)

    async def send_conversation(self, call_id: str, conversation_payload: dict):
        if not self.cfg.enabled:
            return
        await self._post(f"/api/calls/{call_id}/conversation", conversation_payload)
