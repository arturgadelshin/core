#!/usr/bin/env python3
"""Benchmark Silero VAD inference time to calculate max concurrent devices.

This script replicates exactly what Home Assistant does on every audio chunk:
loads silero_vad.jit and calls model(tensor, sr_tensor) synchronously.

The critical finding: this inference runs on the asyncio event loop without
run_in_executor, so ALL satellites' VAD processing is serialized.

Run inside the HA container (where torch + silero-vad are installed):

    docker exec ha-test python3 /path/to/bench_vad.py

    # Or from the repo with volume mount:
    docker exec ha-test python3 /workspaces/core/load-test/bench_vad.py

Options:
    --iterations 2000    Number of benchmark iterations (after warmup)
    --warmup 200         Warmup iterations (not counted)
    --threads 1,2,4      torch.set_num_threads values to test
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("bench_vad")

SAMPLE_RATE = 16000
SILERO_SAMPLES_PER_CHUNK = 512  # 32ms at 16kHz
SILERO_BYTES_PER_CHUNK = 1024   # 512 samples * 2 bytes
CHUNKS_PER_SEC = SAMPLE_RATE / SILERO_SAMPLES_PER_CHUNK  # 31.25


def find_silero_model() -> Path:
    """Find silero_vad.jit in the same locations HA checks."""
    candidates = []

    # 1. Same dir as assist_pipeline component (HA custom fork)
    ha_component = Path(
        "/workspaces/core/homeassistant/components/assist_pipeline/silero_vad.jit"
    )
    candidates.append(ha_component)

    # 2. silero_vad pip package
    try:
        import importlib.util

        spec = importlib.util.find_spec("silero_vad")
        if spec and spec.submodule_search_locations:
            for sp in spec.submodule_search_locations:
                candidates.append(Path(sp) / "data" / "silero_vad.jit")
    except Exception:
        pass

    # 3. Local directory
    candidates.append(Path(__file__).parent / "silero_vad.jit")

    # 4. Common cache locations
    candidates.extend([
        Path.home() / ".cache" / "silero" / "silero_vad.jit",
        Path("/root/.cache/silero_vad.jit"),
    ])

    for p in candidates:
        if p.exists():
            _LOGGER.info("Found Silero VAD model: %s", p)
            return p

    searched = "\n  ".join(str(c) for c in candidates)
    _LOGGER.error("Silero VAD model not found. Searched:\n  %s", searched)
    sys.exit(1)


def generate_test_chunks(count: int) -> list[bytes]:
    """Generate test audio chunks: alternating speech-like and silence.

    Speech-like: modulated tone at ~200Hz with envelope (high VAD probability)
    Silence: zeros (low VAD probability)
    """
    chunks = []
    for i in range(count):
        samples = np.zeros(SILERO_SAMPLES_PER_CHUNK, dtype=np.int16)
        if i % 4 != 0:  # 75% speech-like
            t = np.arange(SILERO_SAMPLES_PER_CHUNK) / SAMPLE_RATE
            envelope = np.sin(np.pi * t / 0.032)
            freq = 180 + 40 * np.sin(2 * np.pi * 5 * t)
            signal = 12000 * envelope * np.sin(2 * np.pi * freq * t)
            samples = signal.astype(np.int16)
        chunks.append(samples.tobytes())
    return chunks


def benchmark_model(
    model,
    chunks: list[bytes],
    iterations: int,
    warmup: int,
    label: str,
) -> dict:
    """Run benchmark and return statistics."""
    import torch

    total_chunks = warmup + iterations
    times_us = []

    _LOGGER.info("[%s] Warmup: %d iterations...", label, warmup)
    for i in range(total_chunks):
        chunk = chunks[i % len(chunks)]
        audio_np = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        audio_tensor = torch.from_numpy(audio_np).unsqueeze(0)
        sr_tensor = torch.tensor(SAMPLE_RATE)

        t0 = time.perf_counter()
        with torch.no_grad():
            prob = model(audio_tensor, sr_tensor)
        t1 = time.perf_counter()

        if i >= warmup:
            times_us.append((t1 - t0) * 1_000_000)

    times_ms = [t / 1000 for t in times_us]
    mean_ms = statistics.mean(times_ms)
    median_ms = statistics.median(times_ms)
    stdev_ms = statistics.stdev(times_ms) if len(times_ms) > 1 else 0
    p95_ms = _percentile(times_ms, 95)
    p99_ms = _percentile(times_ms, 99)
    min_ms = min(times_ms)
    max_ms = max(times_ms)

    max_chunks_per_sec = 1000 / mean_ms if mean_ms > 0 else 0
    max_devices = max_chunks_per_sec / CHUNKS_PER_SEC

    return {
        "label": label,
        "mean_ms": mean_ms,
        "median_ms": median_ms,
        "stdev_ms": stdev_ms,
        "p95_ms": p95_ms,
        "p99_ms": p99_ms,
        "min_ms": min_ms,
        "max_ms": max_ms,
        "max_chunks_per_sec": max_chunks_per_sec,
        "max_devices": max_devices,
        "iterations": iterations,
    }


def _percentile(data: list[float], pct: float) -> float:
    """Calculate percentile."""
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def print_results(results: list[dict]) -> None:
    """Print benchmark results table."""
    print()
    print("=" * 78)
    print("SILERO VAD BENCHMARK RESULTS")
    print("=" * 78)

    for r in results:
        print(f"\n--- {r['label']} ({r['iterations']} iterations) ---")
        print(f"  Mean:          {r['mean_ms']:.3f} ms/chunk")
        print(f"  Median:        {r['median_ms']:.3f} ms/chunk")
        print(f"  Stdev:         {r['stdev_ms']:.3f} ms")
        print(f"  Min / Max:     {r['min_ms']:.3f} / {r['max_ms']:.3f} ms")
        print(f"  p95 / p99:     {r['p95_ms']:.3f} / {r['p99_ms']:.3f} ms")
        print()
        max_chunks = r["max_chunks_per_sec"]
        max_dev = r["max_devices"]
        print(f"  Max throughput: {max_chunks:.0f} chunks/sec")
        print(f"  Chunks/sec per device: {CHUNKS_PER_SEC:.1f}")
        print(f"  >>> MAX CONCURRENT DEVICES: {max_dev:.0f} <<<")

        # Interpretation
        print()
        if max_dev < 5:
            print(f"  !! CRITICAL: VAD can only handle ~{max_dev:.0f} devices")
            print(f"     This is your main bottleneck (event loop blocked)")
        elif max_dev < 15:
            print(f"  ! WARNING: VAD can handle ~{max_dev:.0f} devices")
            print(f"     Adding more will cause audio backlog and latency spikes")
        elif max_dev < 50:
            print(f"  OK for ~{max_dev:.0f} devices, but watch for contention")
        else:
            print(f"  Good: VAD can handle ~{max_dev:.0f} devices")

    print()
    print("=" * 78)
    print("INTERPRETATION")
    print("=" * 78)
    print("""
