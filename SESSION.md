# Сессия разработки: Голосовой ассистент Home Assistant

**Дата:** 01-08.06.2026 (обновлено 08.06)
**Репозиторий:** https://github.com/arturgadelshin/core
**Рабочая директория:** `E:\Project_OpenCode\Core\core`
**Ветка:** `model_stream`
**Задача:** Настраиваемый голосовой помощник с Silero VAD, trigger matching, SQLite-персистентностью

---

## Что было сделано

### 1. Keyword Subsequence Matching (01.06)
- Форк `home-assistant/core`, слияние веток `link_audio` → `dev` → `main`
- Добавлен `_keyword_match_triggers()` в `default_agent.py`
- Собран Docker-образ `Dockerfile.my_dev`

### 2. Исследование VAD (02.06)
- Проанализирована цепочка: ESPHome → assist_satellite → pipeline → STT
- Изучён встроенный MicroWakeWord VAD (energy-based, неточный)
- Выбран Silero VAD как замена (нейросетевой, точный)

### 3. Интеграция Silero VAD — ONNX (провал)
- Попробовал `silero_vad.onnx` через `onnxruntime 1.26.0`
- **Баг:** ONNX модель всегда возвращает ~0.0005 независимо от входного сигнала
- **Решение:** отказаться от ONNX, использовать torch JIT

### 4. Интеграция Silero VAD — torch JIT (успех)
- Создан `silero_vad_manager.py` с тремя классами:
  - `SileroVadSingleton` — загрузка модели один раз при старте HA
  - `SileroVadStream` — lightweight wrapper для shared модели
  - `SileroVadPerPipeline` — отдельная модель на pipeline
- Модель: `silero_vad.jit` через `torch.jit.load()`
- Результаты: тихая речь 0.064, громкая речь 0.819, тишина 0.032

### 5. Буферизация аудио
- ESPHome отправляет 1024-байтовые чанки (32ms)
- Pipeline разбивает на 320-байтовые (10ms) через `chunk_samples`
- `SileroVadSpeexEnhancer` буферизует обратно до 1024 байт для Silero
- Константы: `SILERO_MS_PER_CHUNK=32`, `SILERO_SAMPLES_PER_CHUNK=512`, `SILERO_BYTES_PER_CHUNK=1024`

### 6. Исправление обрыва на паузах
- Проблема: `silence_seconds` перезаписывался через `VadSensitivity.to_seconds()` → 0.2с
- `_resolve_audio_settings()` в `entity.py` использовал `_resolve_vad_sensitivity()`
- Исправлено: параметры VAD берутся из атрибутов сущности, а не из VadSensitivity enum

### 7. Настраиваемые параметры VAD с SQLite-персистентностью
- `vad_settings_db.py` — SQLite хранилище настроек по каждому спутнику
- `conf_assist_pipeline.yaml` — централизованные дефолты (YAML)
- Приоритет: SQLite БД > YAML конфиг > hardcoded defaults
- Автоматические ALTER TABLE миграции при добавлении новых колонок

### 8. Таймаут до начала команды
- Добавлен `before_command_timeout_seconds` (default 5.0с)
- Если после активации за N секунд не обнаружена речь → прерывание

### 9. Шумоподавление и автоусиление
- Добавлены `noise_suppression_level` (0-4) и `auto_gain_dbfs` (0-31)
- Speex DSP: noise suppression + auto gain + volume multiplier

### 10. Streaming STT (протестирован, убран)
- Реализован streaming режим: GrowingBufferStream + partial results каждые 5с
- `_check_streaming_trigger()` — early stop при совпадении триггера
- GigaAM RNN-T batch на growing buffer, RTFx=10-13x
- **Убран:** сложность не оправдана, batch + trigger check после распознавания работает проще и надёжнее

### 11. Trigger check после распознавания (текущий подход)
- VAD слушает → тишина/таймаут → batch распознавание → `_check_trigger()`
- `_check_trigger()` вызывает `_keyword_match_triggers()` из conversation agent
- Trigger совпал → intent. Нет → pipeline сброс (stt-no-text-recognized)
- `trigger_timeout_seconds` (default 7.0с) — жёсткий лимит после начала речи
- `enable_trigger_check` — флаг: True для wake word, False для ask_question
- Все распознавания логируются в `recognition_log.txt`

