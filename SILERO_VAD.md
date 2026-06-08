# Silero VAD — Полная документация

## Обзор

Silero VAD (Voice Activity Detection) — нейросетевая модель для определения наличия речи в аудиопотоке.
Заменяет встроенный MicroWakeWord/VAD в Assist Pipeline на более точную torch JIT модель.

**Модель:** `silero_vad.jit` (torch JIT, ~2.3 МБ)
**Библиотека:** `silero-vad` (pip)
**Рантайм:** `torch` (CPU, JIT inference)

---

## Архитектура

### Поток данных

```
ESPHome спутник
  │ (1024 байт = 32ms @ 16kHz 16-bit mono)
  ▼
assist_satellite entity (_resolve_audio_settings)
  │ AudioSettings(silence_seconds, command_seconds, ...)
  ▼
pipeline.py (PipelineRun._speech_to_text)
  │ создаёт SileroVadSpeexEnhancer + VoiceCommandSegmenter
  ▼
audio_enhancer.py (SileroVadSpeexEnhancer.enhance_chunk)
  │ буферизует 10ms → 32ms чанки (1024 байт)
  │ вызывает silero_vad.process_chunk(1024 bytes)
  │ возвращает EnhancedAudioChunk(audio, speech_probability)
  ▼
pipeline.py (_speech_to_text_stream)
  │ VoiceCommandSegmenter.process(chunk_seconds, speech_probability)
  │ отслеживает: SPEECH_START → запись → SILENCE_FINISH
  ▼
STT (GigaAM через wyoming-onnxasr)
```

### Режимы VAD

| Режим | Класс | Описание |
|-------|-------|----------|
| `singleton` | `SileroVadSingleton` → `SileroVadStream` | Одна модель на все pipeline. Экономит память. |
| `per_pipeline` | `SileroVadPerPipeline` | Отдельная копия модели (~10 МБ) на каждый pipeline. Изолирует pipeline. |

> **Примечание:** Модель Silero JIT stateless — не имеет внутреннего состояния между вызовами.
> Разницы в качестве распознавания между режимами **нет**.

### Ключевые классы

| Класс | Файл | Описание |
|-------|------|----------|
| `SileroVadSingleton` | `silero_vad_manager.py` | Загружает модель один раз при старте HA, хранит в `hass.data[DATA_SILERO_VAD]` |
| `SileroVadStream` | `silero_vad_manager.py` | Лёгкий wrapper вокруг shared модели для каждого потока |
| `SileroVadPerPipeline` | `silero_vad_manager.py` | Отдельная модель для каждого pipeline (lazy loading) |
| `SileroVadSpeexEnhancer` | `audio_enhancer.py` | Буферизует 10ms чанки в 32ms, вызывает Silero, возвращает `speech_probability` |
| `VoiceCommandSegmenter` | `vad.py` | State machine: ожидание речи → запись → обрыв по тишина/таймаут |
| `AudioSettings` | `pipeline.py` | Dataclass с параметрами VAD, передаётся из `AssistSatelliteEntity` |

---

## Параметры VAD

### Текущие значения по умолчанию

| Параметр | Дефолт | Диапазон |
|----------|--------|----------|
| `speech_threshold` | **0.5** | 0.1 — 1.0 |
| `before_command_speech_threshold` | **0.5** | 0.05 — 0.9 |
| `silence_seconds` | **2.0** | 0.3 — 10.0 |
| `command_seconds` | **1.5** | 0.3 — 10.0 |
| `vad_timeout_seconds` | **30.0** | 1.0 — 60.0 |
| `vad_mode` | **per_pipeline** | singleton / per_pipeline |
| `before_command_timeout_seconds` | **5.0** | 1.0 — 30.0 |
| `noise_suppression_level` | **0** | 0 — 4 |
| `auto_gain_dbfs` | **0** | 0 — 31 |
| `volume_multiplier` | **1.0** | 0.1 — 30.0 |
| `trigger_timeout_seconds` | **7.0** | 1.0 — 30.0 |

### Подробное описание параметров

#### 1. `speech_threshold` — порог речи внутри команды

Порог вероятности речи Silero VAD, используемый **во время** голосовой команды.
Определяет, считает ли система текущий аудио-фрагмент речью или тишиной.

