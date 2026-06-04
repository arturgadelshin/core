"""SQLite store for satellite VAD/audio settings."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DB_DIR = "assist_satellite_settings"
DB_FILENAME = "vad_settings.db"

PARAM_NAMES = {
    "speech_threshold",
    "before_command_speech_threshold",
    "silence_seconds",
    "command_seconds",
    "vad_timeout_seconds",
    "vad_mode",
    "before_command_timeout_seconds",
    "noise_suppression_level",
    "auto_gain_dbfs",
}

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS satellite_settings (
    entity_id TEXT PRIMARY KEY,
    speech_threshold REAL DEFAULT 0.5,
    before_command_speech_threshold REAL DEFAULT 0.5,
    silence_seconds REAL DEFAULT 2.0,
    command_seconds REAL DEFAULT 2.0,
    vad_timeout_seconds REAL DEFAULT 30.0,
    vad_mode TEXT DEFAULT 'per_pipeline',
    before_command_timeout_seconds REAL DEFAULT 4.0,
    noise_suppression_level INTEGER DEFAULT 0,
    auto_gain_dbfs INTEGER DEFAULT 0
)
"""


class VadSettingsStore:
    """Persistent storage for satellite VAD/audio settings in SQLite."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(CREATE_TABLE)
        _LOGGER.info("VAD settings DB initialized: %s", self._db_path)

    def get(self, entity_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM satellite_settings WHERE entity_id = ?",
                (entity_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def set_param(self, entity_id: str, param: str, value: Any) -> None:
        if param not in PARAM_NAMES:
            raise ValueError(f"Unknown param: {param}")
        with self._get_conn() as conn:
            conn.execute(
                f"""
                INSERT INTO satellite_settings (entity_id, {param})
                VALUES (?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET {param} = ?
                """,
                (entity_id, value, value),
            )

    def set_all(self, entity_id: str, settings: dict[str, Any]) -> None:
        cols = [k for k in PARAM_NAMES if k in settings]
        if not cols:
            return
        placeholders_insert = ", ".join(["?"] * (len(cols) + 1))
        cols_insert = "entity_id, " + ", ".join(cols)
        update_clause = ", ".join(f"{c} = ?" for c in cols)
        vals = [entity_id] + [settings[c] for c in cols]
        with self._get_conn() as conn:
            conn.execute(
                f"""
                INSERT INTO satellite_settings ({cols_insert})
                VALUES ({placeholders_insert})
                ON CONFLICT(entity_id) DO UPDATE SET {update_clause}
                """,
                vals + vals[1:],
            )

    def get_all(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM satellite_settings")
            return [dict(row) for row in cur]

    def delete(self, entity_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM satellite_settings WHERE entity_id = ?",
                (entity_id,),
            )


DATA_VAD_SETTINGS = "assist_satellite_vad_settings"


async def async_get_store(hass: HomeAssistant) -> VadSettingsStore:
    """Get or create the VAD settings store."""
    if DATA_VAD_SETTINGS in hass.data:
        return hass.data[DATA_VAD_SETTINGS]

    db_dir = Path(hass.config.config_dir) / DB_DIR
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / DB_FILENAME)

    store = await hass.async_add_executor_job(VadSettingsStore, db_path)
    hass.data[DATA_VAD_SETTINGS] = store
    return store
