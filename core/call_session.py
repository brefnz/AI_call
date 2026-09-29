"""
Orkestrasi satu sesi panggilan outbound, dari originate sampai report tersimpan.
Menyatukan: ari_client (telephony) + rtp_bridge (audio transport) +
gemini_live (STT+TTS+conversation) + conversation_state (guardrail/tracking).
"""
import asyncio
import logging
from datetime import datetime

from config import settings
from core.ari_client import AriClient
from core.audio_utils import Resampler
from core.conversation_state import ConversationState, CallState
from core.gemini_live import GeminiLiveSession
from core.rtp_bridge import RtpEndpoint, RtpPortPool
from core.topic_config import Topic
from db.database import Database, new_id, now_iso

logger = logging.getLogger("call_session")

# Hangup cause -> status/outcome mapping (subset umum, sesuaikan dgn Asterisk causes.h bila perlu)
HANGUP_CAUSE_MAP = {
    16: ("COMPLETED", None),        # NORMAL_CLEARING — biarkan outcome dihitung dari conversation
    17: ("BUSY", "BUSY"),
    18: ("NO_ANSWER", "NO_ANSWER"),
    19: ("NO_ANSWER", "NO_ANSWER"),
    21: ("REJECTED", "REJECTED"),
    1: ("FAILED", "FAILED"),        # UNALLOCATED_NUMBER / invalid
}


