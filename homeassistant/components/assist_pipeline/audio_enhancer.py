"""Audio enhancement for Assist."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging

from pyspeex_noise import AudioProcessor

from .const import BYTES_PER_CHUNK, SILERO_BYTES_PER_CHUNK
from .silero_vad_manager import SileroVadPerPipeline, SileroVadStream

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnhancedAudioChunk:
    """Enhanced audio chunk and metadata."""

    audio: bytes
    """Raw PCM audio @ 16Khz with 16-bit mono samples"""

    timestamp_ms: int
    """Timestamp relative to start of audio stream (milliseconds)"""

    speech_probability: float | None
    """Probability that audio chunk contains speech (0-1), None if unknown"""


class AudioEnhancer(ABC):
    """Base class for audio enhancement."""

    def __init__(
        self, auto_gain: int, noise_suppression: int, is_vad_enabled: bool
    ) -> None:
        """Initialize audio enhancer."""
        self.auto_gain = auto_gain
        self.noise_suppression = noise_suppression
        self.is_vad_enabled = is_vad_enabled

    @abstractmethod
    def enhance_chunk(self, audio: bytes, timestamp_ms: int) -> EnhancedAudioChunk:
        """Enhance chunk of PCM audio @ 16Khz with 16-bit mono samples."""


class SileroVadSpeexEnhancer(AudioEnhancer):
    """Audio enhancer using Silero VAD and Speex noise suppression."""

    def __init__(
        self,
        auto_gain: int,
        noise_suppression: int,
        is_vad_enabled: bool,
        silero_vad: SileroVadStream | SileroVadPerPipeline | None = None,
    ) -> None:
        """Initialize audio enhancer."""
        super().__init__(auto_gain, noise_suppression, is_vad_enabled)

        self.audio_processor: AudioProcessor | None = None

        self.noise_suppression = noise_suppression * -15
        self.auto_gain = auto_gain * 300

        if (self.auto_gain != 0) or (self.noise_suppression != 0):
            self.audio_processor = AudioProcessor(
                self.auto_gain, self.noise_suppression
            )

        self._silero_vad = silero_vad
        self._audio_buffer = bytearray()
        self._last_probability: float | None = None
        self._chunk_count = 0
        self._peak_history: list[int] = []
        self._target_peak = 20000

    def enhance_chunk(self, audio: bytes, timestamp_ms: int) -> EnhancedAudioChunk:
        """Enhance 10ms chunk of PCM audio @ 16Khz with 16-bit mono samples."""
        import array as _array

        speech_probability: float | None = self._last_probability

        assert len(audio) == BYTES_PER_CHUNK

        if self.audio_processor is not None:
            audio = self.audio_processor.Process10ms(audio).audio

        if self._silero_vad is not None and self.is_vad_enabled:
            self._audio_buffer.extend(audio)
            self._chunk_count += 1
            if len(self._audio_buffer) >= SILERO_BYTES_PER_CHUNK:
                chunk_32ms = bytes(self._audio_buffer[:SILERO_BYTES_PER_CHUNK])
                self._audio_buffer = self._audio_buffer[SILERO_BYTES_PER_CHUNK:]
                speech_probability = self._silero_vad.process_chunk(chunk_32ms)
                self._last_probability = speech_probability
                if self._chunk_count <= 30 or self._chunk_count % 100 == 0 or speech_probability > 0.1:
                    _LOGGER.warning(
                        "Silero prob=%.4f chunk#%d",
                        speech_probability, self._chunk_count,
                    )

        return EnhancedAudioChunk(
            audio=audio,
            timestamp_ms=timestamp_ms,
            speech_probability=speech_probability,
        )
