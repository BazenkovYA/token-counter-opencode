# Подключение OpenCode

Маршрут до подключения:

```text
OpenCode → существующий LiteLLM gateway
```

После подключения:

```text
OpenCode → http://127.0.0.1:8001/v1 → тот же LiteLLM gateway
```

В `~/.config/opencode/opencode.json` меняются только два поля провайдера
`bifrost-litellm`: `baseURL` указывает на локальный proxy, а `apiKey` — на
`runtime/opencode_litellm/client.key` текущего клона. Добавляется
`~/.config/opencode/plugins/token-counter.js`. Модели, лимиты, MCP и остальные
поля сохраняются.

Gateway key остаётся в существующей переменной `LITELLM_API_KEY`. В env счётчика
хранится ссылка `{env:LITELLM_API_KEY}`, а не значение секрета.

## Применение

```powershell
.\.venv\Scripts\python.exe scripts\connect_opencode.py prepare
.\start.ps1
.\.venv\Scripts\python.exe scripts\connect_opencode.py apply
```

`prepare` валидирует provider, положительные лимиты и ссылки на ключи, затем
создаёт в runtime реестр моделей этой установки. `apply` повторно проверяет SHA-256
исходного конфига, здоровье сервиса и создаёт защищённую резервную копию перед
изменением. При изменившемся конфиге план нужно подготовить заново.

После применения полностью перезапустите OpenCode Desktop. Отправьте один короткий
запрос и проверьте его Session ID, модель и usage в панели. Effective URL выбранного
provider должен быть `http://127.0.0.1:8001/v1`; глобальные или проектные настройки
OpenCode могут перекрывать основной конфиг.

## Откат

```powershell
.\.venv\Scripts\python.exe scripts\connect_opencode.py rollback
```

Откат восстанавливает исходные `baseURL` и `apiKey`, удаляет только неизменённый
плагин счётчика и сохраняет позднейшие правки моделей/MCP. При конфликте с ручной
правкой операция прекращается. После отката перезапустите OpenCode. База usage и
резервная копия остаются в runtime.

Если OpenCode подключён к проверенной другой копии, новый установщик может перейти
без предварительного rollback: он сверяет прежние connection-plan/env/plugin,
повторно использует локальный client key, обновляет проверенный plugin до версии
нового клона и меняет только путь `{file:...}`. Для rollback сохраняется предыдущая
версия plugin. Если хотя бы одна проверка не проходит, автоматический переход
прекращается. Не удаляйте прежний каталог, пока новый doctor, запуск и
пользовательский запрос не пройдут.
