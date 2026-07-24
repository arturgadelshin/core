#!/usr/bin/env python3
"""Load test for Wyoming STT server (GigaAM RNN-T).

Opens N concurrent TCP connections to the Wyoming STT server, each sending
a pre-recorded WAV file and measuring recognition latency and RTFx.

This isolates the STT server from the HA pipeline (no VAD, no intent, no TTS).
Use this to determine how many concurrent recognitions the STT server can handle.

Usage:
    python bench_stt.py --host localhost --port 10301 \\
        --audio audio/vyzovi_lift.wav \\
        --ramp 1,5,10,20,50,100,200

Requirements:
    pip install wyoming numpy
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("bench_stt")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
SEND_CHUNK_BYTES = 6400  # 200ms chunks (matching HA's _batch_process buffer)
REALTIME_SEND = True  # Send at real-time pace to simulate streaming devices


@dataclass
class SttResult:
    """Result of a single STT request."""

    device_id: int
    audio_duration: float
    send_time: float
    recognize_time: float
    total_time: float
    text: str
    error: str | None = None
    rtfx: float = 0.0

    @property
    def success(self) -> bool:
        return self.error is None


def load_wav(path: Path) -> tuple[bytes, float]:
    """Load WAV file and return raw PCM + duration."""
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"Expected {SAMPLE_RATE}Hz, got {wf.getframerate()}"
        assert wf.getsampwidth() == SAMPLE_WIDTH, f"Expected {SAMPLE_WIDTH} bytes, got {wf.getsampwidth()}"
        assert wf.getnchannels() == CHANNELS, f"Expected {CHANNELS}ch, got {wf.getnchannels()}"
        pcm = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()
    return pcm, duration


async def single_stt_request(
    device_id: int,
    host: str,
    port: int,
    pcm_data: bytes,
    audio_duration: float,
    language: str,
    start_delay: float,
    send_realtime: bool,
) -> SttResult:
    """Send one audio clip to STT server and measure timing."""
    from wyoming.client import AsyncTcpClient
    from wyoming.asr import Transcribe, Transcript
    from wyoming.audio import AudioStart, AudioStop, AudioChunk

    await asyncio.sleep(start_delay)
    t_start = time.monotonic()

    try:
        async with AsyncTcpClient(host, port) as client:
            t_send_start = time.monotonic()

            await client.write_event(
                Transcribe(language=language).event()
            )
            await client.write_event(
                AudioStart(
                    rate=SAMPLE_RATE,
                    width=SAMPLE_WIDTH,
                    channels=CHANNELS,
                ).event()
            )

            offset = 0
            while offset < len(pcm_data):
                chunk = pcm_data[offset : offset + SEND_CHUNK_BYTES]
                await client.write_event(
                    AudioChunk(
                        rate=SAMPLE_RATE,
                        width=SAMPLE_WIDTH,
                        channels=CHANNELS,
                        audio=chunk,
                    ).event()
                )
                offset += SEND_CHUNK_BYTES

                if send_realtime:
                    chunk_duration = len(chunk) / (SAMPLE_RATE * SAMPLE_WIDTH)
                    await asyncio.sleep(chunk_duration)

            await client.write_event(AudioStop().event())
            t_send_end = time.monotonic()

            text = ""
            while True:
                event = await client.read_event()
                if event is None:
                    return SttResult(
                        device_id=device_id,
                        audio_duration=audio_duration,
                        send_time=t_send_end - t_send_start,
                        recognize_time=0,
                        total_time=time.monotonic() - t_start,
                        text="",
                        error="Connection lost (no Transcript event)",
                    )

                if Transcript.is_type(event.type):
                    transcript = Transcript.from_event(event)
                    text = transcript.text
                    break

            t_end = time.monotonic()
            total_time = t_end - t_start
            recognize_time = t_end - t_send_end
            send_time = t_send_end - t_send_start
            rtfx = audio_duration / recognize_time if recognize_time > 0 else 0

            return SttResult(
                device_id=device_id,
                audio_duration=audio_duration,
                send_time=send_time,
                recognize_time=recognize_time,
                total_time=total_time,
                text=text.strip(),
                rtfx=rtfx,
            )

    except Exception as e:
        return SttResult(
            device_id=device_id,
            audio_duration=audio_duration,
            send_time=0,
            recognize_time=0,
            total_time=time.monotonic() - t_start,
            text="",
            error=str(e),
        )


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


@dataclass
class LevelResult:
    """Results for one ramp-up level."""

    concurrent: int
    results: list[SttResult] = field(default_factory=list)
    wall_time: float = 0.0


async def run_level(
    concurrent: int,
    host: str,
    port: int,
    pcm_data: bytes,
    audio_duration: float,
    language: str,
    send_realtime: bool,
) -> LevelResult:
    """Run one ramp-up level with N concurrent requests."""
    _LOGGER.info(
        "Level: %d concurrent | audio=%.1fs | %s",
        concurrent,
        audio_duration,
        "realtime" if send_realtime else "burst",
    )

    tasks = []
    t_wall_start = time.monotonic()

    for i in range(concurrent):
        stagger = (i * 0.05) if concurrent > 1 else 0  # 50ms stagger to avoid SYN flood
        tasks.append(
            single_stt_request(
                device_id=i,
                host=host,
                port=port,
                pcm_data=pcm_data,
                audio_duration=audio_duration,
                language=language,
                start_delay=stagger,
                send_realtime=send_realtime,
            )
        )

    results = await asyncio.gather(*tasks)
    wall_time = time.monotonic() - t_wall_start

    level_result = LevelResult(concurrent=concurrent, results=results, wall_time=wall_time)
    print_level_result(level_result, audio_duration)
    return level_result


def print_level_result(level: LevelResult, audio_duration: float) -> None:
    """Print results for one level."""
    results = level.results
    ok = [r for r in results if r.success]
    fail = [r for r in results if not r.success]

    if ok:
        recognize_times = [r.recognize_time for r in ok]
        total_times = [r.total_time for r in ok]
        rtfxs = [r.rtfx for r in ok]

        print(f"\n  {'Concurrent':>10} | {'RTFx':>6} | {'p50':>6} | {'p95':>6} | {'p99':>6} | {'Errors':>6} | {'Wall':>6}")
        print(f"  {'-'*10}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}")

        print(
            f"  {level.concurrent:>10} | "
            f"{statistics.mean(rtfxs):>6.1f} | "
            f"{_percentile(total_times, 50):>6.2f}s | "
            f"{_percentile(total_times, 95):>6.2f}s | "
            f"{_percentile(total_times, 99):>6.2f}s | "
            f"{len(fail):>6} | "
            f"{level.wall_time:>6.1f}s"
        )

        if level.concurrent <= 10 or len(fail) > 0:
            print(f"  {'':>10}   Recognize time: mean={statistics.mean(recognize_times):.2f}s, "
                  f"min={min(recognize_times):.2f}s, max={max(recognize_times):.2f}s")

        for r in ok[:5]:
            print(f"    device #{r.device_id}: {r.total_time:.2f}s  RTFx={r.rtfx:.1f}  text=\"{r.text[:40]}\"")
        if len(ok) > 5:
            print(f"    ... and {len(ok) - 5} more")

    if fail:
        print(f"  FAILURES ({len(fail)}):")
        for r in fail[:5]:
            print(f"    device #{r.device_id}: {r.error}")

    # Degradation warnings
    if ok:
        mean_rtfx = statistics.mean(rtfxs)
        if mean_rtfx < 3.0:
            print(f"  !! WARNING: RTFx={mean_rtfx:.1f}x is low — STT server is near capacity")
        if level.wall_time > audio_duration * level.concurrent / 3:
            print(f"  !! NOTE: Wall time suggests serialization/queueing in STT server")


def print_summary(all_results: list[LevelResult]) -> None:
    """Print final summary table."""
    print()
    print("=" * 78)
    print("STT LOAD TEST SUMMARY")
    print("=" * 78)
    print(f"\n  {'Concurrent':>10} | {'RTFx avg':>8} | {'p50 total':>9} | {'p95 total':>9} | {'p99 total':>9} | {'Errors':>6}")
    print(f"  {'-'*10}-+-{'-'*8}-+-{'-'*9}-+-{'-'*9}-+-{'-'*9}-+-{'-'*6}")

    for level in all_results:
        ok = [r for r in level.results if r.success]
        fail = [r for r in level.results if not r.success]

        if ok:
            rtfxs = [r.rtfx for r in ok]
            totals = [r.total_time for r in ok]
            print(
                f"  {level.concurrent:>10} | "
                f"{statistics.mean(rtfxs):>8.1f} | "
                f"{_percentile(totals, 50):>8.2f}s | "
                f"{_percentile(totals, 95):>8.2f}s | "
                f"{_percentile(totals, 99):>8.2f}s | "
                f"{len(fail):>6}"
            )
        else:
            print(f"  {level.concurrent:>10} | {'N/A':>8} | {'N/A':>9} | {'N/A':>9} | {'N/A':>9} | {len(fail):>6}")

    print()
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    print("""
RTFx (real-time factor) = audio_duration / recognize_time
  RTFx > 10: Excellent — server can handle 10+ concurrent requests
  RTFx 3-10: OK — moderate concurrency
  RTFx < 3:  Bottleneck — requests are queuing, add STT instances