Each device sends ~31.25 chunks/sec (32ms each). Silero VAD processes
each chunk synchronously on the HA asyncio event loop (no run_in_executor).

If max_devices < your satellite count, audio backs up and:
  - 5s of audio takes 50-130s to process (your observed dur=130.7s)
  - trigger_timeout fires incorrectly (uses wall-clock, not audio time)
  - All HA automations slow down (event loop saturated)

FIX: Move process_chunk() to run_in_executor or use ONNX/VAD that releases GIL.
""")
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Silero VAD inference time"
    )
    parser.add_argument(
        "--iterations", type=int, default=2000, help="Benchmark iterations"
    )
    parser.add_argument(
        "--warmup", type=int, default=200, help="Warmup iterations (not counted)"
    )
    parser.add_argument(
        "--threads",
        default="1,2,4",
        help="Comma-separated torch.set_num_threads values to test",
    )
    args = parser.parse_args()

    import torch

    model_path = find_silero_model()
    _LOGGER.info("Loading Silero VAD model from %s", model_path)
    model = torch.jit.load(str(model_path))
    model.eval()
    _LOGGER.info("Model loaded. Generating test audio...")

    chunks = generate_test_chunks(max(args.iterations, 1000))
    _LOGGER.info("Generated %d test chunks", len(chunks))

    thread_values = [int(x.strip()) for x in args.threads.split(",")]
    results = []

    for num_threads in thread_values:
        _LOGGER.info("Setting torch threads = %d", num_threads)
        torch.set_num_threads(num_threads)
        label = f"torch threads={num_threads}"
        r = benchmark_model(model, chunks, args.iterations, args.warmup, label)
        results.append(r)

    print_results(results)


if __name__ == "__main__":
    main()
