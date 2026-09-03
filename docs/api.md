# HTTP API

Base URL: `http://127.0.0.1:8001`. Панель и `/api/*` доступны без входа, потому
что сервис слушает только loopback и проверяет Host/Origin. `/v1/*` требует
локальный client key, `/internal/*` — admin key. Отсутствующий admin key даёт 403.

| Метод и путь | Доступ | Назначение |
|---|---|---|
| `GET /health` | Локальный | Минимальная готовность процесса |
| `GET /api/health` | Локальный | БД, upstream, очередь и потери |
| `GET /api/models` | Локальный | Aliases, лимиты и статусы проверки |
| `GET /api/stats` | Локальный | Totals, current, recent, by_model, active |
| `GET /api/stats/latest` | Локальный | Последний релевантный вызов сессии |
| `GET /api/history` | Локальный | События с пагинацией снимка |
| `GET /api/sessions` | Локальный | Метаданные сессий OpenCode |
| `POST /api/export` | Локальный | ZIP с SQLite, CSV, JSONL и manifest |
| `GET /v1/models` | Client key | Прозрачный запрос к upstream `/models` |
| `POST /v1/chat/completions` | Client key | JSON/SSE proxy с учётом usage |
| `POST /internal/shutdown` | Admin key | Штатная остановка процесса |

Неподдерживаемые OpenAI API возвращают 501. `n` допускается только 1. Максимум
JSON body — 32 MiB. Для stream proxy добавляет `stream_options.include_usage=true`
с сохранением прочих параметров.

## Фильтры и пагинация

Основные фильтры: `provider_id`, `requested_model`, `client_instance_id`,
`conversation_id`, `session_id`, `project_id`, `request_kind`, `request_status`,
`usage_status`, `context_limit_status`, `context_limit`, `from`, `to`.

`GET /api/stats/latest?session_id=...` требует ID. История принимает `limit=1..200`
и возвращает непрозрачный `next_cursor`, связанный с фильтрами и верхней границей
снимка. Между страницами фильтры менять нельзя.

Пример экспорта:

```json
{"filters":{"session_id":"ses_example"},"include_titles":false}
```

## Заголовки OpenCode plugin

| Заголовок | Смысл |
|---|---|
| `X-Token-Counter-Client` | Всегда `opencode` |
| `X-Token-Counter-Instance-Id` | ID установки OpenCode |
| `X-Token-Counter-Session-Id` | Session ID |
| `X-Token-Counter-Message-Id` | Связь HTTP-шагов с сообщением |
| `X-Token-Counter-Parent-Session-Id` | Родительская сессия |
| `X-Token-Counter-Project-Id` | Проект, если известен |
| `X-Token-Counter-Agent` | Агент OpenCode |
| `X-Token-Counter-Request-Kind` | `main`, `auxiliary`, `compaction`, `unknown` |
| `X-Request-Id` | Внешний request ID, не ключ дедупликации |

Строки ограничены 256 символами и не допускают управляющие символы. Отсутствие
Session ID означает отсутствие группировки, но событие сохраняется.

## Приватность и ошибки

Прокси передаёт upstream HTTP status и безопасные rate-limit headers, не следует
redirect и не повторяет POST. Обрыв SSE не превращается в успешный ответ. Тексты,
reasoning и tool results не сохраняются. Чужие Cookie, Authorization и внутренние
заголовки не пересылаются. Интерфейс использует локальные assets и CSP без CDN.
