# Кнопки Прослушивание / Запись у каждого ассистента

Custom component `satellite_audio`: живое прослушивание комнаты и диктофон
с микрофона любой ESPHome-спутницы, **без прошивки плат**.

## Возможности

- **Прослушивание** — вкл → живой аудиопоток с платы (браузер/телефон),
  выкл → стоп. Пока включено — голосовые команды платы не работают
  (микрофон занят), объявления продолжают работать.
- **Запись** — вкл → поток пишется в WAV на сервере HA, выкл → файл
  финализируется. Формат 16 кГц / 16 бит / mono, 32 КБ/с (~115 МБ/час).
- Обе кнопки одновременно — один стрим раздаётся в эфир и в файл.
- **Поле «Микрофон»** — все коэффициенты ассистента одним текстом (см. ниже).

## Где кнопки

- **Страница устройства платы в HA** — сущности «…Прослушивание» и «…Запись»
  привязаны к устройству платы (device_id через entity_registry).
- **Глобус в шапке страницы устройства** (`configuration_url`) — открывает
  персональную панель платы `panel?satellite=<entity_id>`. Компонент
  переутверждает ссылку при каждом reconcile (esphome может затирать).
- **Панель быстрого доступа** — `http://<ha>:8123/api/satellite_audio/panel`:
  список всех спутников, кнопки Слушать/Записать, WebAudio-плеер (WebSocket,
  работает в любом браузере и на телефоне), индикатор уровня.
  С параметром `?satellite=<assist_satellite entity_id>` — карточка одной
  платы (для флота из ~196 плат использовать только персональные ссылки).
- **Встроенный медиа-браузер HA** («Медиа» в сайдбаре) → «Спутники — аудио»:
  все записи платы (кликабельны, играют встроенным плеером HA в браузере,
  с поддержкой перемотки) + пункт «Живой эфир» пока прослушивание включено.
- **Сервисы** (для автоматизаций): `satellite_audio.listen` /
  `satellite_audio.record` с полями `entity_id`, `enabled`.

Атрибуты сущности Запись: путь к текущему/последнему файлу и длительность.
Папка записей: `/config/recordings/<имя платы>/<дата-время>.wav`.
Файлы раздаются через `/api/satellite_audio/file/<плата>/<файл>`
(aiohttp FileResponse — Range-запросы = перемотка).

## Поля «Микрофон» (коэффициенты — каждое отдельно)

На странице устройства платы (категория «Настройки») — 11 number-полей
+ 1 select. Каждое поле: свой диапазон, имя с пояснением, атрибут
`comment` с полным описанием, атрибут `firmware` с коэффициентами прошивки.

| Поле | Диапазон | Атрибут ассистента | Сервис |
|------|----------|--------------------|--------|
| Шумоподавление 0-4 | 0–4 | noise_suppression_level | set_noise_suppression |
| Автоусиление дБ 0-31 | 0–31 | auto_gain_dbfs | set_auto_gain |
| Множитель громкости 0.1-30 | 0.1–30 | volume_multiplier | set_volume_multiplier |
| VAD порог речи | 0.1–1 | speech_threshold | set_speech_threshold |
| VAD порог начала речи | 0.05–0.9 | before_command_speech_threshold | set_before_command_speech_threshold |
| VAD тишина до обрыва с | 0.3–10 | silence_seconds | set_silence_seconds |
| VAD мин. команда с | 0.3–10 | command_seconds | set_command_seconds |
| VAD макс. запись с | 1–60 | vad_timeout_seconds | set_vad_timeout |
| VAD ожидание речи с | 1–30 | before_command_timeout_seconds | set_before_command_timeout |
| VAD таймаут триггера с | 1–30 | trigger_timeout_seconds | set_trigger_timeout |
| VAD режим | singleton / per_pipeline | vad_mode | set_vad_mode |

Ввод значения → сервис форка `assist_satellite.set_*`. Значения live —
обновляются от атрибутов ассистента (подписка на state changes).
Хранение — как у форка: SQLite `assist_satellite_settings/vad_settings.db`
per-satellite, дефолты `conf_assist_pipeline.yaml`.

API: `POST /api/satellite_audio/control` c `action=mic, value=<k=v …>`
(короткие или полные имена ключей), текущие значения — в
`GET /api/satellite_audio/status` (блок `mic`).

## Коэффициенты прошивки (read-only)

`gain_factor` (VA/mww), `mic_gain` кодека, `probability_cutoff`, прошивные
`ns/ag/vol` **не передаются по API** —ESPHome их применяет на устройстве.
Runtime-действий в esphome-cx нет (только компиляция). Для видимости в HA
в test-board.yaml добавлен text_sensor «Микрофон прошивка» (значение —
статическая строка `mic_gain=… va_gain=… mww_gain=… prob=… fw_ns=… fw_ag=… fw_vol=…`),
компонент подтягивает его в атрибут `firmware` поля «Микрофон».
Изменение = правка yaml + перепрошивка (сценарий ниже).

### Прошивка платы через сервер (OTA через reverse-туннель)

Плата на LAN 192.168.0.x недостижима с сервера. Рабочая цепочка:

