"""Recognition logger — writes all STT recognition results to a text file.

Uses a single background worker thread with a queue to avoid creating
threads on every recognition call.
"""

import gzip
import os
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue

_LOG_DIR = "/config/assist_pipeline"
_LOG_FILE = os.path.join(_LOG_DIR, "recognition_log.txt")
_MAX_SIZE = 25 * 1024 * 1024
_MAX_ARCHIVES = 3
_FLUSH_INTERVAL = 0.5


class RecognitionLogger:
    """Writes all recognition results to a text file.

    Uses a queue + single background worker thread for non-blocking writes.
    """

    _queue: Queue[str | None] | None = None
    _worker: threading.Thread | None = None

    @classmethod
    def _ensure_worker(cls) -> None:
        """Start the background worker thread if not running."""
        if cls._worker is not None and cls._worker.is_alive():
            return
        cls._queue = Queue()
        cls._worker = threading.Thread(
            target=cls._worker_loop, daemon=True, name="recognition-log"
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
    def log(
        satellite_id: str | None,
        mode: str,
        text: str,
        trigger_matched: bool,
        duration: float | None = None,
    ) -> None:
        """Queue a recognition result for async writing. Non-blocking."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur_str = f"{duration:.1f}s" if duration is not None else "?"
        trigger_str = "trigger=True" if trigger_matched else "trigger=False"
        sat = satellite_id or "unknown"
        lines = (
            f"[{ts}] satellite={sat} mode={mode} {trigger_str} dur={dur_str}\n"
            f"  \"{text}\"\n"
        )
        RecognitionLogger._enqueue(lines)

    @classmethod
    def _enqueue(cls, text: str) -> None:
        """Put text into the write queue (non-blocking)."""
        cls._ensure_worker()
        assert cls._queue is not None
        cls._queue.put_nowait(text)

    @staticmethod
    def _rotate_if_needed() -> None:
        """Gzip-rotate the log when it exceeds _MAX_SIZE."""
        if not os.path.exists(_LOG_FILE):
            return
        try:
            if os.path.getsize(_LOG_FILE) < _MAX_SIZE:
                return
        except OSError:
            return

        try:
            archive_name = (
                f"recognition_log_"
                f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt.gz"
            )
            archive_path = os.path.join(_LOG_DIR, archive_name)

            with open(_LOG_FILE, "rb") as f_in:
                with gzip.open(archive_path, "wb") as f_out:
                    f_out.writelines(f_in)

            open(_LOG_FILE, "w").close()

            RecognitionLogger._cleanup_old_archives()
        except Exception:
            pass

    @staticmethod
    def _cleanup_old_archives() -> None:
        """Keep only the most recent _MAX_ARCHIVES gzip files."""
        try:
            archives = sorted(
                Path(_LOG_DIR).glob("recognition_log_*.txt.gz"),
                key=lambda p: p.stat().st_mtime,
            )
            while len(archives) > _MAX_ARCHIVES:
                oldest = archives.pop(0)
                oldest.unlink(missing_ok=True)
        except Exception:
            pass
