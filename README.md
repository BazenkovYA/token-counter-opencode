# Token Counter для OpenCode

Локальный HTTP-прокси для OpenCode, который передаёт запросы существующему
LiteLLM gateway, сохраняет числовой `usage` в SQLite и показывает состояние
контекста в русскоязычной панели.

Рабочая панель доступна без формы входа на `http://127.0.0.1:8001`. Сервис жёстко
привязан к loopback и проверяет Host/Origin. Клиентский ключ защищает `/v1`,
административный ключ используется только для управления процессом. Runtime,
ключи, пользовательские настройки OpenCode и история обращений не входят в Git.

## Установка из любого клона

Нужен Python 3.12+. Все внутренние пути вычисляются от корня репозитория.

```powershell
# Windows: установка, запуск и обратимое подключение OpenCode
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ConnectOpenCode

# Добавьте -Dev, чтобы установить тестовые зависимости
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ConnectOpenCode -Dev

# Только подготовка, без изменения OpenCode
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoStart
```

```bash
# Linux/macOS
bash scripts/install.sh --connect-opencode
```

`-ConnectOpenCode` / `--connect-opencode` разрешает установщику изменить только
`baseURL` и `apiKey` провайдера `bifrost-litellm` и установить Session ID plugin.
Перед изменением создаётся резервная копия. Модели, лимиты, MCP и остальные поля
OpenCode сохраняются. Установщик не выполняет генерации и не меняет gateway.
Если OpenCode уже подключён к проверенной предыдущей копии, установщик валидирует
её plan/env/plugin, переносит upstream-ссылку и сохраняет прежний локальный client
key. Поэтому открытый OpenCode не получает 401 во время перехода между клонами.

Для безопасного просмотра интерфейса на синтетических данных:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Demo
```

## Управление

```powershell
.\start.ps1
.\status.ps1
.\stop.ps1
```

Сервис работает в фоне. `stop` проверяет PID, время создания, командную строку,
каталог и HTTP nonce, поэтому не завершает чужой процесс. Остановка счётчика
прерывает доступ OpenCode к gateway до повторного запуска или отката подключения.

Ручное подключение и откат описаны в [docs/connection.md](docs/connection.md).
Промпт для установки ИИ-агентом находится в
[INSTALL_WITH_AI.md](INSTALL_WITH_AI.md).

## Как устроен учёт

Каждый вызов `/v1/chat/completions` создаёт одно итоговое событие. Прокси не
сохраняет тексты запросов, ответов, tool results или reasoning. В SQLite попадают
числовой `usage`, модель, служебные ID и состояние вызова. SSE передаётся клиенту
по мере поступления.

Для `P` prompt tokens, `C` completion tokens и лимита модели `L` панель показывает:

- занято: `P + C`;
- остаток: `max(0, L - P - C)`;
- заполнение: `100 * (P + C) / L`.

Отсутствующий `usage` остаётся неизвестным и показывается как `—`. Reasoning и
cache details не прибавляются повторно. Gateway fallback делает лимит конкретного
события неизвестным. При подключении установщик создаёт runtime-реестр из моделей
и лимитов текущего OpenCode; `config/models.json` служит исходным примером и
реестром демонстрационного режима.

Плагин OpenCode передаёт Session ID, message ID, agent и тип вызова. Названия
сессий читаются отдельно из OpenCode SQLite только для отображения; сообщения не
читаются. Без Session ID событие всё равно учитывается, но не группируется.

## API, данные и экспорт

Описание API: [docs/api.md](docs/api.md). База данных создаётся в
`runtime/opencode_litellm/data/usage.db`. Runtime полностью исключён из Git.

```powershell
.\.venv\Scripts\python.exe -m token_counter backup --output exports\usage.db
.\.venv\Scripts\python.exe -m token_counter export --output exports\session --filter session_id=SESSION_ID
.\.venv\Scripts\python.exe -m token_counter verify exports\session
```

Экспорт включает SQLite snapshot, CSV, JSONL, metadata, SHA-256 manifest и ZIP.
Существующий output намеренно не перезаписывается.

## Проверка

```powershell
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe -m pytest -q
node --test integrations/opencode/token-counter.test.js
```

Тесты используют mock upstream и не выполняют платные запросы. После подключения
полностью перезапустите OpenCode, отправьте один новый короткий запрос и убедитесь,
что в панели появилась одна запись с ожидаемым Session ID.

Подготовка проекта к внешнему remote описана в
[docs/repository.md](docs/repository.md). Актуальные требования находятся в
[docs/specification/OPENCODE_ONLY_REQUIREMENTS.md](docs/specification/OPENCODE_ONLY_REQUIREMENTS.md).