### 12. Recognition logging
- `recognition_log.py` — лог финальных распознаваний
- Формат: `[timestamp] satellite=id mode=vad|ask_question trigger=True|False dur=Xs` + текст
- Ротация: gzip архив при >100MB, хранит последние 3 архива
- Запись через daemon thread, не блокирует pipeline

### 13. RNNoise (протестирован, убран)
- RNNoise .so (5h_ru_500k) — ресемплинг 16→48→16kHz искажает аудио
- RTFx падает с 10-13 до 5.9-8.1, качество распознавания хуже
- Ветка `RNNoise` сохранена для будущего

### 14. GigaAM CTC (протестирована, убрана)
- Качество хуже: "вызоровлеи" вместо "вызови лифт"
- Скорость +5-10% (незначительно)
- Вернулись на RNN-T

---

## Текущие параметры (12 настраиваемых)

| # | Параметр | Дефолт | Диапазон | Сервис | Описание |
|---|----------|--------|----------|--------|----------|
| 1 | `speech_threshold` | 0.5 | 0.1-1.0 | `set_speech_threshold` | Порог речи внутри команды |
| 2 | `before_command_speech_threshold` | 0.5 | 0.05-0.9 | `set_before_command_speech_threshold` | Порог начала команды |
| 3 | `silence_seconds` | 2.0 | 0.3-10.0 | `set_silence_seconds` | Тишина до обрыва |
| 4 | `command_seconds` | 1.5 | 0.3-10.0 | `set_command_seconds` | Минимальная длительность команды |
| 5 | `vad_timeout_seconds` | 30.0 | 1.0-60.0 | `set_vad_timeout` | Максимальная длина записи |
| 6 | `vad_mode` | per_pipeline | singleton/per_pipeline | `set_vad_mode` | Режим VAD |
| 7 | `before_command_timeout_seconds` | 5.0 | 1.0-30.0 | `set_before_command_timeout` | Таймаут до начала команды |
| 8 | `noise_suppression_level` | 0 | 0-4 | `set_noise_suppression` | Шумоподавление Speex |
| 9 | `auto_gain_dbfs` | 0 | 0-31 | `set_auto_gain` | Автоусиление дБFS |
| 10 | `volume_multiplier` | 1.0 | 0.1-30.0 | `set_volume_multiplier` | Множитель громкости микрофона |
| 11 | `trigger_timeout_seconds` | 7.0 | 1.0-30.0 | `set_trigger_timeout` | Таймаут ожидания триггера после начала речи |
| 12 | (внутренний) `enable_trigger_check` | True | — | — | False для ask_question |

---

## Логика pipeline (текущая)

### Wake word → команда
```
Wake word detected
  → BEFORE_COMMAND: ждём начала речи (before_command_timeout_seconds)
  → COMMAND_START: речь началась
     → VAD слушает (silence_seconds) + жёсткий лимит (trigger_timeout_seconds)
     → Тишина ИЛИ таймаут → batch распознавание всего буфера
     → _check_trigger(): keyword subsequence matching
        → trigger=True → intent → ответ
        → trigger=False → pipeline сброс
     → RecognitionLogger.log() — ВСЕ распознавания (и trigger, и no-trigger)
```

### ask_question
```
Вопрос задан (TTS)
  → BEFORE_COMMAND: ждём начала речи (before_command_timeout_seconds)
  → COMMAND_START: речь началась
     → VAD слушает до тишины (silence_seconds) ИЛИ таймаут (vad_timeout_seconds)
     → batch распознавание → полный текст → intent → ответ
     → enable_trigger_check=False → trigger check НЕ выполняется
     → RecognitionLogger.log() с mode=ask_question
```

---

## Файлы и расположение

### Конфигурация и данные (в контейнере, volume mount)

| Файл | Путь (в контейнере) | Описание |
|------|---------------------|----------|
| YAML дефолты | `/config/conf_assist_pipeline.yaml` | Централизованные дефолты для 11 параметров |
| SQLite БД | `/config/assist_satellite_settings/vad_settings.db` | Настройки по каждому спутнику |
| Recognition log | `/config/assist_pipeline/recognition_log.txt` | Лог всех распознаваний |
| HA config | `/config/` | Конфигурация Home Assistant |

### Исходный код HA (volume mount: `core/ → /workspaces/core/`)

