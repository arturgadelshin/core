# Home Assistant — Кастомная сборка для разработки

Форк [home-assistant/core](https://github.com/home-assistant/core) с доработками:
- Тишина при нераспознанной речи (убраны ответы об ошибках)
- Ссылки на аудиозаписи в автоматизациях (ветка link_audio → объединена в dev)
- Keyword subsequence matching для sentence triggers
- **Silero VAD** — замена встроенного MicroWakeWord на нейросетевой VAD через torch JIT
- **Trigger check** — проверка распознанного текста на совпадение с триггерами
- **SQLite-персистентность** — настройки по каждому спутнику

## Требования

- Docker Desktop (запущен)
- Git

## Быстрая сборка

```bash
docker build -f Dockerfile.my_dev -t ha-custom .
```

### Известные проблемы при сборке

| Проблема | Решение |
|----------|---------|
| `isal` таймаут | Убран из Dockerfile (опциональный пакет) |
| `hass-release` таймаут | Закомментирован (не нужен для dev) |
| `pycares`/`aiodns` несовместимость | Патч `from __future__ import annotations` в Dockerfile |
| Runtime `pyotp` TLS error | Запускать с `-e PIP_INDEX_URL=...` (см. ниже) |

## Запуск через docker-compose

```bash
docker compose up -d
```

После запуска открыть http://localhost:8123

## Редактирование кода на лету

Исходники примонтированы как volume. После правки:

```bash
# Очистить __pycache__ и перезапустить
docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__ /workspaces/core/homeassistant/components/assist_satellite/__pycache__ /workspaces/core/homeassistant/components/esphome/__pycache__ /workspaces/core/homeassistant/components/wyoming/__pycache__
docker restart ha-test
```

**Пересборка образа нужна только при изменении зависимостей (requirements.txt / Dockerfile).**

```bash
# Пересобрать STT сервер
docker compose build whisper; docker compose up -d whisper
```

---

## Структура проекта

### Конфигурация и данные

| Что | Путь (в контейнере) | Путь (на хосте) | Описание |
|-----|---------------------|-----------------|----------|
| YAML дефолты | `/config/conf_assist_pipeline.yaml` | `config/conf_assist_pipeline.yaml` | Централизованные дефолты для параметров VAD |
| SQLite БД | `/config/assist_satellite_settings/vad_settings.db` | `config/assist_satellite_settings/vad_settings.db` | Настройки по каждому спутнику |
| Recognition log | `/config/assist_pipeline/recognition_log.txt` | `config/assist_pipeline/recognition_log.txt` | Лог всех распознаваний (gzip ротация >100MB) |
| HA конфигурация | `/config/` | `config/` | Home Assistant config directory |

### Приоритет настроек

```
SQLite DB (per-satellite) > conf_assist_pipeline.yaml (defaults) > HARDCODED_DEFAULTS (code)
```

### Исходный код

#### assist_pipeline — pipeline обработки голоса

| Файл | Описание |
|------|----------|
| `pipeline.py` | `AudioSettings` (12 параметров), `_check_trigger()`, `_speech_to_text_stream()`, `RecognitionLogger` |
| `recognition_log.py` | `RecognitionLogger.log()` — лог финальных распознаваний, gzip ротация |
| `silero_vad_manager.py` | `SileroVadSingleton`, `SileroVadStream`, `SileroVadPerPipeline` |
| `audio_enhancer.py` | `SileroVadSpeexEnhancer` — буферизация 10ms→32ms, Speex noise+gain |
| `vad.py` | `VoiceCommandSegmenter` — state machine (ожидание → запись → обрыв) |
| `__init__.py` | Загрузка singleton Silero при старте HA |

#### assist_satellite — спутники и настройки

| Файл | Описание |
|------|----------|
| `entity.py` | 12 атрибутов, `_resolve_audio_settings()`, SQLite load/save |
| `vad_settings_db.py` | `VadSettingsStore` — SQLite, ALTER TABLE миграции, YAML defaults |
| `__init__.py` | Регистрация 11 сервисов |
| `services.yaml` | 11 сервисов с русскими описаниями и ползунками |

#### Прочие компоненты

| Файл | Описание |
|------|----------|
| `esphome/assist_satellite.py` | `_resolve_audio_settings()` с fallback DB > ESPHome для noise/gain/volume |
| `wyoming/stt.py` | `_batch_process()` — отправка аудио + чтение Transcript |
| `conversation/default_agent.py` | `_keyword_match_triggers()` — keyword subsequence matching |

#### STT сервер

| Файл | Описание |
|------|----------|
| `wyoming-onnxasr/server.py` | GigaAM v3 RNN-T int8, batch режим (без partial) |
| `docker-compose.yaml` | Контейнеры ha-test + wyoming-onnxasr |

---

## Параметры VAD (11 настраиваемых сервисов)

| Параметр | Дефолт | Сервис | Описание |
|----------|--------|--------|----------|
| `speech_threshold` | 0.5 | `set_speech_threshold` | Порог речи внутри команды |
| `before_command_speech_threshold` | 0.5 | `set_before_command_speech_threshold` | Порог начала команды |
| `silence_seconds` | 2.0 | `set_silence_seconds` | Тишина до обрыва записи |
| `command_seconds` | 1.5 | `set_command_seconds` | Минимальная длительность команды |
| `vad_timeout_seconds` | 30.0 | `set_vad_timeout` | Максимальная длина записи |
| `vad_mode` | per_pipeline | `set_vad_mode` | singleton / per_pipeline |
| `before_command_timeout_seconds` | 5.0 | `set_before_command_timeout` | Таймаут до начала команды |
| `noise_suppression_level` | 0 | `set_noise_suppression` | Шумоподавление Speex (0-4) |
| `auto_gain_dbfs` | 0 | `set_auto_gain` | Автоусиление дБFS (0-31) |
| `volume_multiplier` | 1.0 | `set_volume_multiplier` | Множитель громкости микрофона |
| `trigger_timeout_seconds` | 7.0 | `set_trigger_timeout` | Таймаут ожидания триггера после начала речи |

---

## Логика pipeline

### Wake word → команда
```
Wake word → VAD ожидает речь (5с) → речь началась
  → VAD слушает до тишины (2с) ИЛИ trigger_timeout (7с)
  → batch распознавание → _check_trigger()
     → trigger=True → intent → ответ
     → trigger=False → pipeline сброс
```

### ask_question
```
Вопрос (TTS) → VAD ожидает ответ (до vad_timeout)
  → batch распознавание → полный текст → intent → ответ
  → trigger check НЕ выполняется
```

---

## Логи

### Docker логи
```bash
docker logs ha-test --since 60s 2>&1 | Select-Object -Last 20
docker logs wyoming-onnxasr --since 60s 2>&1 | Select-Object -Last 20
```

### Recognition log
```bash
docker exec ha-test cat /config/assist_pipeline/recognition_log.txt
```

Формат:
```
[2026-06-08 08:44:57] satellite=assist_satellite.gg_voice_tdm_none mode=vad trigger=True dur=7.0s
  "один вызови два лит"
[2026-06-08 08:44:00] satellite=assist_satellite.gg_voice_tdm_none mode=vad trigger=False dur=8.5s
  "один два три четыре пять"
[2026-06-08 08:45:37] satellite=assist_satellite.gg_voice_tdm_none mode=ask_question trigger=True dur=30.0s
  "стоимость лицензии..."
```

### SQLite — настройки спутника
```bash
docker exec ha-test python3 -c "
import sqlite3
conn = sqlite3.connect('/config/assist_satellite_settings/vad_settings.db')
conn.row_factory = sqlite3.Row
for row in conn.execute('SELECT * FROM satellite_settings'):
    print(dict(row))
"
```

---

## Ключевые решения

Архив технических решений из истории разработки (сессия 01–08.06.2026):

| Решение | Причина |
|---------|---------|
| torch JIT вместо ONNX | ONNX через onnxruntime всегда возвращает ~0.0005 |
| per_pipeline по умолчанию | Изоляция pipeline, модель stateless — разницы нет |
| 512 сэмплов (не 480) | Silero VAD не поддерживает 480, минимум 512 |
| SQLite вместо RestoreEntity | Надёжная персистентность, ALTER TABLE миграции, централизованное хранилище |
| Batch VAD + trigger check | Проще и надёжнее streaming; RTFx=10-13x достаточно |
| `_check_trigger()` после batch | Keyword subsequence matching на финальном тексте — точнее чем на partial |
| RNN-T лучше CTC | CTC даёт "вызоровлеи" вместо "вызови лифт" |
| RNNoise убран | Ресемплинг 16→48→16kHz искажает аудио, RTFx падает |
| Batch STT (без partial) | partial results не используются, batch экономит CPU |
| `enable_trigger_check=False` для ask_question | ask_question ожидает любой ответ, не только trigger |

## Критический контекст

- **ONNX сломана:** `silero_vad.onnx` через onnxruntime → всегда ~0.0005
- **torch JIT работает:** `silero_vad.jit` → корректные вероятности
- **ESPHome аудио тихое:** peak ~100-600 из 32767
- **ESPHome чанки:** 1024 байта (32ms). Pipeline разбивает на 320 байт (10ms). Enhancer буферизует обратно.
- **GigaAM batch RTFx:** 10-13x (real-time factor, чем выше тем лучше)
- **HA логирует WARNING+:** `_LOGGER.debug` невидим в логах, `_LOGGER.warning` виден
- **`__pycache__`** нужно очищать перед рестартом
- **Silero VAD prob высокая после речи:** prob=0.98-1.00 для шума/эха → silence_seconds может не сработать

---

## Документация

- [SILERO_VAD.md](SILERO_VAD.md) — документация Silero VAD (архитектура, параметры, troubleshooting)
