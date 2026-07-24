"""Audio enhancement for Assist."""

import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os

from pyspeex_noise import AudioProcessor

from .const import BYTES_PER_CHUNK, SILERO_BYTES_PER_CHUNK
from .silero_vad_manager import SileroVadPerPipeline, SileroVadStream

_LOGGER = logging.getLogger(__name__)

_VAD_EXECUTOR_MAX_WORKERS = int(os.environ.get("SILERO_VAD_THREADS", "4"))


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
    async def enhance_chunk(
        self, audio: bytes, timestamp_ms: int
    ) -> EnhancedAudioChunk:
        """Enhance chunk of PCM audio @ 16Khz with 16-bit mono samples."""


class SileroVadSpeexEnhancer(AudioEnhancer):
    """Audio enhancer using Silero VAD and Speex noise suppression."""

    _vad_executor: ThreadPoolExecutor | None = None

    @classmethod
    def _get_vad_executor(cls) -> ThreadPoolExecutor:
        """Return dedicated VAD thread pool (lazy init)."""
        if cls._vad_executor is None:
            cls._vad_executor = ThreadPoolExecutor(
                max_workers=_VAD_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="silero-vad",
            )
            _LOGGER.info(
                "Silero VAD executor started (%d workers)",
                _VAD_EXECUTOR_MAX_WORKERS,
            )
        return cls._vad_executor

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
        self._speex_leftover = bytearray()
        self._last_probability: float | None = None

    async def enhance_chunk(
        self, audio: bytes, timestamp_ms: int
    ) -> EnhancedAudioChunk:
        """Enhance 10ms chunk of PCM audio @ 16Khz with 16-bit mono samples."""
        speech_probability: float | None = self._last_probability

        assert len(audio) in (BYTES_PER_CHUNK, SILERO_BYTES_PER_CHUNK), f"Unexpected chunk size: {len(audio)}"

        if self.audio_processor is not None:
            if len(audio) == BYTES_PER_CHUNK:
                audio = self.audio_processor.Process10ms(audio).audio
            else:
                self._speex_leftover.extend(audio)
                _result = bytearray()
                while len(self._speex_leftover) >= BYTES_PER_CHUNK:
                    _sub = bytes(self._speex_leftover[:BYTES_PER_CHUNK])
                    del self._speex_leftover[:BYTES_PER_CHUNK]
                    _result.extend(self.audio_processor.Process10ms(_sub).audio)
                audio = bytes(_result)

        if self._silero_vad is not None and self.is_vad_enabled:
            self._audio_buffer.extend(audio)
            if len(self._audio_buffer) >= SILERO_BYTES_PER_CHUNK:
                chunk_32ms = bytes(self._audio_buffer[:SILERO_BYTES_PER_CHUNK])
                self._audio_buffer = self._audio_buffer[SILERO_BYTES_PER_CHUNK:]
                loop = asyncio.get_running_loop()
                speech_probability = await loop.run_in_executor(
                    self._get_vad_executor(),
                    self._silero_vad.process_chunk, chunk_32ms
                )
                self._last_probability = speech_probability

        return EnhancedAudioChunk(
            audio=audio,
            timestamp_ms=timestamp_ms,
            speech_probability=speech_probability,
        )
