"""
Helper resampling PCM16 mono. Dibutuhkan karena kontrak Gemini Live API:
- audio masuk ke Gemini: PCM16 @ 16kHz mono (cocok langsung dengan slin16 Asterisk)
- audio keluar dari Gemini: PCM16 @ 24kHz mono (harus di-downsample ke 16kHz
  sebelum dikirim balik ke Asterisk sebagai slin16)

Pakai `audioop` (stdlib, atau `audioop-lts` di Python 3.13+) — cukup untuk
resampling sederhana tanpa dependency berat seperti scipy/soundfile.
"""
import io
import wave

try:
    import audioop
except ImportError:  # Python 3.13+ tanpa stdlib audioop
    import audioop_lts as audioop  # type: ignore


def pcm16_to_wav(pcm: bytes, rate: int) -> bytes:
    """Bungkus PCM16LE mono jadi WAV (untuk upload ke endpoint STT)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_bytes_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Decode WAV -> (PCM16LE mono, sample_rate). Naikkan 8-bit & stereo -> mono."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    pcm = raw
    if width == 1:                      # 8-bit unsigned -> 16-bit signed
        pcm = audioop.bias(pcm, 1, -128)
        pcm = audioop.lin2lin(pcm, 1, 2)
    elif width != 2:
        raise ValueError(f"sample width {width} byte belum didukung")
    if nch == 2:                        # stereo -> mono
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif nch != 1:
        raise ValueError(f"channel {nch} belum didukung")
    return pcm, rate


class Resampler:
    def __init__(self, in_rate: int, out_rate: int, channels: int = 1, sample_width: int = 2):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.channels = channels
        self.sample_width = sample_width
        self._state = None

    def process(self, pcm_bytes: bytes) -> bytes:
        if self.in_rate == self.out_rate:
            return pcm_bytes
        converted, self._state = audioop.ratecv(
            pcm_bytes, self.sample_width, self.channels,
            self.in_rate, self.out_rate, self._state,
        )
        return converted