The STT server uses run_in_executor() with a default thread pool
(min(32, cpu_count+4) workers). Beyond that, requests queue.

This test does NOT include VAD overhead — that's measured separately
by bench_vad.py. If VAD is your bottleneck, fixing STT won't help.
""")
    print("=" * 78)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Wyoming STT load test")
    parser.add_argument("--host", default="localhost", help="STT server host")
    parser.add_argument("--port", type=int, default=10301, help="STT server port")
    parser.add_argument("--audio", required=True, help="Path to WAV file to send")
    parser.add_argument(
        "--ramp",
        default="1,5,10,20,50",
        help="Comma-separated concurrent request counts",
    )
    parser.add_argument("--language", default="ru", help="Language code")
    parser.add_argument(
        "--burst",
        action="store_true",
        help="Send audio as fast as possible (no realtime pacing)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Repeat each level N times (for stability)",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        _LOGGER.error("Audio file not found: %s", audio_path)
        return

    pcm_data, audio_duration = load_wav(audio_path)
    _LOGGER.info(
        "Loaded %s: %.1fs, %d bytes (%d KB)",
        audio_path.name,
        audio_duration,
        len(pcm_data),
        len(pcm_data) // 1024,
    )

    levels = [int(x.strip()) for x in args.ramp.split(",")]
    send_realtime = not args.burst

    all_results: list[LevelResult] = []

    for level in levels:
        for rep in range(args.repeat):
            if args.repeat > 1:
                _LOGGER.info("Repeat %d/%d", rep + 1, args.repeat)
            result = await run_level(
                concurrent=level,
                host=args.host,
                port=args.port,
                pcm_data=pcm_data,
                audio_duration=audio_duration,
                language=args.language,
                send_realtime=send_realtime,
            )
            all_results.append(result)

            if level < levels[-1]:
                cooldown = max(2.0, audio_duration)
                _LOGGER.info("Cooldown %.0fs before next level...", cooldown)
                await asyncio.sleep(cooldown)

    print_summary(all_results)


if __name__ == "__main__":
    asyncio.run(main())
