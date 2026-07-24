#!/usr/bin/env python3
"""Simulate N voice assistant satellites through the full HA Assist pipeline.

Each virtual satellite:
  1. Opens a WebSocket to HA
  2. Sends assist_pipeline/run (start_stage=stt, end_stage=tts)
  3. Receives binary handler_id from run-start event
  4. Streams pre-recorded WAV as binary PCM chunks (1024 bytes / 32ms)
  5. HA runs: VAD -> STT -> intent -> TTS (your custom pipeline)
  6. Collects per-stage timing from pipeline events

This tests the REAL bottleneck: Silero VAD blocking the event loop.

Usage:
    python simulate_satellites.py \\
        --ha-url ws://localhost:8123 \\
        --token YOUR_LONG_LIVED_TOKEN \\
        --audio audio/vyzovi_lift.wav \\
        --ramp 1,5,10,20,50

Requirements:
    pip install websockets numpy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import struct
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("simulator")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
CHUNK_BYTES = 1024  # 512 samples * 2 bytes = 32ms (matches ESPHome)
CHUNK_INTERVAL = 0.032  # 32ms realtime pacing
SILENCE_TRAILER_SECONDS = 4.0  # trailing silence so VAD detects end-of-command

PIPELINE_TIMEOUT = 120.0  # Max seconds to wait for pipeline completion


@dataclass
class DeviceTimeline:
    """Timeline of pipeline events for one device."""

    device_id: int
    audio_file: str = ""
    audio_duration: float = 0.0

    t_connect: float = 0.0
    t_run_start: float = 0.0
    t_vad_start: float = 0.0
    t_vad_end: float = 0.0
    t_stt_end: float = 0.0
    t_intent_start: float = 0.0
    t_intent_end: float = 0.0
    t_tts_start: float = 0.0
    t_tts_end: float = 0.0
    t_run_end: float = 0.0

    handler_id: int | None = None
    stt_text: str = ""
    intent_speech: str = ""
    error: str | None = None

    @property
    def total_time(self) -> float:
        if self.t_run_end > 0 and self.t_run_start > 0:
            return self.t_run_end - self.t_run_start
        return 0.0

    @property
    def vad_time(self) -> float:
        if self.t_vad_end > 0 and self.t_vad_start > 0:
            return self.t_vad_end - self.t_vad_start
        return 0.0

    @property
    def stt_time(self) -> float:
        if self.t_stt_end > 0 and self.t_vad_end > 0:
            return self.t_stt_end - self.t_vad_end
        return 0.0

    @property
    def intent_time(self) -> float:
        if self.t_intent_end > 0 and self.t_intent_start > 0:
            return self.t_intent_end - self.t_intent_start
        return 0.0

    @property
    def success(self) -> bool:
        return self.error is None and self.t_run_end > 0


def load_wav(path: Path) -> tuple[bytes, float]:
    """Load WAV file, return raw PCM + duration."""
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        duration = wf.getnframes() / wf.getframerate()
    return pcm, duration


def chunk_audio(pcm: bytes, chunk_size: int = CHUNK_BYTES) -> list[bytes]:
    """Split PCM into ESP32-sized chunks."""
    return [pcm[i : i + chunk_size] for i in range(0, len(pcm), chunk_size)]


class VirtualSatellite:
    """One simulated ESP32 voice satellite."""

    def __init__(
        self,
        device_id: int,
        ws_url: str,
        token: str,
        audio_chunks: list[bytes],
        audio_duration: float,
        audio_name: str,
        pipeline: str | None,
        start_delay: float,
        vad_mode: str,
        speech_threshold: float,
    ):
        self.device_id = device_id
        self.ws_url = ws_url
        self.token = token
        self.audio_chunks = audio_chunks
        self.audio_duration = audio_duration
        self.audio_name = audio_name
        self.pipeline = pipeline
        self.start_delay = start_delay
        self.vad_mode = vad_mode
        self.speech_threshold = speech_threshold
        self.timeline = DeviceTimeline(
            device_id=device_id,
            audio_file=audio_name,
            audio_duration=audio_duration,
        )

    async def run(self) -> DeviceTimeline:
        """Run the full pipeline simulation for this device."""
        import websockets

        await asyncio.sleep(self.start_delay)

        try:
            async with websockets.connect(
                f"{self.ws_url}/api/websocket",
                max_size=2**20,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                self.timeline.t_connect = time.monotonic()

                await self._auth(ws)
                await self._run_pipeline(ws)

        except Exception as e:
            self.timeline.error = f"{type(e).__name__}: {e}"
            _LOGGER.error("Device #%d failed: %s", self.device_id, self.timeline.error)

        return self.timeline

    async def _auth(self, ws) -> None:
        """Authenticate with HA."""
        msg = await ws.recv()
        data = json.loads(msg)
        if data.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {data}")

        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))

        msg = await ws.recv()
        data = json.loads(msg)
        if data.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {data}")

    async def _run_pipeline(self, ws) -> None:
        """Send pipeline run command and stream audio."""
        run_msg: dict = {
            "type": "assist_pipeline/run",
            "id": 1,
            "start_stage": "stt",
            "end_stage": "tts",
            "input": {
                "sample_rate": SAMPLE_RATE,
            },
        }
        if self.pipeline:
            run_msg["pipeline"] = self.pipeline

        audio_input = run_msg["input"]
        if self.vad_mode:
            audio_input["vad_mode"] = self.vad_mode
        if self.speech_threshold:
            audio_input["speech_threshold"] = self.speech_threshold

        await ws.send(json.dumps(run_msg))

        await asyncio.gather(
            self._receive_events(ws),
            self._stream_audio(ws),
        )

    async def _stream_audio(self, ws) -> None:
        """Stream audio chunks as binary frames at real-time pace.

        Waits for handler_id to be received first (set by _receive_events).
        """
        while self.timeline.handler_id is None and self.timeline.error is None:
            await asyncio.sleep(0.005)

        if self.timeline.handler_id is None or self.timeline.error is not None:
            return

        prefix = bytes([self.timeline.handler_id])

        for chunk in self.audio_chunks:
            try:
                await ws.send(prefix + chunk)
            except Exception as e:
                if self.timeline.error is None:
                    self.timeline.error = f"Send error: {e}"
                return
            await asyncio.sleep(CHUNK_INTERVAL)

        # Stream trailing silence so VAD detects end-of-command, then signal
        # end-of-stream. The fork's stt_stream generator terminates on an empty
        # chunk; real ESPHome satellites stream continuously (incl. silence).
        silence = bytes(CHUNK_BYTES)
        silence_chunks = int(SILENCE_TRAILER_SECONDS / CHUNK_INTERVAL)
        for _ in range(silence_chunks):
            if self.timeline.error is not None or self.timeline.t_run_end > 0.0:
                break
            try:
                await ws.send(prefix + silence)
            except Exception as e:
                if self.timeline.error is None:
                    self.timeline.error = f"Send error: {e}"
                return
            await asyncio.sleep(CHUNK_INTERVAL)

        # End-of-stream marker (empty data) to finalize STT
        try:
            await ws.send(prefix)
        except Exception:
            pass

    async def _receive_events(self, ws) -> None:
        """Receive and process pipeline events until run-end or error."""
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=PIPELINE_TIMEOUT)
                now = time.monotonic()

                if isinstance(raw, bytes):
                    continue

                data = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "result":
                    if not data.get("success"):
                        self.timeline.error = data.get("error", {}).get(
                            "message", "Pipeline validation failed"
                        )
                        return
                    self.timeline.t_run_start = now
                    continue

                if msg_type == "event":
                    event = data.get("event", {})
                    event_type = event.get("type", "")
                    event_data = event.get("data", {})

                    self._handle_event(event_type, event_data, now)

                    if event_type == "run-end":
                        self.timeline.t_run_end = now
                        return

                    if event_type == "error":
                        self.timeline.error = event_data.get(
                            "message", "Pipeline error"
                        )
                        code = event_data.get("code", "")
                        self.timeline.error = f"{code}: {self.timeline.error}"
                        self.timeline.t_run_end = now
                        return

        except asyncio.TimeoutError:
            if self.timeline.error is None:
                self.timeline.error = f"Timeout after {PIPELINE_TIMEOUT}s"
        except Exception as e:
            if self.timeline.error is None:
                self.timeline.error = f"{type(e).__name__}: {e}"

    def _handle_event(self, event_type: str, data: dict, timestamp: float) -> None:
        """Handle a single pipeline event."""
        if event_type == "run-start":
            runner_data = data.get("runner_data", {})
            self.timeline.handler_id = runner_data.get("stt_binary_handler_id")
            _LOGGER.info(
                "device #%s run-start handler_id=%r runner_data=%s",
                self.device_id, self.timeline.handler_id, runner_data,
            )
            if self.timeline.t_run_start == 0.0:
                self.timeline.t_run_start = timestamp

        elif event_type == "stt-vad-start":
            self.timeline.t_vad_start = timestamp

        elif event_type == "stt-vad-end":
            self.timeline.t_vad_end = timestamp

        elif event_type == "stt-end":
            self.timeline.t_stt_end = timestamp
            stt_output = data.get("stt_output", {})
            self.timeline.stt_text = stt_output.get("text", "")

        elif event_type == "intent-start":
            self.timeline.t_intent_start = timestamp

        elif event_type == "intent-end":
            self.timeline.t_intent_end = timestamp
            intent_output = data.get("intent_output", {})
            self.timeline.intent_speech = intent_output.get("speech", "")

        elif event_type == "tts-start":
            self.timeline.t_tts_start = timestamp

        elif event_type == "tts-end":
            self.timeline.t_tts_end = timestamp


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
    concurrent: int
    timelines: list[DeviceTimeline] = field(default_factory=list)
    wall_time: float = 0.0


async def run_level(
    concurrent: int,
    ws_url: str,
    token: str,
    audio_chunks: list[bytes],
    audio_duration: float,
    audio_name: str,
    pipelines: list[str] | None,
    vad_mode: str,
    speech_threshold: float,
) -> LevelResult:
    """Run one ramp-up level."""
    if pipelines and len(pipelines) > 1:
        _LOGGER.info(
            "Starting level: %d devices across %d pipelines | audio=%s (%.1fs)",
            concurrent,
            len(pipelines),
            audio_name,
            audio_duration,
        )
    else:
        _LOGGER.info(
            "Starting level: %d devices | audio=%s (%.1fs)",
            concurrent,
            audio_name,
            audio_duration,
        )

    satellites = [
        VirtualSatellite(
            device_id=i,
            ws_url=ws_url,
            token=token,
            audio_chunks=audio_chunks,
            audio_duration=audio_duration,
            audio_name=audio_name,
            pipeline=pipelines[i % len(pipelines)] if pipelines else None,
            start_delay=min(i * 0.1, 5.0),  # Stagger starts, max 5s
            vad_mode=vad_mode,
            speech_threshold=speech_threshold,
        )
        for i in range(concurrent)
    ]

    t_wall = time.monotonic()
    timelines = await asyncio.gather(*[sat.run() for sat in satellites])
    wall_time = time.monotonic() - t_wall

    result = LevelResult(concurrent=concurrent, timelines=timelines, wall_time=wall_time)
    print_level_result(result)
    return result


def print_level_result(level: LevelResult) -> None:
    """Print results for one level."""
    timelines = level.timelines
    ok = [t for t in timelines if t.success]
    fail = [t for t in timelines if not t.success]

    print(f"\n{'='*78}")
    print(f"LEVEL: {level.concurrent} devices  |  wall_time={level.wall_time:.1f}s")
    print(f"{'='*78}")

    if not ok:
        print(f"  ALL {len(fail)} DEVICES FAILED!")
        for t in fail[:5]:
            print(f"    device #{t.device_id}: {t.error}")
        return

    # Per-device details (first 10)
    print(f"\n  Per-device ({len(ok)} ok, {len(fail)} failed):")
    for t in sorted(ok, key=lambda x: x.total_time)[:10]:
        parts = []
        if t.t_vad_start > 0 and t.t_vad_end > 0:
            parts.append(f"vad={t.vad_time:.1f}s")
        if t.t_stt_end > 0:
            parts.append(f"stt={t.stt_time:.1f}s")
        if t.t_intent_end > 0 and t.t_intent_start > 0:
            parts.append(f"intent={t.intent_time:.1f}s")
        print(
            f"    #{t.device_id:>3}: total={t.total_time:.1f}s  {' '.join(parts)}"
            f'  stt="{t.stt_text[:30]}"'
        )

    if len(ok) > 10:
        print(f"    ... and {len(ok) - 10} more")

    # Aggregate stats
    total_times = [t.total_time for t in ok]
    vad_times = [t.vad_time for t in ok if t.vad_time > 0]
    stt_times = [t.stt_time for t in ok if t.stt_time > 0]

    print(f"\n  {'Metric':>20} | {'mean':>8} | {'p50':>8} | {'p95':>8} | {'p99':>8} | {'max':>8}")
    print(f"  {'-'*20}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}")

    def _row(name: str, data: list[float]):
        if not data:
            print(f"  {name:>20} | {'n/a':>8} |")
            return
        print(
            f"  {name:>20} | "
            f"{statistics.mean(data):>7.2f}s | "
            f"{_percentile(data, 50):>7.2f}s | "
            f"{_percentile(data, 95):>7.2f}s | "
            f"{_percentile(data, 99):>7.2f}s | "
            f"{max(data):>7.2f}s"
        )

    _row("total (end-to-end)", total_times)
    if vad_times:
        _row("VAD duration", vad_times)
    if stt_times:
        _row("STT duration", stt_times)

    # Bottleneck analysis
    print(f"\n  Bottleneck analysis:")
    if vad_times and stt_times:
        mean_vad = statistics.mean(vad_times)
        mean_stt = statistics.mean(stt_times)
        if mean_vad > mean_stt * 2:
            print(f"    -> VAD ({mean_vad:.1f}s) >> STT ({mean_stt:.1f}s)")
            print(f"    -> VAD is the bottleneck (event loop blocked)")
        elif mean_stt > mean_vad * 2:
            print(f"    -> STT ({mean_stt:.1f}s) >> VAD ({mean_vad:.1f}s)")
            print(f"    -> STT server is the bottleneck")
        else:
            print(f"    -> VAD ({mean_vad:.1f}s) ~ STT ({mean_stt:.1f}s)")
            print(f"    -> Both contribute equally")

    # STT text samples
    texts = [t.stt_text for t in ok if t.stt_text]
    if texts:
        print(f"\n  STT results ({len(texts)} recognized):")
        from collections import Counter
        text_counts = Counter(texts)
        for text, count in text_counts.most_common(5):
            print(f"    [{count}x] \"{text[:50]}\"")

    if fail:
        print(f"\n  FAILURES ({len(fail)}):")
        for t in fail[:5]:
            print(f"    device #{t.device_id}: {t.error}")


def print_summary(all_results: list[LevelResult]) -> None:
    """Print final summary across all levels."""
    print(f"\n{'='*78}")
    print("FULL PIPELINE SIMULATION SUMMARY")
    print(f"{'='*78}")

    print(f"\n  {'Devices':>8} | {'p50':>8} | {'p95':>8} | {'p99':>8} | {'max':>8} | {'VAD p50':>8} | {'STT p50':>8} | {'OK':>4}")
    print(f"  {'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*4}")

    for level in all_results:
        ok = [t for t in level.timelines if t.success]
        if not ok:
            print(f"  {level.concurrent:>8} | {'FAIL':>8} |")
            continue

        totals = [t.total_time for t in ok]
        vads = [t.vad_time for t in ok if t.vad_time > 0]
        stts = [t.stt_time for t in ok if t.stt_time > 0]

        print(
            f"  {level.concurrent:>8} | "
            f"{_percentile(totals, 50):>7.2f}s | "
            f"{_percentile(totals, 95):>7.2f}s | "
            f"{_percentile(totals, 99):>7.2f}s | "
            f"{max(totals):>7.2f}s | "
            f"{_percentile(vads, 50) if vads else 0:>7.2f}s | "
            f"{_percentile(stts, 50) if stts else 0:>7.2f}s | "
            f"{len(ok):>4}"
        )

    print(f"\n{'='*78}")
    print("DIAGNOSTIC GUIDE")
    print(f"{'='*78}")
    print("""
