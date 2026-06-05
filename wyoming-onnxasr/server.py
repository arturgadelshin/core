import argparse
import asyncio
import logging
import re
import time
import wave
import io
import numpy as np
from functools import partial
from wyoming.server import AsyncTcpServer, AsyncEventHandler
from wyoming.asr import Transcript, TranscriptStart, TranscriptStop, TranscriptChunk, Transcribe
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.info import Describe, Info, AsrProgram, AsrModel, Attribution
from wyoming.event import Event

import onnx_asr

logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s", datefmt="%H:%M:%S")
_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class GrowingBufferStream:
    def __init__(self, model, partial_interval_ms: int = 2000):
        self._model = model
        self._partial_interval = partial_interval_ms / 1000.0
        self._audio_buffer = bytearray()
        self._last_partial_time = 0.0
        self._start_time = 0.0
        self._prev_text = ""
        self._recognizing = False

    def reset(self):
        self._audio_buffer = bytearray()
        self._last_partial_time = 0.0
        self._start_time = 0.0
        self._prev_text = ""
        self._recognizing = False

    def add_audio(self, pcm_int16: bytes) -> str | None:
        self._audio_buffer.extend(pcm_int16)

        if self._start_time == 0.0:
            self._start_time = time.monotonic()
            self._last_partial_time = self._start_time
            return None

        now = time.monotonic()
        if now - self._last_partial_time < self._partial_interval:
            return None

        if self._recognizing:
            return None

        self._last_partial_time = now
        return self._recognize_buffer()

    def finalize(self) -> str | None:
        if not self._audio_buffer:
            return None

        while self._recognizing:
            time.sleep(0.05)

        text = self._recognize_buffer()
        self._audio_buffer = bytearray()
        return text

    def _recognize_buffer(self) -> str | None:
        if len(self._audio_buffer) < SAMPLE_RATE:
            return None

        self._recognizing = True
        try:
            audio_np = np.frombuffer(bytes(self._audio_buffer), dtype=np.int16).astype(np.float32) / 32768.0
            duration = len(audio_np) / SAMPLE_RATE

            t0 = time.monotonic()
            text = self._model.recognize(audio_np)
            if isinstance(text, list):
                text = " ".join(t.get("text", str(t)) if isinstance(t, dict) else str(t) for t in text)
            text = str(text).strip()
            elapsed = time.monotonic() - t0

            _LOGGER.info(
                "[STREAM] Recognize: %.3fs audio in %.1fms (RTFx=%.1f) → '%s'",
                duration, elapsed * 1000, duration / elapsed if elapsed > 0 else 0, text,
            )

            if not text:
                return None

            if text.startswith(self._prev_text):
                new_text = text[len(self._prev_text):].strip()
            else:
                new_text = text
            self._prev_text = text

            if not new_text:
                return None

            return new_text
        finally:
            self._recognizing = False


