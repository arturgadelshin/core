#!/bin/bash

# Путь к русскому файлу intents
RU_JSON_PATH="/opt/venv/lib/python3.14/site-packages/home_assistant_intents/data/ru.json"

echo "Обновление файла intents для русского языка..."

# Заменяем "no_intent": "Обращение не распознано" на "no_intent": ""
if [[ -f "$RU_JSON_PATH" ]]; then
    echo "Найден $RU_JSON_PATH"
    echo 'Заменяю "no_intent": "Обращение не распознано" на "no_intent": ""'
    sed -i "s/\"no_intent\": \"Обращение не распознано\"/\"no_intent\": \"\"/g" "$RU_JSON_PATH"
    echo "Файл $RU_JSON_PATH обновлен."
else
    echo "ОШИБКА: Файл $RU_JSON_PATH не найден."
    echo "Проверка доступных файлов intents..."
    ls -la /opt/venv/lib/python3.14/site-packages/home_assistant_intents/data/
fi

echo "Готово."