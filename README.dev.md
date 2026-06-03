# Home Assistant — Кастомная сборка для разработки

Форк [home-assistant/core](https://github.com/home-assistant/core) с доработками:
- Тишина при нераспознанной речи (убраны ответы об ошибках)
- Ссылки на аудиозаписи в автоматизациях (ветка link_audio → объединена в dev)
- Keyword subsequence matching для sentence triggers
- **Silero VAD** — замена встроенного MicroWakeWord на нейросетевой VAD через torch JIT

## Требования

- Docker Desktop (запущен)
- Git

## Быстрая сборка

```bash
cd E:\Project_OpenCode\Core\core

docker build -f Dockerfile.my_dev -t ha-custom .
```

### Известные проблемы при сборке

| Проблема | Решение |
|----------|---------|
| `isal` таймаут | Убран из Dockerfile (опциональный пакет) |
| `hass-release` таймаут | Закомментирован (не нужен для dev) |
| `pycares`/`aiodns` несовместимость | Патч `from __future__ import annotations` в Dockerfile |
| Runtime `pyotp` TLS error | Запускать с `-e PIP_INDEX_URL=...` (см. ниже) |

## Запуск с volume mount (для разработки)

```bash
mkdir config

docker run -it --name ha-test ^
  -e PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ^
  -e UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ^
  -v %cd%:/workspaces/core ^
  -v %cd%/config:/config ^
  -p 8123:8123 ^
  ha-custom
```

После запуска открыть http://localhost:8123

## Редактирование кода на лету

Исходники примонтированы как volume. После правки:

```bash
# Очистить __pycache__ и перезапустить
docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__
docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_satellite/__pycache__
docker restart ha-test
```

**Пересборка образа нужна только при изменении зависимостей (requirements.txt / Dockerfile).**

## Структура веток

| Ветка | Описание |
|-------|----------|
| `main` | Стабильная сборка |
| `dev` | Разработка (все изменения слиты) |
| `link_audio` | (устарела, слита в dev) |

## Кастомные изменения vs upstream

### Файлы Silero VAD

| Файл | Описание |
|------|----------|
| `homeassistant/components/assist_pipeline/silero_vad_manager.py` | **Новый.** Классы `SileroVadSingleton`, `SileroVadStream`, `SileroVadPerPipeline`, загрузка torch JIT модели |
| `homeassistant/components/assist_pipeline/audio_enhancer.py` | **Изменён.** `SileroVadSpeexEnhancer` с 32ms буферизацией для Silero |
| `homeassistant/components/assist_pipeline/const.py` | **Изменён.** Добавлены `DATA_SILERO_VAD`, `SILERO_MS_PER_CHUNK=32`, `SILERO_SAMPLES_PER_CHUNK=512`, `SILERO_BYTES_PER_CHUNK=1024` |
| `homeassistant/components/assist_pipeline/pipeline.py` | **Изменён.** `AudioSettings` с настраиваемыми VAD-параметрами, `_create_silero_vad()`, `_speech_to_text_stream()` с VAD |
| `homeassistant/components/assist_pipeline/__init__.py` | **Изменён.** Инициализация singleton Silero при старте HA |
| `homeassistant/components/assist_pipeline/vad.py` | **Изменён.** Отладочное логирование в `VoiceCommandSegmenter` |

### Файлы настроек VAD (по спутникам)

| Файл | Описание |
|------|----------|
| `homeassistant/components/assist_satellite/entity.py` | **Изменён.** 7 настраиваемых атрибутов с RestoreEntity-персистентностью |
| `homeassistant/components/assist_satellite/__init__.py` | **Изменён.** Регистрация 7 сервисов VAD |
| `homeassistant/components/assist_satellite/services.yaml` | **Изменён.** Определения сервисов с русскими описаниями и ползунками |

### Прочие файлы

| Файл | Описание |
|------|----------|
| `homeassistant/components/conversation/default_agent.py` | Убраны тексты ошибок + keyword matching |
| `homeassistant/components/esphome/assist_satellite.py` | Отладочное WAV-сохранение |
| `Dockerfile.my_dev` | Docker-образ: silero-vad, torch, патч aiodns |
| `docker-compose.yaml` | Volume mount + переменные окружения |

---

## Документация Silero VAD

См. [SILERO_VAD.md](SILERO_VAD.md) — полная документация по архитектуре, параметрам, сервисам и устранению неполадок.
