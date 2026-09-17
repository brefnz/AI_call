"""
Helper resampling PCM16 mono. Dibutuhkan karena kontrak Gemini Live API:
- audio masuk ke Gemini: PCM16 @ 16kHz mono (cocok langsung dengan slin16 Asterisk)
- audio keluar dari Gemini: PCM16 @ 24kHz mono (harus di-downsample ke 16kHz
  sebelum dikirim balik ke Asterisk sebagai slin16)

Pakai `audioop` (stdlib, atau `audioop-lts` di Python 3.13+) — cukup untuk
resampling sederhana tanpa dependency berat seperti scipy/soundfile.
"""
try:
    import audioop
except ImportError:  # Python 3.13+ tanpa stdlib audioop
    import audioop_lts as audioop  # type: ignore


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