class OnnxAsrEventHandler(AsyncEventHandler):
    def __init__(self, reader, writer, model, model_name: str, partial_interval_ms: int = 0):
        super().__init__(reader, writer)
        self._model = model
        self._model_name = model_name
        self._partial_interval_ms = partial_interval_ms
        self._is_rnnt = hasattr(model.asr, "_create_state") if hasattr(model, "asr") else False
        self._streamer: GrowingBufferStream | None = None
        self._audio_buffer = bytearray()
        self._audio_start_time = 0.0
        self._chunk_count = 0
        self._audio_size = 0
        self._partial_count = 0
        self._recognize_task: asyncio.Task | None = None
        self._audio_done = asyncio.Event()

    async def _recognize_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while not self._audio_done.is_set():
            await asyncio.sleep(self._streamer._partial_interval)
            if self._streamer is None or self._streamer._recognizing:
                continue
            if not self._streamer._audio_buffer:
                continue
            self._partial_count += 1
            partial = await loop.run_in_executor(None, self._streamer._recognize_buffer)
            if partial is not None:
                full_text = self._streamer._prev_text
                try:
                    await self.write_event(TranscriptChunk(text=full_text).event())
                except (ConnectionResetError, BrokenPipeError):
                    break
                _LOGGER.info("[STREAM] Partial #%d: %s", self._partial_count, partial.strip())

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(
                asr=[
                    AsrProgram(
                        name="onnx-asr",
                        attribution=Attribution(name="onnx-asr", url="https://github.com/istupakov/onnx-asr"),
                        installed=True,
                        description="ONNX ASR (streaming capable)",
                        version="0.12.0",
                        models=[
                            AsrModel(
                                name=self._model_name,
                                attribution=Attribution(name="onnx-asr", url="https://github.com/istupakov/onnx-asr"),
                                installed=True,
                                description=self._model_name,
                                version=None,
                                languages=["ru"],
                            )
                        ],
                    )
                ],
            )
            await self.write_event(info.event())
            return True

        if Transcribe.is_type(event.type):
            self._audio_buffer = bytearray()
            if self._streamer is not None:
                self._streamer.reset()
            return True

        if AudioStart.is_type(event.type):
            self._audio_start_time = time.monotonic()
            self._chunk_count = 0
            self._audio_size = 0
            self._partial_count = 0
            self._audio_buffer = bytearray()
            self._audio_done.clear()
            if self._partial_interval_ms > 0:
                self._streamer = GrowingBufferStream(
                    model=self._model,
                    partial_interval_ms=self._partial_interval_ms,
                )
                _LOGGER.info("[STREAM] Streaming mode: growing buffer, partial every %dms", self._partial_interval_ms)
                self._recognize_task = asyncio.ensure_future(self._recognize_loop())
            else:
                self._streamer = None
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self._chunk_count += 1
            self._audio_size += len(chunk.audio)

            if self._streamer is not None:
                self._streamer._audio_buffer.extend(chunk.audio)
                if self._streamer._start_time == 0.0:
                    self._streamer._start_time = time.monotonic()
                    self._streamer._last_partial_time = time.monotonic()
            else:
                self._audio_buffer.extend(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            audio_elapsed = time.monotonic() - self._audio_start_time
            _LOGGER.info("[TIMING] Audio received: %.2fs, %d chunks, %d bytes", audio_elapsed, self._chunk_count, self._audio_size)

            if self._streamer is not None:
                self._audio_done.set()
                if self._recognize_task is not None:
                    self._recognize_task.cancel()
                    try:
                        await self._recognize_task
                    except asyncio.CancelledError:
                        pass
                    self._recognize_task = None

                while self._streamer._recognizing:
                    await asyncio.sleep(0.05)

                final_text = self._streamer._prev_text

                partial = await asyncio.get_event_loop().run_in_executor(
                    None, self._streamer.finalize
                )
                if partial is not None and partial.strip():
                    self._partial_count += 1
                    await self.write_event(TranscriptChunk(text=partial).event())
                    _LOGGER.info("[STREAM] Final partial #%d: %s", self._partial_count, partial.strip())
                    final_text = self._streamer._prev_text

                elapsed = time.monotonic() - self._audio_start_time
                _LOGGER.info("[STREAM] Done: %.1fs total | Partials: %d | Final: %s", elapsed, self._partial_count, final_text.strip())
                await self.write_event(Transcript(text=final_text).event())
                self._streamer.reset()
            else:
                await self._batch_recognize()
            return True

        if Transcript.is_type(event.type) or TranscriptStart.is_type(event.type) or TranscriptStop.is_type(event.type):
            return True

        return True

    async def _batch_recognize(self) -> None:
        if not self._audio_buffer:
            await self.write_event(Transcript(text="").event())
            return

        t0 = time.monotonic()
        audio_bytes = bytes(self._audio_buffer)
        try:
            wf = wave.open(io.BytesIO(audio_bytes), "rb")
            frames = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
            sw = wf.getsampwidth()
            ch = wf.getnchannels()
            wf.close()
            dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sw, np.int16)
            audio_np = np.frombuffer(frames, dtype=dtype).astype(np.float32) / 32768.0
            duration = len(audio_np) / sr
            _LOGGER.info("[TIMING] Audio parsed: %.3fs audio (%dHz, %dch, %dbytes), parse took %.1fms", duration, sr, ch, len(audio_bytes), (time.monotonic() - t0) * 1000)
        except Exception:
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            duration = len(audio_np) / SAMPLE_RATE
            _LOGGER.info("[TIMING] Audio fallback: %.3fs audio, parse took %.1fms", duration, (time.monotonic() - t0) * 1000)

        t1 = time.monotonic()
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._sync_recognize, audio_np)
        t2 = time.monotonic()
        _LOGGER.info("[TIMING] Recognition: %.3fs for %.3fs audio (RTFx=%.1f)", t2 - t1, duration, duration / (t2 - t1) if t2 > t1 else 0)

        t3 = time.monotonic()
        await self.write_event(Transcript(text=str(text)).event())
        _LOGGER.info("[TIMING] Send response: %.1fms | Result: %s", (time.monotonic() - t3) * 1000, text)

    def _sync_recognize(self, audio_np: np.ndarray) -> str:
        text = self._model.recognize(audio_np)
        if isinstance(text, list):
            text = " ".join(t.get("text", str(t)) if isinstance(t, dict) else str(t) for t in text)
        return str(text)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--quantization", default=None)
    parser.add_argument("--partial-interval-ms", type=int, default=0, help="Partial result interval in ms (0=disabled)")
    parser.add_argument("--chunk-size-ms", type=int, default=0, help="Alias for --partial-interval-ms")
    args = parser.parse_args()

    interval = args.partial_interval_ms or args.chunk_size_ms

    parts = args.uri.replace("tcp://", "").split(":")
    host = parts[0] if parts else "0.0.0.0"
    port = int(parts[1]) if len(parts) > 1 else 10301

    _LOGGER.info("Loading model: %s ...", args.model)
    model = onnx_asr.load_model(args.model, quantization=args.quantization)
    _LOGGER.info("Model loaded. Starting server on %s:%s (partial_interval=%dms)", host, port, interval)

    server = AsyncTcpServer(host, port)
    await server.run(
        partial(OnnxAsrEventHandler, model=model, model_name=args.model, partial_interval_ms=interval)
    )


if __name__ == "__main__":
    asyncio.run(main())