- **0.1-0.3** — высокая чувствительность: распознаёт тихую речь, но может принимать шум за речь
- **0.5** (default) — сбалансированный
- **0.6-0.8** — низкая чувствительность: требуется чёткая речь, фоновый шум игнорируется

**Когда менять:**
- Уменьшить если ассистент обрывает запись на тихой речи
- Увеличить если ассистент не обрывает запись из-за фонового шума

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_speech_threshold`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_speech_threshold
data:
  value: 0.3
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 2. `before_command_speech_threshold` — порог начала команды

Порог вероятности речи для **обнаружения начала** голосовой команды.
Обычно устанавливается ниже основного порога, чтобы система уловила начало речи
даже при тихом голосе.

- **0.05-0.15** — распознаёт речь раньше, но может срабатывать на шум
- **0.5** (default) — сбалансированный
- **0.6-0.8** — требует более чёткой речи для начала записи

**Когда менять:**
- Уменьшить если ассистент не реагирует на тихое "окей" или начало фразы
- Увеличить если ассистент ложно начинает запись на фоновый шум

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_before_command_speech_threshold`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_before_command_speech_threshold
data:
  value: 0.15
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 3. `silence_seconds` — тишина до обрыва

Сколько секунд **непрерывной** тишины должно пройти, прежде чем запись будет остановлена.

- **0.5-1.0с** — быстрый ответ, но обрезает паузы между предложениями
- **2.0с** (default) — допускает естественные паузы в речи
- **3.0-5.0с** — для длинных фраз с паузами, но ассистент медленнее реагирует

**Когда менять:**
- Увеличить если фразы обрезаются на паузах между словами
- Уменьшить если ассистент слишком долго ждёт после окончания речи

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_silence_seconds`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_silence_seconds
data:
  value: 2.0
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 4. `command_seconds` — минимальная длительность команды

Минимальная длительность голосовой команды. Запись **не будет** остановлена,
пока не пройдёт это время **И** не истечёт таймер тишины.
Оба условия должны быть выполнены одновременно.

- **0.3-0.5с** — минимальная задержка, но можно обрезать короткие фразы
- **2.0с** (default) — защищает от случайных обрывов на коротких фразах
- **3.0-5.0с** — для длинных команд, но увеличивает задержку

**Когда менять:**
- Увеличить если короткие команды обрезаются
- Уменьшить если ассистент слишком долго ждёт обработки

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_command_seconds`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_command_seconds
data:
  value: 2.0
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 5. `vad_timeout_seconds` — таймаут записи

Максимальная длительность одной записи в секундах. Принудительная остановка.

- **10-15с** — для коротких команд ("включи свет")
- **30с** (default) — сбалансированный
- **30-60с** — для длинных диктовок

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_vad_timeout`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_vad_timeout
data:
  value: 15
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 6. `vad_mode` — режим VAD

| Значение | Описание |
|----------|----------|
| `singleton` | Одна модель на все pipeline. Экономит память. |
| `per_pipeline` | Отдельная модель (~10 МБ) на каждый pipeline. Изолирует pipeline. |

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_vad_mode`** → выберите target entity → выберите `singleton` или `per_pipeline` из выпадающего списка

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_vad_mode
data:
  value: per_pipeline
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 7. `before_command_timeout_seconds` — таймаут до начала команды

Максимальное время ожидания начала речи после активации ассистента.
Если за это время вероятность речи не превысила `before_command_speech_threshold`,
запись прерывается (`VAD BEFORE_COMMAND_TIMEOUT`) и ассистент возвращается в ожидание.

- **1.0-2.0с** — быстро прерывает при случайной активации, но требует быстрой реакции
- **4.0с** (default) — сбалансированный, даёт время на подготовку к речи
- **5.0-10.0с** — для случаев когда пользователь долго думает перед фразой

**Когда менять:**
- Увеличить если ассистент слишком быстро прерывается до того как вы начали говорить
- Уменьшить если при случайной активации долго висит в режиме прослушивания

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_before_command_timeout`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_before_command_timeout
data:
  value: 4.0
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 8. `noise_suppression_level` — шумоподавление

Уровень шумоподавления Speex DSP. Убирает фоновый шум из аудио перед отправкой
на распознавание речи и VAD. Обрабатывает каждый 10ms чанк.

- **0** (default) — выключено
- **1** — минимальное, убирает лёгкий шум
- **2** — среднее, подходит для большинства случаев
- **3** — высокое, для шумных помещений
- **4** — максимальное, может подавлять тихую речь

**Когда менять:**
- Включить (1-2) если фоновый шум мешает распознаванию
- Уменьшить если тихая речь обрезается

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_noise_suppression`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_noise_suppression
data:
  value: 2
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

