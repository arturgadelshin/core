#!/usr/bin/env python3
"""Generate test WAV files via Piper TTS (Wyoming protocol) or synthetic fallback.

Produces 16kHz / 16-bit / mono PCM WAV files for use by bench_stt.py and
simulate_satellites.py.

Usage:
    # Generate via Piper TTS (default localhost:10201)
    python generate_audio.py --piper-host localhost --piper-port 10201

    # Generate synthetic audio only (no Piper needed)
    python generate_audio.py --synthetic

    # Custom output directory
    python generate_audio.py --output-dir ./audio
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import struct
import sys
import wave
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("generate_audio")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1

DEFAULT_PHRASES = [
    "да",
    "вызови лифт",
    "скажи время",
    "включи свет",
    "выключи свет",
    "расскажи о себе",
]

CHUNKS_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH // 1024  # ~31.25 chunks/sec


async def generate_via_piper(
    host: str, port: int, phrases: list[str], output_dir: Path
) -> list[Path]:
    """Generate WAV files using Piper TTS via Wyoming protocol."""
    try:
        from wyoming.client import AsyncTcpClient
        from wyoming.tts import Synthesize, SynthesizeStart, SynthesizeStop
        from wyoming.audio import AudioChunk, AudioStart, AudioStop
    except ImportError:
        _LOGGER.error("wyoming package not installed. Run: pip install wyoming")
        return []

    results: list[Path] = []

    for phrase in phrases:
        _LOGGER.info("Generating '%s' via Piper TTS (%s:%s)...", phrase, host, port)
        audio_data = bytearray()

        try:
            async with AsyncTcpClient(host, port) as client:
                await client.write_event(
                    Synthesize(text=phrase, voice=None).event()
                )

                while True:
                    event = await client.read_event()
                    if event is None:
                        break
                    if AudioChunk.is_type(event.type):
                        chunk = AudioChunk.from_event(event)
                        audio_data.extend(chunk.audio)
                    elif AudioStop.is_type(event.type):
                        break
        except (OSError, ConnectionRefusedError) as e:
            _LOGGER.error("Cannot connect to Piper TTS at %s:%s: %s", host, port, e)
            _LOGGER.info("Falling back to synthetic for '%s'", phrase)
            continue

        if not audio_data:
            _LOGGER.warning("Piper returned no audio for '%s', using synthetic", phrase)
            continue

        wav_path = output_dir / f"{_safe_name(phrase)}.wav"
        _write_wav(wav_path, bytes(audio_data))
        duration = len(audio_data) / (SAMPLE_RATE * SAMPLE_WIDTH)
        _LOGGER.info("  -> %s (%.1fs, %d bytes)", wav_path.name, duration, len(audio_data))
        results.append(wav_path)

    return results


def generate_synthetic(phrases: list[str], output_dir: Path) -> list[Path]:
    """Generate synthetic WAV files with tone bursts simulating speech.

    Each phrase gets a different tone pattern to produce distinct audio
    that Silero VAD will detect as speech.
    """
    results: list[Path] = []

    for i, phrase in enumerate(phrases):
        _LOGGER.info("Generating synthetic '%s'...", phrase)

        num_syllables = max(2, len(phrase.split()) * 2)
        syllable_duration = 0.15  # 150ms per syllable
        gap_duration = 0.05  # 50ms gap between syllables
        total_duration = num_syllables * syllable_duration + (num_syllables - 1) * gap_duration + 0.5  # +0.5s silence

        samples = []
        base_freq = 150 + i * 30  # Different base frequency per phrase

        for s in range(num_syllables):
            num_samples = int(syllable_duration * SAMPLE_RATE)
            for j in range(num_samples):
                t = j / SAMPLE_RATE
                env = math.sin(math.pi * t / syllable_duration)
                freq = base_freq + 50 * math.sin(2 * math.pi * 3 * t)
                sample = int(16000 * env * math.sin(2 * math.pi * freq * t))
                samples.append(sample)

            if s < num_syllables - 1:
                gap_samples = int(gap_duration * SAMPLE_RATE)
                samples.extend([0] * gap_samples)

        silence_samples = int(0.5 * SAMPLE_RATE)
        samples.extend([0] * silence_samples)

        audio_bytes = struct.pack(f"<{len(samples)}h", *samples)

        wav_path = output_dir / f"{_safe_name(phrase)}.wav"
        _write_wav(wav_path, audio_bytes)
        duration = len(samples) / SAMPLE_RATE
        _LOGGER.info("  -> %s (%.1fs, synthetic)", wav_path.name, duration)
        results.append(wav_path)

    return results


def _safe_name(phrase: str) -> str:
    """Convert phrase to safe filename."""
    return (
        phrase.strip()
        .lower()
        .replace(" ", "_")
        .replace("?", "")
        .replace("!", "")
        .replace(".", "")
    )


def _write_wav(path: Path, pcm_data: bytes) -> None:
    """Write raw PCM to a WAV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate test WAV files")
    parser.add_argument("--piper-host", default="localhost", help="Piper TTS host")
    parser.add_argument("--piper-port", type=int, default=10201, help="Piper TTS port")
    parser.add_argument(
        "--output-dir", default="./audio", help="Output directory for WAV files"
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic audio (no Piper needed)",
    )
    parser.add_argument(
        "--phrases",
        nargs="+",
        default=DEFAULT_PHRASES,
        help="Custom phrases to generate",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_files: list[Path] = []

    if not args.synthetic:
        piper_files = await generate_via_piper(
            args.piper_host, args.piper_port, args.phrases, output_dir
        )
        all_files.extend(piper_files)

        missing = set(_safe_name(p) for p in args.phrases) - set(
            _safe_name(f.stem) for f in all_files
        )
        if missing:
            missing_phrases = [p for p in args.phrases if _safe_name(p) in missing]
            _LOGGER.info("Generating synthetic fallback for: %s", missing_phrases)
            synth_files = generate_synthetic(missing_phrases, output_dir)
            all_files.extend(synth_files)
    else:
        all_files = generate_synthetic(args.phrases, output_dir)

    _LOGGER.info("=" * 60)
    _LOGGER.info("Generated %d WAV files in %s:", len(all_files), output_dir)
    for f in sorted(all_files):
        with wave.open(str(f), "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
            _LOGGER.info("  %s (%.1fs)", f.name, duration)
    _LOGGER.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
