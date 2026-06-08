import gzip
import os
from datetime import datetime
from pathlib import Path
import threading

_LOG_DIR = "/config/assist_pipeline"
_LOG_FILE = os.path.join(_LOG_DIR, "recognition_log.txt")
_MAX_SIZE = 100 * 1024 * 1024
_MAX_ARCHIVES = 3


class RecognitionLogger:
    _lock = threading.Lock()

    @staticmethod
    def log(
        satellite_id: str | None,
        mode: str,
        text: str,
        trigger_matched: bool,
        duration: float | None = None,
    ) -> None:
        try:
            RecognitionLogger._rotate_if_needed()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dur_str = f"{duration:.1f}s" if duration is not None else "?"
            trigger_str = "trigger=True" if trigger_matched else "trigger=False"
            sat = satellite_id or "unknown"
            lines = (
                f"[{ts}] satellite={sat} mode={mode} {trigger_str} dur={dur_str}\n"
                f"  \"{text}\"\n"
            )
            with RecognitionLogger._lock:
                os.makedirs(_LOG_DIR, exist_ok=True)
                with open(_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(lines)
        except Exception:
            pass

    @staticmethod
    def _rotate_if_needed() -> None:
        if not os.path.exists(_LOG_FILE):
            return
        try:
            if os.path.getsize(_LOG_FILE) < _MAX_SIZE:
                return
        except OSError:
            return

        with RecognitionLogger._lock:
            try:
                if not os.path.exists(_LOG_FILE):
                    return
                if os.path.getsize(_LOG_FILE) < _MAX_SIZE:
                    return

                archive_name = f"recognition_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt.gz"
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
