"""
Custom ARI client (REST via aiohttp + event stream via websockets).

Dipilih daripada library `ari`/swaggerpy karena di project robocall sebelumnya,
loading Swagger doc-nya (banyak request berurutan tanpa jeda) bikin Asterisk
HTTP server drop koneksi (RemoteDisconnected). Client ini jauh lebih ringan:
cuma REST call yang benar-benar dipakai + satu koneksi WS untuk event stream.
"""
import asyncio
import base64
import json
import logging
from typing import Callable, Optional

import aiohttp
import websockets

from config import AriConfig

logger = logging.getLogger("ari_client")


class AriClient:
    def __init__(self, cfg: AriConfig):
        self.cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None
        self._event_handlers: dict[str, list[Callable]] = {}
        self._running = False

    # ---------------- lifecycle ----------------

    async def connect(self):
        auth = aiohttp.BasicAuth(self.cfg.user, self.cfg.password)
        self._session = aiohttp.ClientSession(auth=auth)
        self._running = True
        asyncio.create_task(self._event_loop())
        logger.info("ARI client connected, listening app=%s", self.cfg.app)

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    def on_event(self, event_type: str, handler: Callable):
        """Daftarkan handler(event: dict) untuk tipe event ARI tertentu."""
        self._event_handlers.setdefault(event_type, []).append(handler)

    async def _event_loop(self):
        ws_url = (
            self.cfg.base_url.replace("http://", "ws://").replace("https://", "wss://")
            + f"/ari/events?api_key={self.cfg.user}:{self.cfg.password}&app={self.cfg.app}&subscribeAll=true"
        )
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("ARI event websocket connected")
                    async for raw in ws:
                        event = json.loads(raw)
                        await self._dispatch(event)
            except Exception as e:
                logger.warning("ARI event websocket error: %s (retry in %ss)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _dispatch(self, event: dict):
        etype = event.get("type")
        for handler in self._event_handlers.get(etype, []):
            try:
                res = handler(event)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("Error in ARI event handler for %s", etype)

    # ---------------- REST helpers ----------------

    async def _request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        url = f"{self.cfg.base_url}/ari{path}"
        async with self._session.request(method, url, params=params, json=json_body) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"ARI {method} {path} -> {resp.status}: {text}")
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    # ---------------- channels ----------------

    async def originate(
        self,
        endpoint: str,
        caller_id: str,
        timeout_sec: int,
        variables: dict = None,
    ) -> dict:
        """
        Originate outbound call langsung masuk ke Stasis app (app=self.cfg.app)
        begitu channel terjawab/di-answer oleh peer.
        """
        params = {
            "endpoint": endpoint,
            "app": self.cfg.app,
            "callerId": caller_id,
            "timeout": timeout_sec,
        }
        body = {"variables": variables or {}}
        return await self._request("POST", "/channels", params=params, json_body=body)

    async def answer_channel(self, channel_id: str):
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def hangup_channel(self, channel_id: str, reason: str = "normal"):
        await self._request("DELETE", f"/channels/{channel_id}", params={"reason": reason})

    async def get_channel(self, channel_id: str) -> dict:
        return await self._request("GET", f"/channels/{channel_id}")

    async def play_sound(self, channel_id: str, media: str) -> dict:
        return await self._request("POST", f"/channels/{channel_id}/play", params={"media": media})

    # ---------------- bridges + externalMedia (audio bridging ke Gemini) ----------------

    async def create_bridge(self, bridge_type: str = "mixing") -> dict:
        return await self._request("POST", "/bridges", params={"type": bridge_type})

    async def add_channel_to_bridge(self, bridge_id: str, channel_id: str):
        await self._request(
            "POST", f"/bridges/{bridge_id}/addChannel", params={"channel": channel_id}
        )

    async def destroy_bridge(self, bridge_id: str):
        await self._request("DELETE", f"/bridges/{bridge_id}")

    async def create_external_media_channel(
        self, external_host: str, codec: str, encapsulation: str = "rtp"
    ) -> dict:
        """
        Membuat channel externalMedia: Asterisk akan mengirim RTP audio caller ke
        external_host:port ini, dan menerima audio balik (respons Gemini) dari
        alamat yang sama (symmetric RTP).
        """
        params = {
            "app": self.cfg.app,
            "external_host": external_host,   # format "host:port"
            "format": codec,
            "encapsulation": encapsulation,
            "transport": "udp",
            "direction": "both",
        }
        return await self._request("POST", "/channels/externalMedia", params=params)