```
1. Правка /home/green/esphome-cx/config/test-board.yaml (на сервере)
2. docker exec esphome-cx esphome compile test-board.yaml
3. На локальной машине: ssh -p 8222 -N -R 127.0.0.1:33232:192.168.0.115:3232 root@95.104.236.85
4. На сервере: socat TCP-LISTEN:3232,bind=172.18.0.1,fork,reuseaddr TCP:127.0.0.1:33232 &
   (172.18.0.1 — gateway docker-сети esphome-cx; port платы — 3232, esp-idf OTA)
5. docker exec esphome-cx esphome upload test-board.yaml --device 172.18.0.1
6. Убить socat и туннель
```

## Архитектура (итоговая, v3)

Перепробованы три варианта; финальный работает без churн-циклов:

```
Триггер разговора: client.send_voice_assistant_announcement_await_response(
    silent.wav, start_conversation=True)   ← анонс на volume 0 (media_player)
  → плата начинает стрим микрофона в HA (тот же API-коннект esphome-интеграции)

Перехват аудио: entity._audio_queue заменяется на _TeeQueue
  → сессия (writer + live-очереди) + реальная очередь (пока её читает pipeline)

Удержание стрима: client.send_voice_assistant_event обёрнут — терминальные
  события (RUN_END / ERROR / STT_VAD_END) сбрасываются, пока сессия активна
  → HA-pipeline умирает по VAD-таймауту, но плата не узнаёт и стримит дальше

Монитор: фоновая задача, 4с без чанков → перезапуск разговора (до 8 попыток)

Стоп: восстановить очередь и send_voice_assistant_event, отправить RUN_END
  → плата завершает разговор, on_end в yaml перезапускает micro_wake_word
```

### Почему нельзя проще (найдено в отладке)

- `subscribe_voice_assistant` **не запускает** стрим — только регистрирует
  хоста; разговор стартует только VoiceAssistantRequest от платы
  (wake word) или анонсом с `start_conversation=True`.
- Сущность EsphomeAssistSatellite держит свою подписку **вечно**
  (bound-методы захвачены при `async_added_to_hass`) — обёртка методов
  сущности не работает. Динамические точки: `entity._audio_queue`
  и `self.cli.send_voice_assistant_event`.
- Если не кормить pipeline аудио — его 3с stall-watchdog рвёт весь
  API-коннект (форк, Phase 1). TeeQueue кормит реальную очередь,
  пока есть потребитель.
- Если НЕ гасить терминальные события — pipeline умирает за 5с
  (BEFORE_COMMAND_TIMEOUT) / 30с (wall clock), плата завершает разговор
  → churn-циклы по ~6с (наблюдалось в первой реализации).
- Дедуп wake-word в форке (WAKE_WORD_COOLDOWN=2с) ронял частые
  перезапуски с ошибкой duplicate_wake_up_detected.

### Тихий триггер

Анонс-заглушка `/api/satellite_audio/silent.wav` (0.1с тишины) + на время
анонса `media_player.volume_set(0)` с восстановлением. При включении
возможен тихий щелчокopening/closing аудиотракта DAC.

## Верификация (ha-test + ec4-test-board @ 192.168.0.115)

- [x] Сущности создаются и привязаны к устройству платы
- [x] Прослушка: 32 0748 байт за 10с (полные 32 КБ/с), стабильна >30с
- [x] Запись 8.2с → 264 КБ WAV, поверх активной прослушки
- [x] Выключение: состояние off стабильное, RUN_END уходит
- [x] Панель `/api/satellite_audio/panel` рендерится (+ режим ?satellite=)
- [x] Поля «Микрофон»: 11 number + 1 select созданы и привязаны к устройству;
  ns=2 speech=0.5 → атрибуты ассистента обновились; сброс OK
- [x] Глобус: configuration_url → panel?satellite=… на устройстве платы
- [x] Прошивка: text_sensor «Микрофон прошивка» виден, атрибут firmware
  поля «Микрофон» = mic_gain=-38.0 va_gain=1 mww_gain=2 prob=0.8 …
- [ ] E2E руками: wake word после выключения (у юзера)
- [ ] Тихий триггер: не слышно ли аудио при включении (у юзера)
- [ ] E2E руками: поле «Микрофон» в UI (у юзера)

## Исходники и деплой

- Репо: `satellite_audio/` (source of truth, коммитится в git).
- Рантайм: копия в `config/custom_components/satellite_audio/`.
- Включение: секция `satellite_audio:` в configuration.yaml.
- Требует esphome-интеграцию с загруженным entry платы
  (aioesphomeapi 46.2.0).

## Безопасность (пилот!)

- HTTP-эндпоинты `/api/satellite_audio/*` без авторизации, доступ из LAN.
  Живой стрим защищён ротируемым токеном в URL.
- Для прода: перевести requires_auth=True + signed paths, либо оставить
  только сущности/сервисы (панель выключить).

## Ограничения

- Перезапуск HA во время записи оставляет WAV с нулевыми размерами в
  заголовке (VLC читает; чинится `ffmpeg -i in.wav out.wav`).
- Латентность живого стрима в браузере ~1–2с (буферизация `<audio>`).
- Во время сессии первые ~5с аудио уходит в HA-pipeline (trigger check
  фильтрует случайные команды); после его смерти — только в сессию.
- Если юзер скажет wake word в момент старта сессии — возможен конфликт
  (watchdog откатит и включение можно повторить).

## Статус

- [x] Пилот на локальном стенде (ha-test + ec4-test-board @ 192.168.0.115)
- [ ] Прод-роллаут (копия компонента + configuration.yaml на сервер)
