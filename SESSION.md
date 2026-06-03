# Сессия разработки: Silero VAD для Home Assistant

**Дата:** 01-03.06.2026
**Репозиторий:** https://github.com/arturgadelshin/core
**Рабочая директория:** `E:\Project_OpenCode\Core\core`
**Задача:** Заменить встроенный MicroWakeWord VAD на Silero VAD в форке Home Assistant

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
- Протестировано 3 варианта ONNX моделей — все ведут себя одинаково
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

### 7. Настраиваемые параметры VAD
- 6 параметров с персистентностью (RestoreEntity) для каждого спутника
- 6 сервисов с русскими описаниями и ползунками в UI
- `services.yaml` с полным описанием на русском

### 8. Документация
- `SILERO_VAD.md` — полная документация: архитектура, параметры, сервисы, troubleshooting
- `README.dev.md` — обзор проекта, структура файлов
- `SESSION.md` — история сессии

---

## Текущие параметры VAD (по умолчанию)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `speech_threshold` | **0.5** | Порог речи внутри команды |
| `before_command_speech_threshold` | **0.5** | Порог для начала команды |
| `silence_seconds` | **2.0** | Тишина до обрыва записи |
| `command_seconds` | **2.0** | Минимальная длительность команды |
| `vad_timeout_seconds` | **30.0** | Максимальная длина записи |
| `vad_mode` | **per_pipeline** | Режим VAD (singleton / per_pipeline) |

---

## Текущий статус

- [x] Анализ форка и слияние веток
- [x] Keyword subsequence matching
- [x] Исследование VAD вариантов
- [x] Интеграция Silero VAD (torch JIT)
- [x] Буферизация 10ms → 32ms
- [x] Исправление обрыва на паузах
- [x] Настраиваемые параметры VAD (6 сервисов)
- [x] Описания сервисов на русском
- [x] Документация (SILERO_VAD.md, README.dev.md, SESSION.md)
- [ ] Очистить отладочное WARNING-логирование
- [ ] Убрать debug WAV-сохранение из esphome/assist_satellite.py
- [ ] Тестирование с разными профилями (шум, тихая речь, диктовка)

---

## Ключевые решения

| Решение | Причина |
|---------|---------|
| torch JIT вместо ONNX | ONNX через onnxruntime всегда возвращает ~0.0005 |
| per_pipeline по умолчанию | Изоляция pipeline, модель stateless — разницы нет |
| 512 сэмплов (не 480) | Silero VAD не поддерживает 480, минимум 512 |
| RestoreEntity для параметров | Персистентность без дополнительной интеграции |
| Сервисы вместо сущностей-number | ESPHome отклоняет междоменные сущности |
| `_resolve_audio_settings()` | Параметры из атрибутов сущности, не из VadSensitivity enum |

---

## Критический контекст

- **ONNX сломана:** `silero_vad.onnx` через onnxruntime 1.26.0 → всегда ~0.0005
- **torch JIT работает:** `silero_vad.jit` → корректные вероятности
- **ESPHome аудио тихое:** peak ~100-600 из 32767. Silero справляется без усиления.
- **ESPHome чанки:** 1024 байта (32ms). Pipeline разбивает на 320 байт (10ms). Enhancer буферизует обратно.
- **__pycache__** нужно очищать перед рестартом:
  ```bash
  docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__ /workspaces/core/homeassistant/components/assist_satellite/__pycache__
  docker restart ha-test
  ```
- **Полная пересборка** нужна только при изменении зависимостей/Dockerfile

---

## Изменённые файлы

| Файл | Тип изменения | Описание |
|------|--------------|----------|
| `assist_pipeline/silero_vad_manager.py` | Новый | SileroVadSingleton, SileroVadStream, SileroVadPerPipeline, загрузка torch JIT |
| `assist_pipeline/audio_enhancer.py` | Изменён | SileroVadSpeexEnhancer с 32ms буферизацией |
| `assist_pipeline/const.py` | Изменён | DATA_SILERO_VAD, SILERO_* константы |
| `assist_pipeline/pipeline.py` | Изменён | AudioSettings (6 параметров), _create_silero_vad(), отладочное логирование |
| `assist_pipeline/__init__.py` | Изменён | Загрузка singleton Silero при старте HA |
| `assist_pipeline/vad.py` | Изменён | Отладочное логирование VoiceCommandSegmenter |
| `assist_satellite/entity.py` | Изменён | 6 атрибутов + RestoreEntity + 6 service handlers |
| `assist_satellite/__init__.py` | Изменён | Регистрация 6 сервисов |
| `assist_satellite/services.yaml` | Изменён | 6 сервисов с русскими описаниями и ползунками |
| `esphome/assist_satellite.py` | Изменён | Debug WAV-сохранение |
| `Dockerfile.my_dev` | Изменён | silero-vad, torch, патч aiodns |
| `SILERO_VAD.md` | Новый | Полная документация |
| `README.dev.md` | Изменён | Обновлён под Silero VAD |
| `SESSION.md` | Этот файл | История сессии |

---

## Следующий шаг

Очистить отладочное логирование (заменить `_LOGGER.warning` на `_LOGGER.debug`) после подтверждения стабильной работы.
