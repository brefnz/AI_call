"""
Jembatan audio RTP <-> PCM mentah, dipakai untuk menghubungkan channel
externalMedia Asterisk dengan Gemini Live API.

REVISI (ganti codec slin16 -> ulaw untuk investigasi TX=0):
- Sebelumnya endpoint ini murni transparan: payload RTP diteruskan apa adanya
  sebagai PCM16 (slin16) ke/dari pcm_in_queue dan send_pcm(). Ternyata di
  server ini, kombinasi bridge (simple_bridge) + UnicastRTP(slin16) +
  transcode ke PJSIP(ulaw) membuat Asterisk TIDAK PERNAH mengirim RTP balik
  ke sisi PJSIP (dibuktikan dengan "rtp set debug on": nol baris
  "Sent RTP packet to <ip telepon>" walau semua bridge/format/transcode-path
  terbukti benar di level channel). Tes "channel originate ... Playback"
  langsung ke PJSIP/101 terdengar normal, jadi RTP dasar Asterisk<->PJSIP
  sehat -- masalahnya spesifik di transcoding pada bridge ini.
- Fix: samakan codec kedua sisi jadi ulaw (native format PJSIP/101), supaya
  Asterisk tidak perlu transcode di bridge sama sekali. Encode/decode
  ulaw<->PCM16 dilakukan DI SINI (pakai modul stdlib `audioop`), supaya
  call_session.py & kode lain di atasnya tetap bekerja dengan PCM16 seperti
  sebelumnya -- tidak perlu diubah.
- codec & sample_rate_hz diambil dari RtpBridgeConfig (cfg.codec,
  cfg.sample_rate_hz). Untuk ulaw, set cfg.codec="ulaw" dan
  cfg.sample_rate_hz=8000 di config.py. Kalau cfg.codec bukan "ulaw", perilaku
  lama (slin16, passthrough PCM16 apa adanya) tetap dipakai -- jadi gampang
  di-revert cuma dengan ganti config, tanpa ubah kode ini lagi.

Catatan lama (masih berlaku):
- Implementasi RTP di sini minimal by design: parsing/pembuatan header 12-byte
  standar RFC 3550 tanpa CSRC/extension.
- "Symmetric RTP": begitu paket pertama diterima dari Asterisk, alamat sumbernya
  dipakai sebagai tujuan kirim balik.
- Payload type RTP dipelajari otomatis dari paket pertama Asterisk, tidak
  ditebak/hardcode.
- send_pcm() menahan (buffer) audio yang mau dikirim sebelum remote_addr/
  payload_type kepelajari, lalu di-flush begitu paket pertama diterima.
- SimpleJitterBuffer untuk arah caller->AI: susun ulang berdasarkan sequence
  number, gap diisi silence.
- Seluruh pengiriman dikunci dengan asyncio.Lock (_send_lock) supaya
  _out_seq/_out_ts tidak diserobot task lain yang manggil send_pcm() bersamaan.
- SSRC random per instance RtpEndpoint (bukan hardcode).

DEBUG SEMENTARA (hapus setelah investigasi selesai):
- Flag `_loopback_test` di RtpEndpoint: kalau True, setiap payload yang
  diterima dari Asterisk langsung dikirim balik apa adanya (echo) lewat
  send_pcm(), TANPA lewat Gemini/resampler.
"""
import asyncio
import audioop
import logging
import random
import struct
from typing import Optional

from config import RtpBridgeConfig

logger = logging.getLogger("rtp_bridge")

RTP_HEADER_LEN = 12

# Payload type statis standar RFC 3551 untuk ulaw/PCMU. Dipakai sebagai
# fallback SEBELUM payload type asli dipelajari dari paket pertama Asterisk
# (Asterisk sendiri konsisten memakai PT=0 untuk ulaw, beda dengan slin16 yang
# dynamic/>=96).
PT_ULAW = 0


def _parse_rtp(packet: bytes) -> Optional[tuple]:
    if len(packet) < RTP_HEADER_LEN:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", packet[:RTP_HEADER_LEN])
    payload_type = b1 & 0x7F
    payload = packet[RTP_HEADER_LEN:]
    return payload_type, seq, ts, ssrc, payload