#### 9. `auto_gain_dbfs` — автоматическое усиление

Автоматическое усиление сигнала в дБFS. Полезно если микрофон спутника тихий.
Speex DSP автоматически поднимает уровень громкости до целевого значения.

- **0** (default) — выключено
- **10-15** — умеренное усиление для тихих микрофонов
- **20-25** — сильное усиление
- **26-31** — максимальное, возможны искажения

**Когда менять:**
- Увеличить если STT плохо распознаёт тихую речь
- Уменьшить если появляются искажения или артефакты

**Как изменить:**

Вариант 1 — через сервис в UI: **Developer Tools → Services → `assist_satellite.set_auto_gain`** → выберите target entity → установите значение ползунком

Вариант 2 — через YAML:
```yaml
service: assist_satellite.set_auto_gain
data:
  value: 15
target:
  entity_id: assist_satellite.gg_voice_tdm_none
```

---

## Просмотр текущих значений

**Developer Tools → States** → найти сущность спутника (например `assist_satellite.gg_voice_tdm_none`) → атрибуты в карточке.

**Developer Tools → Template:**
```jinja2
speech_threshold: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'speech_threshold') }}
before_command_speech_threshold: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'before_command_speech_threshold') }}
silence_seconds: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'silence_seconds') }}
command_seconds: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'command_seconds') }}
vad_timeout_seconds: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'vad_timeout_seconds') }}
vad_mode: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'vad_mode') }}
before_command_timeout_seconds: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'before_command_timeout_seconds') }}
noise_suppression_level: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'noise_suppression_level') }}
auto_gain_dbfs: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'auto_gain_dbfs') }}
volume_multiplier: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'volume_multiplier') }}
trigger_timeout_seconds: {{ state_attr('assist_satellite.gg_voice_tdm_none', 'trigger_timeout_seconds') }}
```

---

## Как работают параметры вместе

`VoiceCommandSegmenter` — state machine с тремя фазами:

### Фаза 1: Ожидание начала команды (`in_command=False`)

```
Аудио → Silero prob
  _before_command_timeout_left -= 10ms
  если _before_command_timeout_left <= 0:
      → BEFORE_COMMAND_TIMEOUT (прерывание, нет речи)

  prob > before_command_speech_threshold? ─── ДА → _speech_seconds_left -= 10ms
                                                      если _speech_seconds_left <= 0:
                                                          → COMMAND_START (фаза 2)
  prob <= before_command_speech_threshold? ─ НЕТ → _reset_seconds_left -= 10ms
                                                      если _reset_seconds_left <= 0:
                                                          сброс счётчика речи
```

- `speech_seconds` (зашито 0.3с) — сколько непрерывной речи нужно для старта
- `before_command_timeout_seconds` (default 4.0с) — максимальное ожидание до старта команды

### Фаза 2: Запись команды (`in_command=True`)

```
prob > speech_threshold? ─── ДА (речь) → _command_seconds_left -= 10ms
                                            _reset_seconds_left -= 10ms
                                            если _reset_seconds_left <= 0:
                                                silence_seconds СБРОШЕН

prob <= speech_threshold? ─ НЕТ (тишина) → _silence_seconds_left -= 10ms
                                             _command_seconds_left -= 10ms
                                             если ОБА <= 0:
                                                 → SILENCE_FINISH
```

- `reset_seconds` (зашито 1.0с) — через сколько непрерывной речи silence counter сбрасывается
- Запись заканчивается когда **ОБА** условия выполнены: silence countdown И command duration прошли

### Фаза 3: Таймаут

Если `_timeout_seconds_left <= 0` → принудительный обрыв. Счётчик уменьшается на каждый чанк.

---

## Персистентность

Настройки хранятся в SQLite БД: `/config/assist_satellite_settings/vad_settings.db`

