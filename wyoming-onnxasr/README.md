# Wyoming ONNX ASR Server

Wyoming-протокол сервер для распознавания речи (STT) с использованием [onnx-asr](https://github.com/istupakov/onnx-asr).
Работает как backend для Home Assistant через интеграцию Wyoming Protocol.

## Архитектурные решения

### 1. Предзагрузка модели (startup preloading)

Модель загружается **один раз** при старте сервера в `main()` и передаётся во все хендлеры через `functools.partial`.
Это исключает задержку на загрузку модели при первом запросе каждого подключения.

```python
model = onnx_asr.load_model(args.model, quantization=args.quantization)
await server.run(partial(OnnxAsrEventHandler, model=model, model_name=args.model))
```

### 2. Асинхронное распознавание (non-blocking event loop)

`model.recognize()` — синхронная CPU-bound операция, которая блокирует asyncio event loop
на 0.3–1.5 секунды. Без выноса в отдельный поток сервер "зависает" и не может обрабатывать
новые подключения/события пока идёт распознавание.

Решение: запуск через `loop.run_in_executor()` в дефолтный `ThreadPoolExecutor`:

```python
async def _recognize(self) -> None:
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, self._sync_recognize, audio_np)

def _sync_recognize(self, audio_np: np.ndarray) -> str:
    return str(self._model.recognize(audio_np))
```

### 3. Поддержка Wyoming протокола

Сервер корректно отвечает на `Describe` → `Info` с описанием ASR-сервиса и моделей,
что необходимо Home Assistant для обнаружения и настройки интеграции.

## Модели

Поддерживаемые модели (из onnx-asr):
- `gigaam-v2-ctc` — быстрая, CTC-декодер
- `gigaam-v2-rnnt` — точнее, RNN-T декодер, медленнее
- `gigaam-v3-ctc` — быстрая, улучшенное качество
- `gigaam-v3-rnnt` — лучшая точность, RNN-T декодер

## Параметры

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--uri` | Адрес `tcp://host:port` | Обязательный |
| `--model` | Имя модели onnx-asr | Обязательный |
| `--device` | Устройство (cpu/cuda) | `cpu` |
| `--quantization` | Квантование (`int8`, `int4`, `fp16`) | Нет |

## Пример docker-compose

```yaml
whisper:
  build: ./wyoming-onnxasr
  ports:
    - "10301:10301"
  command: >
    --model gigaam-v3-rnnt --uri tcp://0.0.0.0:10301 --device cpu --quantization int8
  volumes:
    - ./model-cache:/root/.cache
```