def _build_rtp(payload_type: int, seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
    b0 = 0x80  # version 2, no padding/extension/csrc
    b1 = payload_type & 0x7F
    header = struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    return header + payload


class SimpleJitterBuffer:
    """
    Jitter buffer minimal untuk arah caller -> AI. Bekerja selalu dengan PCM16
    (bukan payload RTP mentah) -- decode ulaw->PCM16 dilakukan SEBELUM data
    masuk ke sini, supaya jitter buffer tetap codec-agnostic.

    Menyusun ulang paket berdasarkan sequence number RTP dan menahan sebentar
    (kira-kira `jitter_ms`) untuk menunggu paket yang datang telat/out-of-order.
    Gap diisi silence (zero-fill PCM16) supaya timing tetap jalan.
    """

    def __init__(self, jitter_ms: int = 60, sample_rate_hz: int = 16000, samples_per_packet: int = 320):
        self._buf: dict[int, bytes] = {}
        self._next_seq: Optional[int] = None
        packet_ms = samples_per_packet / sample_rate_hz * 1000
        self._window = max(1, int(jitter_ms / packet_ms))
        # PCM16 = 2 byte per sample, SELALU -- terlepas dari codec RTP di
        # kawat (ulaw/slin16), karena payload yang di-push ke buffer ini
        # sudah didekode jadi PCM16 lebih dulu oleh pemanggil.
        self._bytes_per_packet = samples_per_packet * 2

    def push(self, seq: int, pcm16_payload: bytes):
        self._buf[seq] = pcm16_payload
        if self._next_seq is None:
            self._next_seq = seq

    def pop_ready(self) -> list[bytes]:
        """Kembalikan payload-payload PCM16 yang sudah siap diteruskan, berurutan."""
        out: list[bytes] = []
        if self._next_seq is None:
            return out
        while len(self._buf) >= self._window or self._next_seq in self._buf:
            if self._next_seq in self._buf:
                out.append(self._buf.pop(self._next_seq))
            elif self._buf:
                logger.warning("Jitter buffer: paket seq=%d hilang, isi silence", self._next_seq)
                out.append(b"\x00" * self._bytes_per_packet)
            else:
                break
            self._next_seq = (self._next_seq + 1) & 0xFFFF
        return out


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, endpoint: "RtpEndpoint"):
        self.endpoint = endpoint

    def connection_made(self, transport):
        self.endpoint._transport = transport

    def datagram_received(self, data: bytes, addr):
        self.endpoint._on_datagram(data, addr)

    def error_received(self, exc):
        logger.warning("RTP socket error: %s", exc)


