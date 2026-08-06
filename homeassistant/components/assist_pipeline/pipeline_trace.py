"""Pipeline trace logger for diagnostics.

Writes detailed pipeline execution traces to a text file for debugging
wake-word -> listening -> processing -> responding chain issues.

Uses a single background worker thread with a queue to avoid creating
threads on every trace call.
"""

import gzip
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue

_LOG_DIR = "/config/assist_pipeline"
_LOG_FILE = os.path.join(_LOG_DIR, "pipeline_trace.log")
_MAX_SIZE = 100 * 1024 * 1024
_MAX_ARCHIVES = 3
_FLUSH_INTERVAL = 0.5


class PipelineTraceLogger:
    """Writes detailed pipeline execution traces to a text file.

    Each pipeline run produces a block of log lines starting with RUN_START
    and ending with RUN_END, with elapsed timestamps relative to run start.
    """

    _timings: dict[str, dict] = {}
    _sat_to_run: dict[str, str] = {}

    _queue: Queue[str | None] | None = None
    _worker: threading.Thread | None = None
    _file = None

    @classmethod
    def _ensure_worker(cls) -> None:
        """Start the background worker thread if not running."""
        if cls._worker is not None and cls._worker.is_alive():
            return
        cls._queue = Queue()
        cls._worker = threading.Thread(
            target=cls._worker_loop, daemon=True, name="pipeline-trace"
        )
        cls._worker.start()

    @classmethod
    def _worker_loop(cls) -> None:
        """Background worker: drains queue and writes to file."""
        assert cls._queue is not None
        os.makedirs(_LOG_DIR, exist_ok=True)
        buf: list[str] = []
        while True:
            try:
                item = cls._queue.get(timeout=_FLUSH_INTERVAL)
            except Exception:
                item = None

            if item is not None:
                buf.append(item)

            if buf and (item is None or cls._queue.empty()):
                cls._flush(buf)
                buf.clear()

    @classmethod
    def _flush(cls, lines: list[str]) -> None:
        """Write buffered lines to file and rotate if needed."""
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass
        cls._rotate_if_needed()

    @staticmethod
    def trace_start(
        run_id: str,
        satellite_id: str | None,
        pipeline_name: str,
        stt_engine: str | None,
        tts_engine: str | None,
        start_stage: str,
        end_stage: str,
        audio_settings,
    ) -> None:
        """Open a new trace block for a pipeline run."""
        ts = time.monotonic()
        PipelineTraceLogger._timings[run_id] = {
            "_start": ts,
            "_sat": satellite_id,
        }
        if satellite_id:
            PipelineTraceLogger._sat_to_run[satellite_id] = run_id

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        sat = satellite_id or "unknown"
        a = audio_settings
        block = (
            "=" * 64 + "\n"
            f"[{now}] RUN_START     run={run_id[:12]}  sat={sat}\n"
            f"  pipeline={pipeline_name}  stt={stt_engine}  tts={tts_engine}\n"
            f"  stage: {start_stage}->{end_stage}\n"
            f"  audio: silence={a.silence_seconds}s cmd={a.command_seconds}s "
            f"bc_to={a.before_command_timeout_seconds}s "
            f"trig_to={a.trigger_timeout_seconds}s "
            f"vad_to={a.vad_timeout_seconds}s\n"
            f"         sp_thr={a.speech_threshold} bc_thr={a.before_command_speech_threshold} "
            f"ns={a.noise_suppression_level} ag={a.auto_gain_dbfs} "
            f"vol={a.volume_multiplier} vad={a.vad_mode}\n"
        )
        PipelineTraceLogger._enqueue(block)

    @staticmethod
    def trace(run_id: str, stage: str, **kwargs) -> None:
        """Write a single trace event line."""
        timing = PipelineTraceLogger._timings.get(run_id)
        if timing is not None:
            elapsed = time.monotonic() - timing["_start"]
        else:
            elapsed = 0.0

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        parts = [f"[{now}] {stage:<13}"]
        for k, v in kwargs.items():
            parts.append(f"{k}={v}")
        parts.append(f" +{elapsed:.2f}s")
        PipelineTraceLogger._enqueue(" ".join(parts) + "\n")

    @staticmethod
    def trace_end(
        run_id: str,
        total_dur: float | None = None,
        final_stage: str | None = None,
    ) -> None:
        """Close a trace block."""
        timing = PipelineTraceLogger._timings.pop(run_id, None)
        sat = timing["_sat"] if timing else None
        if sat:
            PipelineTraceLogger._sat_to_run.pop(sat, None)

        if total_dur is None and timing is not None:
            total_dur = time.monotonic() - timing["_start"]

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        dur_str = f"{total_dur:.2f}s" if total_dur is not None else "?"
        line = (
            f"[{now}] RUN_END       "
            f"total={dur_str} final={final_stage or '?'}\n"
        )
        PipelineTraceLogger._enqueue(line)

    @staticmethod
    def trace_satellite(
        satellite_id: str | None, stage: str, **kwargs
    ) -> None:
        """Trace by satellite_id — resolves to the active run_id."""
        run_id = PipelineTraceLogger._sat_to_run.get(satellite_id or "")
        if run_id:
            PipelineTraceLogger.trace(run_id, stage, **kwargs)

    @classmethod
    def _enqueue(cls, text: str) -> None:
        """Put text into the write queue (non-blocking)."""
        cls._ensure_worker()
        assert cls._queue is not None
        cls._queue.put_nowait(text)

    @staticmethod
    def _rotate_if_needed() -> None:
        """Gzip-rotate the trace log when it exceeds _MAX_SIZE."""
        if not os.path.exists(_LOG_FILE):
            return
        try:
            if os.path.getsize(_LOG_FILE) < _MAX_SIZE:
                return
        except OSError:
            return

        try:
            archive_name = (
                f"pipeline_trace_"
                f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt.gz"
            )
            archive_path = os.path.join(_LOG_DIR, archive_name)

            with open(_LOG_FILE, "rb") as f_in:
                with gzip.open(archive_path, "wb") as f_out:
                    f_out.writelines(f_in)

            open(_LOG_FILE, "w").close()

            PipelineTraceLogger._cleanup_old_archives()
        except Exception:
            pass

    @staticmethod
    def _cleanup_old_archives() -> None:
        """Keep only the most recent _MAX_ARCHIVES gzip files."""
        try:
            archives = sorted(
                Path(_LOG_DIR).glob("pipeline_trace_*.txt.gz"),
                key=lambda p: p.stat().st_mtime,
            )
            while len(archives) > _MAX_ARCHIVES:
                oldest = archives.pop(0)
                oldest.unlink(missing_ok=True)
        except Exception:
            pass