**Приоритет:** SQLite (per-satellite) > `conf_assist_pipeline.yaml` (defaults) > `HARDCODED_DEFAULTS` (code)

- `vad_settings_db.py` — `VadSettingsStore`, автоматические ALTER TABLE миграции
- `conf_assist_pipeline.yaml` — централизованные дефолты для всех спутников
- Настройки загружаются в `entity.py:async_added_to_hass()` из БД
- Сохраняются через `_save_param()` при изменении через сервисы

---

## Константы (зашиты в код)

| Константа | Значение | Файл | Описание |
|-----------|----------|------|----------|
| `SAMPLE_RATE` | 16000 | `const.py` | Частота дискретизации |
| `SAMPLE_WIDTH` | 2 байта | `const.py` | 16-bit PCM |
| `MS_PER_CHUNK` | 10ms | `const.py` | Размер чанка в pipeline |
| `SILERO_MS_PER_CHUNK` | 32ms | `const.py` | Размер чанка для Silero |
| `SILERO_SAMPLES_PER_CHUNK` | 512 | `const.py` | Сэмплов на чанк Silero (минимум 512) |
| `SILERO_BYTES_PER_CHUNK` | 1024 | `const.py` | Байт на чанк Silero |
| `speech_seconds` | 0.3с | `vad.py` | Сколько речи для старта команды |
| `reset_seconds` | 1.0с | `vad.py` | Через сколько речи silence counter сбрасывается |

---

## Рекомендуемые профили

### Тихая комната, чёткая речь (текущий дефолт)
```yaml
speech_threshold: 0.5
before_command_speech_threshold: 0.5
silence_seconds: 2.0
command_seconds: 1.5
vad_timeout_seconds: 30
vad_mode: per_pipeline
before_command_timeout_seconds: 5.0
noise_suppression_level: 0
auto_gain_dbfs: 0
volume_multiplier: 1.0
trigger_timeout_seconds: 7.0
```

### Шумное помещение
```yaml
speech_threshold: 0.6
before_command_speech_threshold: 0.3
silence_seconds: 1.0
command_seconds: 1.0
vad_timeout_seconds: 15
vad_mode: per_pipeline
before_command_timeout_seconds: 3.0
noise_suppression_level: 2
auto_gain_dbfs: 0
volume_multiplier: 1.0
trigger_timeout_seconds: 5.0
```

### Длинные диктовки
```yaml
speech_threshold: 0.4
before_command_speech_threshold: 0.15
silence_seconds: 3.0
command_seconds: 3.0
vad_timeout_seconds: 60
vad_mode: per_pipeline
before_command_timeout_seconds: 6.0
noise_suppression_level: 0
auto_gain_dbfs: 0
volume_multiplier: 1.0
trigger_timeout_seconds: 15.0
```

### Тихий микрофон
```yaml
speech_threshold: 0.4
before_command_speech_threshold: 0.2
silence_seconds: 2.0
command_seconds: 2.0
vad_timeout_seconds: 30
vad_mode: per_pipeline
before_command_timeout_seconds: 4.0
noise_suppression_level: 1
auto_gain_dbfs: 15
volume_multiplier: 2.0
trigger_timeout_seconds: 7.0
```

---

## Устранение неполадок

### Ассистент обрезает фразы на паузах

1. Увеличить `silence_seconds` (попробовать 3.0)
2. Увеличить `command_seconds` (попробовать 3.0)

**Developer Tools → Services → `assist_satellite.set_silence_seconds`** → установите 3.0

### Ассистент не реагирует на тихую речь

1. Уменьшить `speech_threshold` (попробовать 0.3)
2. Уменьшить `before_command_speech_threshold` (попробовать 0.1)

**Developer Tools → Services → `assist_satellite.set_speech_threshold`** → установите 0.3

### Ассистент не обрывает запись (фоновый шум)

1. Увеличить `speech_threshold` (попробовать 0.6-0.7)
2. Уменьшить `silence_seconds` (попробовать 1.0)

**Developer Tools → Services → `assist_satellite.set_speech_threshold`** → установите 0.7

### Ассистент висит после активации, если молчать

1. Уменьшить `before_command_timeout_seconds` (попробовать 2.0-3.0)
2. Это время ожидания речи после нажатия кнопки/активации

**Developer Tools → Services → `assist_satellite.set_before_command_timeout`** → установите 3.0