class RtpEndpoint:
    """
    Satu instance per call session. Membuka satu UDP port lokal, menunggu paket
    pertama dari Asterisk untuk mempelajari alamat balik (symmetric RTP), lalu
    menyediakan queue PCM masuk dan method kirim PCM keluar.

    Semua data yang lewat pcm_in_queue dan yang diterima send_pcm() SELALU
    PCM16LE mono pada cfg.sample_rate_hz -- encode/decode ke codec RTP asli
    (ulaw atau slin16, tergantung cfg.codec) terjadi di dalam kelas ini,
    transparan buat pemanggil (call_session.py dkk tidak perlu tahu/berubah).
    """

    def __init__(self, cfg: RtpBridgeConfig, port: int):
        self.cfg = cfg
        self.port = port
        self._transport = None
        self._remote_addr = None
        self._is_ulaw = getattr(cfg, "codec", "slin16") == "ulaw"

        # None (bukan 0) sengaja dipakai sebagai sentinel "belum dipelajari
        # dari paket Asterisk". Begitu paket pertama masuk, ini ditimpa dengan
        # payload type ASLI yang dipakai Asterisk (biasanya 0 untuk ulaw,
        # dynamic >=96 untuk slin16) -- kita tidak pernah menebak/hardcode.
        self._payload_type: Optional[int] = None

        self._out_seq = 0
        self._out_ts = 0
        self._ssrc = random.getrandbits(32)

        self.pcm_in_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._closed = False

        self._pending_out: list[bytes] = []
        self._max_pending_bytes = cfg.sample_rate_hz * 2 * 2  # ~2 detik audio PCM16, batas aman
        self._ready_event = asyncio.Event()

        # Melindungi _out_seq/_out_ts dari race condition ketika send_pcm()
        # dipanggil dari beberapa task bersamaan.
        self._send_lock = asyncio.Lock()

        # 20ms per paket pada sample rate yang dikonfigurasi -- generik untuk
        # 16000 Hz (slin16, 320 sample/paket) maupun 8000 Hz (ulaw, 160
        # sample/paket).
        self._samples_per_packet = int(self.cfg.sample_rate_hz * 0.02)

        self._jitter = SimpleJitterBuffer(
            sample_rate_hz=self.cfg.sample_rate_hz,
            samples_per_packet=self._samples_per_packet,
        )

        # DEBUG SEMENTARA -- set True dari call_session.py untuk mengaktifkan
        # echo loopback (lihat catatan di docstring modul ini).
        self._loopback_test = False

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: _RtpProtocol(self),
            local_addr=(self.cfg.listen_host, self.port),
        )
        logger.debug("RTP endpoint listening on %s:%s", self.cfg.listen_host, self.port)

    async def wait_ready(self, timeout: Optional[float] = None):
        """
        Tunggu sampai paket RTP pertama dari Asterisk diterima (remote_addr dan
        payload_type sudah kepelajari). Panggil ini SEBELUM memicu AI mulai
        bicara, supaya audio awal tidak ketahan/hilang.
        """
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def _decode_to_pcm16(self, payload: bytes) -> bytes:
        """Payload RTP mentah (ulaw atau slin16) -> PCM16LE."""
        if self._is_ulaw:
            return audioop.ulaw2lin(payload, 2)
        return payload  # slin16: sudah PCM16 apa adanya

    def _encode_from_pcm16(self, pcm16_chunk: bytes) -> bytes:
        """PCM16LE -> payload RTP yang mau dikirim (ulaw atau slin16 apa adanya)."""
        if self._is_ulaw:
            return audioop.lin2ulaw(pcm16_chunk, 2)
        return pcm16_chunk

    def _on_datagram(self, data: bytes, addr):
        first_packet = self._remote_addr is None
        if first_packet:
            self._remote_addr = addr
            logger.info("RTP endpoint %s learned remote addr %s", self.port, addr)

        parsed = _parse_rtp(data)
        if not parsed:
            return
        payload_type, seq, ts, ssrc, raw_payload = parsed

        logger.debug(
            "RTP IN: pt=%d seq=%d ts=%d payload_len=%d first16=%s",
            payload_type, seq, ts, len(raw_payload), raw_payload[:16].hex(" "),
        )

        # Payload type dipelajari dari paket ASLI Asterisk, dipakai lagi apa
        # adanya saat kirim balik -- konsisten dengan perilaku lama.
        self._payload_type = payload_type

        try:
            pcm16_payload = self._decode_to_pcm16(raw_payload)
        except audioop.error:
            logger.warning(
                "RTP IN: gagal decode payload (len=%d) sebagai %s, paket dibuang",
                len(raw_payload), "ulaw" if self._is_ulaw else "slin16",
            )
            return

        # --- LOOPBACK TEST SEMENTARA, hapus setelah selesai debug ---
        # Echo balik lewat send_pcm() (yang akan meng-encode ulang ke codec
        # RTP yang benar), supaya tes ini tetap valid untuk codec ulaw juga.
        if self._loopback_test:
            asyncio.create_task(self.send_pcm(pcm16_payload))
        # --- end loopback test ---

        if first_packet and not self._ready_event.is_set():
            self._ready_event.set()
            if self._pending_out:
                pending = b"".join(self._pending_out)
                self._pending_out.clear()
                logger.info(
                    "RTP endpoint %s siap, flush %d bytes audio yang tadinya ketahan",
                    self.port, len(pending),
                )
                asyncio.create_task(self.send_pcm(pending))

        self._jitter.push(seq, pcm16_payload)
        for ready_payload in self._jitter.pop_ready():
            if self.pcm_in_queue.full():
                try:
                    self.pcm_in_queue.get_nowait()  # buang paket terlama kalau konsumer telat
                except asyncio.QueueEmpty:
                    pass
            try:
                self.pcm_in_queue.put_nowait(ready_payload)
            except asyncio.QueueFull:
                pass

    async def send_pcm(self, pcm_bytes: bytes, samples_per_packet: Optional[int] = None):
        """
        Pecah PCM16 menjadi paket RTP ~20ms dan kirim secara real-time,
        meng-encode ke codec RTP yang dipakai (ulaw/slin16) sebelum dikirim.

        pcm_bytes HARUS PCM16LE (2 byte/sample) pada cfg.sample_rate_hz --
        sama seperti sebelumnya, terlepas dari codec RTP di kawat.

        Dikunci dengan self._send_lock supaya kalau method ini dipanggil dari
        beberapa task bersamaan, cuma satu pemanggil yang menulis ke socket &
        memutakhirkan _out_seq/_out_ts dalam satu waktu.
        """
        if samples_per_packet is None:
            samples_per_packet = self._samples_per_packet

        if (
            self._remote_addr is None
            or self._payload_type is None
            or self._transport is None
        ):
            self._pending_out.append(pcm_bytes)
            total = sum(len(c) for c in self._pending_out)
            while total > self._max_pending_bytes and self._pending_out:
                dropped = self._pending_out.pop(0)
                total -= len(dropped)
            logger.warning(
                "send_pcm: %d bytes DITAHAN (belum siap kirim, "
                "remote_addr=%s, payload_type=%s, transport=%s) port=%s -- "
                "nunggu paket pertama dari Asterisk",
                len(pcm_bytes), self._remote_addr, self._payload_type,
                self._transport is not None, self.port,
            )
            return

        bytes_per_packet = samples_per_packet * 2  # PCM16 input, selalu 2 byte/sample
        packet_interval = samples_per_packet / self.cfg.sample_rate_hz

        packets_sent = 0

        async with self._send_lock:
            for i in range(0, len(pcm_bytes), bytes_per_packet):
                chunk = pcm_bytes[i:i + bytes_per_packet]

                if not chunk:
                    continue

                # Paket terakhir bisa lebih pendek dari bytes_per_packet kalau
                # panjang input bukan kelipatan genap -- pad dengan silence
                # PCM16 supaya audioop.lin2ulaw tidak error (butuh genap byte)
                # dan supaya durasi paket tetap konsisten di sisi Asterisk.
                if len(chunk) < bytes_per_packet:
                    chunk = chunk + b"\x00" * (bytes_per_packet - len(chunk))

                try:
                    out_payload = self._encode_from_pcm16(chunk)
                except audioop.error:
                    logger.warning("send_pcm: gagal encode chunk, dilewati")
                    continue

                packet = _build_rtp(
                    self._payload_type,
                    self._out_seq,
                    self._out_ts,
                    self._ssrc,
                    out_payload,
                )

                self._transport.sendto(packet, self._remote_addr)

                self._out_seq += 1
                self._out_ts += samples_per_packet
                packets_sent += 1

                # Pace RTP secara real-time (20ms per paket).
                await asyncio.sleep(packet_interval)

        logger.info(
            "send_pcm: %d paket terkirim ke %s (port lokal=%s, codec=%s)",
            packets_sent, self._remote_addr, self.port,
            "ulaw" if self._is_ulaw else "slin16",
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._transport:
            self._transport.close()


class RtpPortPool:
    """Alokator port UDP sederhana dari range konfigurasi, dipakai satu per call."""

    def __init__(self, cfg: RtpBridgeConfig):
        self.cfg = cfg
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    async def acquire(self) -> int:
        async with self._lock:
            for port in range(self.cfg.listen_port_start, self.cfg.listen_port_end, 2):
                if port not in self._in_use:
                    self._in_use.add(port)
                    return port
        raise RuntimeError("RTP port pool exhausted — naikkan RTP_LISTEN_PORT_END atau kurangi concurrency")

    async def release(self, port: int):
        async with self._lock:
            self._in_use.discard(port)
