import argparse
import asyncio
import logging
import time
import wave
import io
import numpy as np
from functools import partial
from wyoming.server import AsyncTcpServer, AsyncEventHandler
from wyoming.asr import Transcript, TranscriptStart, TranscriptStop, Transcribe
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.info import Describe, Info, AsrProgram, AsrModel, Attribution
from wyoming.event import Event

import onnx_asr

logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s", datefmt="%H:%M:%S")
_LOGGER = logging.getLogger(__name__)


class OnnxAsrEventHandler(AsyncEventHandler):
    def __init__(self, reader, writer, model, model_name: str):
        super().__init__(reader, writer)
        self._model = model
        self._model_name = model_name
        self._audio_buffer = bytearray()

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(
                asr=[
                    AsrProgram(
                        name="onnx-asr",
                        attribution=Attribution(name="onnx-asr", url="https://github.com/istupakov/onnx-asr"),
                        installed=True,
                        description="ONNX ASR",
                        version="0.11.0",
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
            return True

        if AudioStart.is_type(event.type):
            self._audio_start_time = time.monotonic()
            self._chunk_count = 0
            self._audio_size = 0
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self._audio_buffer.extend(chunk.audio)
            self._chunk_count += 1
            self._audio_size += len(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            audio_elapsed = time.monotonic() - self._audio_start_time
            _LOGGER.info("[TIMING] Audio received: %.2fs, %d chunks, %d bytes", audio_elapsed, self._chunk_count, self._audio_size)
            await self._recognize()
            return True

        if Transcript.is_type(event.type) or TranscriptStart.is_type(event.type) or TranscriptStop.is_type(event.type):
            return True

        return True

    async def _recognize(self) -> None:
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
            duration = len(audio_np) / 16000
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
    args = parser.parse_args()

    parts = args.uri.replace("tcp://", "").split(":")
    host = parts[0] if parts else "0.0.0.0"
    port = int(parts[1]) if len(parts) > 1 else 10301

    _LOGGER.info("Loading model: %s ...", args.model)
    model = onnx_asr.load_model(args.model, quantization=args.quantization)
    _LOGGER.info("Model loaded. Starting server on %s:%s", host, port)

    server = AsyncTcpServer(host, port)
    await server.run(
        partial(OnnxAsrEventHandler, model=model, model_name=args.model)
    )


if __name__ == "__main__":
    asyncio.run(main())