### Ассистент сразу обрывает (prob = -1.0 или ошибка)

Проверить:
1. Silero singleton загружен: `docker logs ha-test 2>&1 | Select-String "Silero VAD model loaded"`
2. `__pycache__` очищен: `docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__`
3. torch установлен: `docker exec ha-test python3 -c "import torch; print(torch.__version__)"`

### Модель ONNX не работает

**Известная проблема:** `silero_vad.onnx` через `onnxruntime` всегда возвращает ~0.0005
независимо от входного сигнала. Это баг взаимодействия onnxruntime + модель Silero.
**Решение:** Используйте torch JIT модель (`silero_vad.jit`) — она работает корректно.

### Ошибка `NameError: name 'gain' is not defined`

Старый `__pycache__`. Очистить:
```bash
docker exec ha-test rm -rf /workspaces/core/homeassistant/components/assist_pipeline/__pycache__
docker restart ha-test
```

---

## Отладочное логирование

В коде оставлены WARNING-логи для отладки. Они показывают:

| Лог | Файл | Описание |
|-----|------|----------|
| `Silero prob=X.XXXX chunk#N` | `audio_enhancer.py` | Вероятность речи на каждом 32ms чанке |
| `STT_VAD chunk #N: prob=X.XXX` | `pipeline.py` | Каждые 50 чанков в pipeline |
| `STT_VAD: SPEECH START at chunk #N` | `pipeline.py` | Начало голосовой команды |
| `STT_VAD: SILENCE detected at chunk #N` | `pipeline.py` | Конец голосовой команды |
| `VAD COMMAND_START: sil_sec=X cmd_sec=X threshold=X` | `vad.py` | Параметры при старте команды |
| `VAD SILENCE_FINISH: sil_left=X cmd_left=X prob=X` | `vad.py` | Счётчики при обрыве |
| `VAD TIMEOUT after Xs` | `vad.py` | Таймаут записи |
| `VAD BEFORE_COMMAND_TIMEOUT after Xs` | `vad.py` | Таймаут до начала команды (нет речи после активации) |
| `PipelineRun: creating audio enhancer, vad_mode=X` | `pipeline.py` | Создание enhancer с параметрами |

**Для отключения** — заменить `_LOGGER.warning` на `_LOGGER.debug` в соответствующих файлах.

---

## Структура файлов

```
homeassistant/components/
├── assist_pipeline/
│   ├── __init__.py              # Инициализация, загрузка singleton Silero
│   ├── const.py                 # DATA_SILERO_VAD, SILERO_* константы
│   ├── silero_vad_manager.py    # SileroVadSingleton, SileroVadStream, SileroVadPerPipeline
│   ├── audio_enhancer.py        # SileroVadSpeexEnhancer (буферизация 10ms→32ms, Speex)
│   ├── vad.py                   # VoiceCommandSegmenter (state machine)
│   ├── pipeline.py              # AudioSettings (12 параметров), _check_trigger(), RecognitionLogger
│   ├── recognition_log.py       # RecognitionLogger — лог распознаваний, gzip ротация
│   └── silero_vad.onnx          # ONNX модель (НЕ ИСПОЛЬЗУЕТСЯ — сломана)
│
├── assist_satellite/
│   ├── __init__.py              # Регистрация 11 сервисов VAD
│   ├── entity.py                # AssistSatelliteEntity с 12 атрибутами, SQLite load/save
│   ├── services.yaml            # Определения сервисов (русские описания, ползунки)
│   └── vad_settings_db.py       # VadSettingsStore (SQLite), HARDCODED_DEFAULTS, YAML defaults
│
├── esphome/
│   └── assist_satellite.py      # ESPHome satellite entity, _resolve_audio_settings() fallback
│
└── wyoming/
    └── stt.py                   # _batch_process() — отправка аудио, чтение Transcript

config/ (volume mount)
├── conf_assist_pipeline.yaml    # Централизованные дефолты для параметров VAD
├── assist_satellite_settings/
│   └── vad_settings.db          # SQLite — настройки по каждому спутнику
└── assist_pipeline/
    └── recognition_log.txt      # Лог всех распознаваний (gzip ротация >100MB)

wyoming-onnxasr/
└── server.py                    # GigaAM v3 RNN-T int8, batch режим
```