class CallSession:
    def __init__(
        self,
        ari: AriClient,
        db: Database,
        port_pool: RtpPortPool,
        target: dict,
        topic: Topic,
    ):
        self.ari = ari
        self.db = db
        self.port_pool = port_pool
        self.target = target
        self.topic = topic

        self.session_id = new_id("call")
        self.state = ConversationState(topic)
        self.channel_id: str | None = None
        self.bridge_id: str | None = None
        self.em_channel_id: str | None = None
        self.rtp: RtpEndpoint | None = None
        self._stasis_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._channel_ended_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._telephony_connected = False
        self.started_at = None
        self.connected_at = None

        # REVISI: buffer untuk gabungkan transcript_text yang di-stream Gemini
        # per kata/token (bukan per kalimat) sebelum disimpan ke DB. Tanpa
        # ini, setiap event transcript_text yang datang (bisa puluhan per
        # kalimat) langsung jadi baris transcript terpisah di dashboard --
        # lihat _flush_transcript_buffer() dan pemakaiannya di run().
        self._transcript_buffer = {"speaker": None, "text": ""}

    # ---------------- public entrypoint ----------------

    async def run(self) -> dict:
        self.started_at = now_iso()
        await self.db.execute(
            """INSERT INTO call_session (id, campaign_target_id, started_at, status)
               VALUES (?,?,?,?)""",
            (self.session_id, self.target["id"], self.started_at, CallState.CALLING.value),
        )
        await self.db.log_event("CALL_INITIATED", target_id=self.target["id"], call_session_id=self.session_id)

        self.ari.on_event("StasisStart", self._on_stasis_start)
        self.ari.on_event("ChannelDestroyed", self._on_channel_destroyed)
        self.ari.on_event("ChannelStateChange", self._on_channel_state_change)

        endpoint = settings.ari.outbound_endpoint_template.format(number=self.target["phone_number"])
        try:
            channel = await self.ari.originate(
                endpoint=endpoint,
                caller_id=settings.ari.caller_id,
                timeout_sec=settings.ari.originate_timeout_sec,
                variables={"CALL_SESSION_ID": self.session_id},
            )
        except Exception as e:
            logger.exception("Originate gagal untuk target %s", self.target["id"])
            return await self._finalize(outcome="FAILED", status="FAILED", error=str(e))

        self.channel_id = channel["id"]

        try:
            done, _ = await asyncio.wait(
                [self._stasis_future, self._channel_ended_future],
                timeout=settings.ari.originate_timeout_sec + 10,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception as e:
            return await self._finalize(outcome="FAILED", status="FAILED", error=str(e))

        if self._channel_ended_future in done and not self._stasis_future.done():
            cause = self._channel_ended_future.result()
            status, outcome = HANGUP_CAUSE_MAP.get(cause, ("FAILED", "FAILED"))
            return await self._finalize(outcome=outcome or status, status=status)

        if not self._stasis_future.done():
            # timeout tanpa StasisStart maupun ChannelDestroyed -> anggap no answer
            return await self._finalize(outcome="NO_ANSWER", status="NO_ANSWER")

        # ---- tersambung ----
        self._telephony_connected = True
        self.connected_at = now_iso()
        await self.db.execute(
            "UPDATE call_session SET connected_at=?, status=? WHERE id=?",
            (self.connected_at, CallState.CONNECTED.value, self.session_id),
        )
        await self.db.log_event("CALL_CONNECTED", call_session_id=self.session_id, ari_call_id=self.channel_id)

        try:
            outcome = await self._run_conversation()
        except Exception as e:
            logger.exception("Error selama percakapan, session=%s", self.session_id)
            outcome = "FAILED"
        finally:
            await self._cleanup_telephony()

        conv_status = CallState.COMPLETED.value if outcome == "SUCCESS" else CallState.INCOMPLETE.value
        return await self._finalize(outcome=outcome, status=conv_status)

    # ---------------- ARI event handlers ----------------

    def _on_stasis_start(self, event: dict):
        if event.get("channel", {}).get("id") == self.channel_id and not self._stasis_future.done():
            self._stasis_future.set_result(event)

    def _on_channel_destroyed(self, event: dict):
        if event.get("channel", {}).get("id") == self.channel_id and not self._channel_ended_future.done():
            logger.info(
                "ChannelDestroyed session=%s cause=%s cause_txt=%s",
                self.session_id, event.get("cause"), event.get("cause_txt"),
            )
            # NEW: tanpa ini, _telephony_connected tetap True selamanya, dan
            # pengecekan "or not self._telephony_connected: break" di loop
            # conversation nggak pernah efektif -- sesi Gemini bisa nggantung
            # lama setelah telepon ditutup (baru mati kalau Gemini sendiri
            # yang nutup koneksi).
            self._telephony_connected = False
            self._channel_ended_future.set_result(event.get("cause", 16))

    def _on_channel_state_change(self, event: dict):
        pass  # hook tersedia untuk kebutuhan monitoring tambahan

    # ---------------- conversation loop ----------------

    async def _run_conversation(self) -> str:
        await self.ari.answer_channel(self.channel_id)

        self.bridge_id = (await self.ari.create_bridge())["id"]
        await self.ari.add_channel_to_bridge(self.bridge_id, self.channel_id)

        port = await self.port_pool.acquire()
        self.rtp = RtpEndpoint(settings.rtp, port)
        # DEBUG SEMENTARA sudah dimatikan: tujuan loopback test (buktikan
        # Asterisk mau meneruskan RTP UnicastRTP->PJSIP) sudah terbukti
        # berhasil setelah pindah ke ulaw. Dibiarkan True akan bikin audio AI
        # putus-putus, karena setiap paket suara caller yang masuk (terus-
        # menerus, ~50x/detik) memicu task echo yang ikut berebut
        # self.rtp._send_lock dengan audio balasan Gemini -- keduanya sama-
        # sama pakai real-time pacing (asyncio.sleep 20ms/paket) di lock yang
        # sama, jadi saling menyela.
        self.rtp._loopback_test = False
        await self.rtp.start()

        em_channel = await self.ari.create_external_media_channel(
            external_host=f"{settings.rtp.advertise_host}:{port}",
            codec=settings.rtp.codec,
        )
        self.em_channel_id = em_channel["id"]
        await self.ari.add_channel_to_bridge(self.bridge_id, self.em_channel_id)

        resampler_out = Resampler(
            in_rate=settings.gemini.output_sample_rate_hz,
            out_rate=settings.rtp.sample_rate_hz,
        )
        # REVISI: RTP sekarang 8kHz (ulaw, lihat config.py) sedangkan Gemini
        # Live selalu butuh PCM16@16kHz untuk audio masuk (kontrak
        # GeminiConfig.input_sample_rate_hz, lihat gemini_live.py send_audio()
        # -- itu cuma LABEL rate ke API, bukan resample otomatis). Tanpa ini,
        # audio caller yang diteruskan ke Gemini akan salah rate (dianggap
        # 16kHz padahal sebenarnya 8kHz), bikin suara caller terdengar
        # setengah kecepatan di telinga model dan STT-nya berantakan.
        self._resampler_in = Resampler(
            in_rate=settings.rtp.sample_rate_hz,
            out_rate=settings.gemini.input_sample_rate_hz,
        )

        async with GeminiLiveSession(settings.gemini, self.topic) as gemini:
            await self.db.execute(
                "UPDATE call_session SET status=?, gemini_session_id=? WHERE id=?",
                (CallState.IN_PROGRESS.value, self.session_id, self.session_id),
            )
            await self.db.log_event(
                "GEMINI_SESSION_STARTED", call_session_id=self.session_id, gemini_session_id=self.session_id
            )

            # Tunggu paket RTP pertama dari Asterisk diterima (remote_addr +
            # payload_type kepelajari) SEBELUM memicu sapaan pembuka. Tanpa ini,
            # audio sapaan bisa ketahan/hilang kalau AI mulai bicara duluan
            # sebelum bridge externalMedia benar-benar mengalirkan RTP. Timeout
            # dikasih biar nggak nge-hang call selamanya kalau externalMedia
            # gagal connect sama sekali.
            try:
                await self.rtp.wait_ready(timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "RTP endpoint belum terima paket pertama dari Asterisk "
                    "setelah 3 detik, session=%s -- lanjut aja, audio awal "
                    "mungkin sempat ketahan di buffer",
                    self.session_id,
                )

            await gemini.send_text(
                "Mulai panggilan ini. Sapa penelepon dengan ramah, perkenalkan topik campaign secara "
                "singkat, lalu mulai ajukan pertanyaan pertama."
            )

            uplink_task = asyncio.create_task(self._pump_caller_audio_to_gemini(gemini))

            # NEW: watcher yang AKTIF nge-cancel conversation loop begitu
            # ChannelDestroyed masuk. Tanpa ini, loop cuma berhenti kalau
            # kebetulan lagi nunggu event Gemini berikutnya -- kalau Gemini
            # nggak ngirim event apa-apa lagi setelah hangup, loop nggantung
            # sampai koneksi Gemini timeout/error sendiri (bisa menit-menitan).
            conversation_task = asyncio.current_task()

            async def _hangup_watcher():
                await self._channel_ended_future
                logger.info(
                    "Hangup terdeteksi, hentikan conversation loop, session=%s",
                    self.session_id,
                )
                conversation_task.cancel()

            watcher_task = asyncio.create_task(_hangup_watcher())

            event_count = 0
            try:
                async for turn_event in gemini.events():
                    event_count += 1
                    logger.info(
                        "Gemini event #%d session=%s audio_bytes=%s transcript=%r tool=%s turn_complete=%s",
                        event_count, self.session_id,
                        len(turn_event.audio_pcm) if turn_event.audio_pcm else None,
                        turn_event.transcript_text,
                        turn_event.tool_call.get("name") if turn_event.tool_call else None,
                        turn_event.turn_complete,
                    )

                    if turn_event.audio_pcm:
                        logger.info(
                            "GEMINI AUDIO BEFORE RESAMPLE: len=%d first32=%s",
                            len(turn_event.audio_pcm),
                            turn_event.audio_pcm[:32].hex(" ")
                        )

                        pcm16k = resampler_out.process(turn_event.audio_pcm)

                        logger.info(
                            "GEMINI AUDIO AFTER RESAMPLE: len=%d first32=%s",
                            len(pcm16k),
                            pcm16k[:32].hex(" ")
                        )

                        await self.rtp.send_pcm(pcm16k)

                    if turn_event.transcript_text:
                        # REVISI: jangan langsung INSERT tiap potongan --
                        # gabungkan dulu di buffer selama speaker sama, flush
                        # (simpan sebagai satu baris utuh) begitu speaker
                        # ganti atau giliran bicara selesai (turn_complete).
                        buf = self._transcript_buffer
                        if buf["speaker"] not in (None, turn_event.transcript_speaker):
                            await self._flush_transcript_buffer()
                            buf = self._transcript_buffer
                        buf["speaker"] = turn_event.transcript_speaker
                        buf["text"] += turn_event.transcript_text

                    if turn_event.turn_complete:
                        await self._flush_transcript_buffer()

                    if turn_event.tool_call:
                        await self._handle_tool_call(gemini, turn_event.tool_call)

                    if self.state.end_requested or not self._telephony_connected:
                        break
            except asyncio.CancelledError:
                logger.info(
                    "Conversation loop di-cancel (kemungkinan karena hangup), session=%s",
                    self.session_id,
                )
            finally:
                watcher_task.cancel()
                # Jaga-jaga: kalau call berakhir (hangup/cancel) di tengah
                # kalimat sebelum turn_complete sempat diterima, sisa buffer
                # tetap disimpan -- daripada hilang begitu saja.
                await self._flush_transcript_buffer()
                logger.info("Conversation loop selesai, total event dari Gemini=%d session=%s", event_count, self.session_id)
                uplink_task.cancel()

        outcome = self.state.compute_outcome(telephony_connected=True)
        quality = self.state.compute_quality()
        await self.db.execute(
            "UPDATE call_session SET conversation_result=? WHERE id=?",
            (quality, self.session_id),
        )
        return outcome

    async def _flush_transcript_buffer(self):
        """
        Simpan buffer transcript_text yang terkumpul (satu speaker, satu
        giliran bicara) sebagai SATU baris di tabel transcript, lalu kosongkan
        buffer. Dipanggil saat speaker ganti, turn_complete, atau di finally
        block loop percakapan (lihat run()).
        """
        buf = self._transcript_buffer
        text = buf["text"].strip()
        if text:
            await self.db.add_transcript(self.session_id, buf["speaker"], text)
        self._transcript_buffer = {"speaker": None, "text": ""}

    async def _pump_caller_audio_to_gemini(self, gemini: GeminiLiveSession):
        while True:
            pcm = await self.rtp.pcm_in_queue.get()
            # REVISI: pcm dari rtp.pcm_in_queue sekarang PCM16@8kHz (hasil
            # decode ulaw di rtp_bridge.py), sedangkan Gemini butuh 16kHz --
            # lihat catatan di _resampler_in di atas.
            pcm16k = self._resampler_in.process(pcm)
            await gemini.send_audio(pcm16k)

    async def _handle_tool_call(self, gemini: GeminiLiveSession, tool_call: dict):
        name = tool_call["name"]
        args = tool_call["args"]
        call_id = tool_call["id"]

        if name == "record_answer":
            question_id = args.get("question_id")
            status = args.get("status", "UNCLEAR")
            answer_text = args.get("answer_text", "")
            ok = self.state.record_answer(question_id, answer_text, status)
            if ok:
                await self.db.record_answer(self.session_id, question_id, answer_text, status)
                await self.db.log_event(
                    "ANSWER_RECEIVED", call_session_id=self.session_id, status=status
                )
            await gemini.send_tool_response(call_id, name, {"recorded": ok})

        elif name == "request_human_agent":
            self.state.request_human_agent()
            await self.db.log_event("REQUEST_HUMAN_AGENT", call_session_id=self.session_id)
            # TODO: jika transfer ARI ke human agent tersedia, panggil ari.originate/redirect di sini.
            await gemini.send_tool_response(call_id, name, {"acknowledged": True})

        elif name == "end_conversation":
            self.state.request_end(args.get("summary", ""))
            await self.db.log_event(
                "CALL_COMPLETED", call_session_id=self.session_id, status="END_REQUESTED"
            )
            await gemini.send_tool_response(call_id, name, {"acknowledged": True})

        else:
            logger.warning("Tool call tidak dikenal: %s", name)
            await gemini.send_tool_response(call_id, name, {"error": "unknown tool"})

    # ---------------- cleanup & report ----------------

    async def _cleanup_telephony(self):
        try:
            if self.em_channel_id:
                await self.ari.hangup_channel(self.em_channel_id)
        except Exception:
            pass
        try:
            if self.bridge_id:
                await self.ari.destroy_bridge(self.bridge_id)
        except Exception:
            pass
        try:
            if self.channel_id:
                await self.ari.hangup_channel(self.channel_id)
        except Exception:
            pass
        if self.rtp:
            self.rtp.close()
            await self.port_pool.release(self.rtp.port)

    async def _finalize(self, outcome: str, status: str, error: str = None) -> dict:
        ended_at = now_iso()
        duration = None
        if self.connected_at:
            duration = int(
                (datetime.fromisoformat(ended_at) - datetime.fromisoformat(self.connected_at)).total_seconds()
            )
        await self.db.execute(
            "UPDATE call_session SET ended_at=?, duration=?, status=?, outcome=? WHERE id=?",
            (ended_at, duration, status, outcome, self.session_id),
        )
        await self.db.execute(
            """UPDATE campaign_target
               SET status=?, outcome=?, ended_at=?, duration=?, updated_at=?
               WHERE id=?""",
            (status, outcome, ended_at, duration, ended_at, self.target["id"]),
        )
        await self.db.log_event(
            "CALL_COMPLETED", target_id=self.target["id"], call_session_id=self.session_id,
            status=status, error=error,
        )
        return {"session_id": self.session_id, "status": status, "outcome": outcome, "duration": duration}
