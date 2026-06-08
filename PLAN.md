# План доработки: Keyword Subsequence Matching для Sentence Triggers

## Проблема

Голосовой ассистент Home Assistant использует hassil для matching триггерных фраз.
hassil требует **полного совпадения** текста с начала до конца (full-text matching).

Если пользователь говорит: "я думаю сделать вызови машину лифт и потом продать ее",
а автоматизация настроена на триггер "вызови лифт" — срабатывания НЕ будет,
потому что "вызови" не стоит в начале строки.

## Решение: Keyword Subsequence Pre-filter

Добавить метод `_keyword_match_triggers()` в `default_agent.py`, который ПЕРЕД hassil
проверяет: все ли слова триггерной фразы встречаются во входном тексте **в том же порядке**
(подпоследовательность). Допускаются любые слова между триггерными.

### Примеры

| Ввод | Триггер "вызови лифт" | Результат |
|---|---|---|
| "вызови лифт" | точное совпадение | Match |
| "я думаю сделать вызови собака лифт потом продать" | вызови...лифт | Match |
| "лифт вызови" | порядок нарушен | No (fallback на hassil) |
| "я хочу купить машину" | слов нет | No |

## Где правим

**Один файл:** `homeassistant/components/conversation/default_agent.py`

### Изменение 1: Новый метод `_keyword_match_triggers()`

Добавить после метода `_rebuild_trigger_intents()` (строка ~1490):

```python
def _keyword_match_triggers(
    self, user_input: ConversationInput
) -> SentenceTriggerResult | None:
    """Pre-filter: ordered keyword subsequence matching for trigger phrases."""
    normalized_text = remove_punctuation(user_input.text).strip().lower().split()

    for trigger_id, trigger_details in enumerate(self._triggers_details):
        for sentence in trigger_details.sentences:
            trigger_words = remove_punctuation(sentence).strip().lower().split()
            if not trigger_words:
                continue

            idx = 0
            for word in normalized_text:
                if idx < len(trigger_words) and word == trigger_words[idx]:
                    idx += 1

            if idx == len(trigger_words):
                result = RecognizeResult(
                    intent=Intent(name=str(trigger_id)),
                    intent_data=IntentData(sentence_texts=[sentence]),
                )
                return SentenceTriggerResult(
                    sentence=user_input.text,
                    sentence_template=sentence,
                    matched_triggers={trigger_id: result},
                )

    return None
```

### Изменение 2: Вызов pre-filter в `async_recognize_sentence_trigger()`

Перед строкой `assert self._trigger_intents is not None` (строка ~1508) добавить:

```python
# Keyword subsequence pre-filter
if keyword_result := self._keyword_match_triggers(user_input):
    _LOGGER.debug(
        "Keyword match for '%s': %s",
        user_input.text,
        list(keyword_result.matched_triggers),
    )
    return keyword_result
```

## Цепочка выполнения (напоминалка)

```
STT транскрипт
  → pipeline.py: PipelineRun.recognize_intent()
    → default_agent.py: _async_handle_message()
      → async_recognize_sentence_trigger()
        → [НОВОЕ] _keyword_match_triggers()  ← pre-filter
        → hassil recognize_all()              ← existing full-text matching
      → _handle_trigger_result()
        → trigger.py: call_action()
          → Автоматизация запускается
```

## Шаги

1. [x] Клонировать репозиторий
2. [x] Собрать Docker-образ
3. [x] Запуск контейнера с volume mount
4. [ ] Правка default_agent.py
5. [ ] Тестирование
6. [ ] Пуш в ветку dev

## Тест-кейсы

Создать автоматизацию:
```yaml
trigger:
  - platform: conversation
    command: "вызови лифт"
action:
  - service: notify.persistent_notification
    data:
      message: "Триггер сработал: {{ trigger.sentence }}"
```

Проверить:
- [ ] "вызови лифт" → срабатывает
- [ ] "я думаю сделать вызови собака лифт потом продать" → срабатывает
- [ ] "я хочу купить машину" → НЕ срабатывает
- [ ] "лифт вызови" → НЕ срабатывает (порядок нарушен)
