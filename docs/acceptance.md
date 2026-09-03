# Результаты приёмки переносимой сборки

Дата первой живой проверки: 03.09.2026. Указанный при проверке путь не является
частью конфигурации: сборка вычисляет корень автоматически.

| Проверка | Статус | Результат |
|---|---|---|
| Python unit/integration | PASS | 66 tests: JSON/SSE, SQLite, экспорт, процессы и миграция clone |
| OpenCode plugin | PASS | 4 tests: Session ID, тип вызова и loopback route guard |
| Windows installer | PASS | Корень вычисляется автоматически |
| Произвольный корень с пробелами | PASS | Внутренние env-пути относительные |
| Реальный OpenCode → proxy → LiteLLM | PASS | Пользователь подтвердил появление событий |
| Отсутствие потерь | PASS | Health показывал нулевые потери записи |
| Shell installer syntax | PASS | `bash -n scripts/install.sh` |
| Linux/macOS полный lifecycle | NOT TESTED | На первой машине не выполнялся |
| Публикация remote | NOT TESTED | Remote намеренно не добавлен |

Автоматическая проверка текущей ревизии:

```text
python -m pytest -q
66 passed
node --test integrations/opencode/token-counter.test.js
4 passed
```

Runtime, ключи, OpenCode config, backup и usage.db не включаются в Git. Скриншоты
созданы на синтетических данных и не содержат переписки.