VAD duration growing with more devices?
  -> Silero VAD inference blocks the asyncio event loop.
  -> Each device needs ~31 chunks/sec, each chunk blocks for inference time.
  -> FIX: Move process_chunk() to run_in_executor, or use ONNX/optimized VAD.

STT duration growing with more devices?
  -> Wyoming STT server thread pool is saturated.
  -> Default pool: min(32, cpu_count+4) workers.
  -> FIX: Run multiple wyoming-onnxasr instances behind a load balancer.

Total time >> audio_duration + 2s (silence_seconds)?
  -> Audio is queuing in the pipeline (processing slower than realtime).
  -> Compare with bench_vad.py max_devices to confirm VAD is the cause.

STT text is wrong/garbled?
  -> Audio quality issue, not performance. Check noise_suppression / auto_gain.
  -> Or the model (GigaAM RNN-T) struggles with your audio.
""")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate N voice satellites through HA Assist pipeline"
    )
    parser.add_argument(
        "--ha-url",
        default="ws://localhost:8123",
        help="HA WebSocket URL (ws:// or wss://)",
    )
    parser.add_argument("--token", required=True, help="HA Long-Lived Access Token")
    parser.add_argument("--audio", required=True, help="Path to WAV file")
    parser.add_argument(
        "--ramp",
        default="1,5,10,20,50",
        help="Comma-separated device counts for ramp-up",
    )
    parser.add_argument(
        "--pipelines",
        default=None,
        help="Comma-separated pipeline IDs (round-robin distribution, default=preferred)",
    )
    parser.add_argument(
        "--num-pipelines",
        type=int,
        default=None,
        help="Auto-distribute across N pipelines (first N unique STT engines)",
    )
    parser.add_argument(
        "--vad-mode",
        default="",
        choices=["", "singleton", "per_pipeline"],
        help="VAD mode override (default: use pipeline default)",
    )
    parser.add_argument(
        "--speech-threshold",
        type=float,
        default=0.0,
        help="Speech threshold override (0 = use default)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=10.0,
        help="Seconds to wait between ramp-up levels",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        _LOGGER.error("Audio file not found: %s", audio_path)
        return

    pcm, audio_duration = load_wav(audio_path)
    audio_chunks = chunk_audio(pcm)
    _LOGGER.info(
        "Loaded %s: %.1fs, %d chunks (%d bytes/chunk)",
        audio_path.name,
        audio_duration,
        len(audio_chunks),
        CHUNK_BYTES,
    )

    levels = [int(x.strip()) for x in args.ramp.split(",")]

    # Parse pipelines
    pipelines: list[str] | None = None
    if args.pipelines:
        pipelines = [p.strip() for p in args.pipelines.split(",")]
        _LOGGER.info("Using %d pipeline(s): %s", len(pipelines), pipelines)
    elif args.num_pipelines:
        # Auto-discover pipelines from HA
        import urllib.request as ur
        pipelines = []
        _LOGGER.info("Auto-discovering %d pipelines...", args.num_pipelines)
        # We'll get them from the pipeline list via websocket
        async def _get_pipelines(n: int) -> list[str]:
            import websockets
            async with websockets.connect(f"{args.ha_url}/api/websocket") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": args.token}))
                await ws.recv()
                await ws.send(json.dumps({"type": "assist_pipeline/pipeline/list", "id": 1}))
                resp = json.loads(await ws.recv())
                seen_stt = set()
                result = []
                for p in resp.get("result", {}).get("pipelines", []):
                    stt = p.get("stt_engine", "")
                    if stt not in seen_stt:
                        seen_stt.add(stt)
                        result.append(p["id"])
                    if len(result) >= n:
                        break
                return result
        pipelines = await _get_pipelines(args.num_pipelines)
        _LOGGER.info("Discovered %d pipelines: %s", len(pipelines), pipelines)

    all_results: list[LevelResult] = []

    for level in levels:
        result = await run_level(
            concurrent=level,
            ws_url=args.ha_url,
            token=args.token,
            audio_chunks=audio_chunks,
            audio_duration=audio_duration,
            audio_name=audio_path.name,
            pipelines=pipelines,
            vad_mode=args.vad_mode,
            speech_threshold=args.speech_threshold,
        )
        all_results.append(result)

        if level < levels[-1]:
            _LOGGER.info("Cooldown %.0fs...", args.cooldown)
            await asyncio.sleep(args.cooldown)

    print_summary(all_results)


if __name__ == "__main__":
    asyncio.run(main())
