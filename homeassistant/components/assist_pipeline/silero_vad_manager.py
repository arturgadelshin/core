"""Silero VAD management for Assist pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import torch

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _find_jit_model_path() -> Path:
    """Find Silero VAD JIT model file."""
    candidates = [
        Path(__file__).parent / "silero_vad.jit",
    ]
    try:
        import importlib.util
        spec = importlib.util.find_spec("silero_vad")
        if spec and spec.submodule_search_locations:
            for sp in spec.submodule_search_locations:
                candidates.append(Path(sp) / "data" / "silero_vad.jit")
    except Exception:
        pass
    for p in candidates:
        if p.exists():
            _LOGGER.debug("Found Silero VAD JIT model: %s", p)
            return p
    raise FileNotFoundError(
        f"Silero VAD JIT model not found. Searched: {candidates}"
    )


class SileroVadStream:
    """Per-stream Silero VAD state using a shared torch model."""

    def __init__(self, model: torch.jit.ScriptModule) -> None:
        self._model = model

    def process_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> float:
        """Process a 32ms PCM audio chunk. Returns speech probability 0-1."""
        audio_np = (
            np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        )
        audio_tensor = torch.from_numpy(audio_np).unsqueeze(0)
        sr_tensor = torch.tensor(sample_rate)
        with torch.no_grad():
            prob = self._model(audio_tensor, sr_tensor)
        return float(prob.item())

    def reset(self) -> None:
        """Reset model state."""
        pass


class SileroVadSingleton:
    """Singleton Silero VAD model shared across all pipelines."""

    def __init__(self, model: torch.jit.ScriptModule) -> None:
        self._model = model

    def create_stream(self) -> SileroVadStream:
        """Create a new per-stream VAD state."""
        return SileroVadStream(self._model)


class SileroVadPerPipeline:
    """Per-pipeline Silero VAD with own torch model."""

    def __init__(self) -> None:
        self._model: torch.jit.ScriptModule | None = None

    def _ensure_model(self) -> torch.jit.ScriptModule:
        if self._model is None:
            model_path = _find_jit_model_path()
            self._model = torch.jit.load(str(model_path))
            self._model.eval()
        return self._model

    def process_chunk(self, audio_bytes: bytes, sample_rate: int = 16000) -> float:
        """Process a 32ms PCM audio chunk. Returns speech probability 0-1."""
        model = self._ensure_model()
        audio_np = (
            np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        )
        audio_tensor = torch.from_numpy(audio_np).unsqueeze(0)
        sr_tensor = torch.tensor(sample_rate)
        with torch.no_grad():
            prob = model(audio_tensor, sr_tensor)
        return float(prob.item())

    def reset(self) -> None:
        """Reset model state."""
        pass


async def async_create_silero_singleton(hass: HomeAssistant) -> SileroVadSingleton:
    """Create singleton Silero VAD manager."""
    model_path = await hass.async_add_executor_job(_find_jit_model_path)
    _LOGGER.debug("Loading Silero VAD model from %s", model_path)
    model = await hass.async_add_executor_job(
        lambda: torch.jit.load(str(model_path))
    )
    model.eval()
    _LOGGER.debug("Silero VAD model loaded (singleton mode, torch JIT)")
    return SileroVadSingleton(model)
