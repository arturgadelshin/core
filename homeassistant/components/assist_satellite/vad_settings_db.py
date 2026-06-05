"""SQLite store for satellite VAD/audio settings."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DB_DIR = "assist_satellite_settings"
DB_FILENAME = "vad_settings.db"

CONFIG_FILENAME = "conf_assist_pipeline.yaml"

HARDCODED_DEFAULTS = {
    "speech_threshold": 0.5,
    "before_command_speech_threshold": 0.5,
    "silence_seconds": 2.0,
    "command_seconds": 1.5,
    "vad_timeout_seconds": 30.0,
    "vad_mode": "per_pipeline",
    "before_command_timeout_seconds": 5.0,
    "noise_suppression_level": 0,
    "auto_gain_dbfs": 0,
    "recognition_mode": "vad",
    "volume_multiplier": 1.0,
}

PARAM_NAMES = set(HARDCODED_DEFAULTS.keys())


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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS satellite_settings (
                    entity_id TEXT PRIMARY KEY,
                    speech_threshold REAL DEFAULT 0.5,
                    before_command_speech_threshold REAL DEFAULT 0.5,
                    silence_seconds REAL DEFAULT 2.0,
                    command_seconds REAL DEFAULT 1.5,
                    vad_timeout_seconds REAL DEFAULT 30.0,
                    vad_mode TEXT DEFAULT 'per_pipeline',
                    before_command_timeout_seconds REAL DEFAULT 5.0,
                    noise_suppression_level INTEGER DEFAULT 0,
                    auto_gain_dbfs INTEGER DEFAULT 0
                )
            """)
            try:
                conn.execute("ALTER TABLE satellite_settings ADD COLUMN recognition_mode TEXT DEFAULT 'vad'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE satellite_settings ADD COLUMN volume_multiplier REAL DEFAULT 1.0")
            except sqlite3.OperationalError:
                pass
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
DATA_PIPELINE_DEFAULTS = "assist_pipeline_defaults"


def load_yaml_defaults(config_dir: str) -> dict[str, Any]:
    """Load defaults from conf_assist_pipeline.yaml."""
    config_path = Path(config_dir) / CONFIG_FILENAME
    if not config_path.exists():
        return dict(HARDCODED_DEFAULTS)
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        yaml_defaults = data.get("assist_pipeline", {})
        merged = dict(HARDCODED_DEFAULTS)
        for k in PARAM_NAMES:
            if k in yaml_defaults:
                merged[k] = yaml_defaults[k]
        return merged
    except Exception as ex:
        _LOGGER.warning("Failed to load %s: %s, using hardcoded defaults", config_path, ex)
        return dict(HARDCODED_DEFAULTS)


def get_pipeline_defaults(hass: HomeAssistant) -> dict[str, Any]:
    """Get pipeline defaults from hass.data (loaded during setup)."""
    return hass.data.get(DATA_PIPELINE_DEFAULTS, HARDCODED_DEFAULTS)


async def async_get_store(hass: HomeAssistant) -> VadSettingsStore:
    """Get or create the VAD settings store and load config defaults."""
    if DATA_PIPELINE_DEFAULTS not in hass.data:
        defaults = await hass.async_add_executor_job(
            load_yaml_defaults, hass.config.config_dir
        )
        hass.data[DATA_PIPELINE_DEFAULTS] = defaults
        _LOGGER.info("Loaded pipeline defaults: %s", defaults)

    if DATA_VAD_SETTINGS in hass.data:
        return hass.data[DATA_VAD_SETTINGS]

    db_dir = Path(hass.config.config_dir) / DB_DIR
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / DB_FILENAME)

    store = await hass.async_add_executor_job(VadSettingsStore, db_path)
    hass.data[DATA_VAD_SETTINGS] = store
    return store