| Файл | Путь | Описание |
|------|------|----------|
| Pipeline | `homeassistant/components/assist_pipeline/pipeline.py` | `AudioSettings` (12 параметров), `_check_trigger()`, `_speech_to_text_stream()`, `RecognitionLogger` |
| Recognition log | `homeassistant/components/assist_pipeline/recognition_log.py` | `RecognitionLogger.log()`, gzip ротация |
| Silero VAD | `homeassistant/components/assist_pipeline/silero_vad_manager.py` | `SileroVadSingleton`, `SileroVadStream`, `SileroVadPerPipeline` |
| Audio enhancer | `homeassistant/components/assist_pipeline/audio_enhancer.py` | `SileroVadSpeexEnhancer` |
| VAD segmenter | `homeassistant/components/assist_pipeline/vad.py` | `VoiceCommandSegmenter` state machine |
| Pipeline init | `homeassistant/components/assist_pipeline/__init__.py` | Singleton Silero при старте HA |
| Entity | `homeassistant/components/assist_satellite/entity.py` | 12 атрибутов, `_resolve_audio_settings()`, SQLite load/save |
| Satellite init | `homeassistant/components/assist_satellite/__init__.py` | Регистрация 11 сервисов |
| Services YAML | `homeassistant/components/assist_satellite/services.yaml` | 11 сервисов с русскими описаниями |
| DB store | `homeassistant/components/assist_satellite/vad_settings_db.py` | `VadSettingsStore`, `HARDCODED_DEFAULTS`, ALTER TABLE миграции |
| ESPHome satellite | `homeassistant/components/esphome/assist_satellite.py` | `_resolve_audio_settings()` с fallback DB > ESPHome |
| STT (wyoming) | `homeassistant/components/wyoming/stt.py` | `_batch_process()` — отправка аудио + чтение Transcript |
| Conversation agent | `homeassistant/components/conversation/default_agent.py` | `_keyword_match_triggers()` — keyword subsequence matching |

### STT сервер

| Файл | Путь | Описание |
|------|------|----------|
| Server | `wyoming-onnxasr/server.py` | GigaAM v3 RNN-T int8, batch режим |
| Dockerfile | `Dockerfile.my_dev` (образ whisper) | onnx-asr + wyoming |

### Docker

| Файл | Путь | Описание |
|------|------|----------|
| Compose | `docker-compose.yaml` | ha-test + wyoming-onnxasr |
| HA Dockerfile | `Dockerfile.my_dev` | ha-custom образ (torch, silero-vad) |

### Документация

| Файл | Описание |
|------|----------|
| `SESSION.md` | Этот файл — история сессии |
| `SILERO_VAD.md` | Полная документация Silero VAD |
| `README.dev.md` | Обзор проекта, структура файлов |
| `PLAN.md` | План keyword subsequence matching |

---

## Команды для разработки

```bash
# Рабочая директория
cd E:\Project_OpenCode\Core\core

# Очистить __pycache__ и перезапустить HA
docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__ /workspaces/core/homeassistant/components/assist_satellite/__pycache__ /workspaces/core/homeassistant/components/esphome/__pycache__ /workspaces/core/homeassistant/components/wyoming/__pycache__
docker restart ha-test

# Пересобрать STT сервер
docker compose build whisper; docker compose up -d whisper

# Логи
docker logs ha-test --since 60s 2>&1 | Select-Object -Last 20
docker logs wyoming-onnxasr --since 60s 2>&1 | Select-Object -Last 20

# Recognition log
docker exec ha-test cat /config/assist_pipeline/recognition_log.txt

# SQLite — посмотреть настройки спутника
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

---

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

## Текущий статус

- [x] Анализ форка и слияние веток
- [x] Keyword subsequence matching
- [x] Исследование VAD вариантов
- [x] Интеграция Silero VAD (torch JIT)
- [x] Буферизация 10ms → 32ms
- [x] Исправление обрыва на паузах
- [x] Настраиваемые параметры VAD (12 параметров, SQLite)
- [x] Описания сервисов на русском
- [x] Таймаут до начала команды
- [x] Шумоподавление и автоусиление
- [x] Trigger check после распознавания
- [x] Trigger timeout (жёсткий лимит после начала речи)
- [x] Recognition logging (все распознавания, включая ask_question)
- [x] Streaming STT протестирован и убран
- [x] RNNoise протестирован и убран
- [x] GigaAM CTC протестирована, вернулись на RNN-T
- [x] Batch STT (partial выключен на сервере)
- [x] Документация обновлена
- [ ] Протестировать стабильность при 5+ устройствах одновременно
- [ ] При необходимости: оптимизировать размер буфера для распознавания
