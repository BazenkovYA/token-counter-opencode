# Локальный Git и внешний репозиторий

Git хранит исходники, универсальные установщики, документацию, тесты, реестр
моделей и синтетические скриншоты. `.gitignore` исключает `.venv`, runtime,
`.env`, ключи, SQLite/WAL/SHM, логи, exports, test-results, IDE-файлы и рабочие
резервные копии OpenCode.

Проверка перед публикацией:

```powershell
git status --short
git ls-files
git grep -n -I -E "(api[_-]?key|token|secret)" -- .
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe -m pytest -q
node --test integrations/opencode/token-counter.test.js
```

Имена переменных и placeholders допустимы; реальные значения — нет. Проверьте
`git diff --cached` перед commit и не используйте `git add -f` для runtime.

Remote подключается после выбора владельцем площадки и политики публикации:

```bash
git remote add origin <REMOTE_URL>
git branch -M main
git push -u origin main
```

## Установка из внешнего клона

```bash
git clone <REMOTE_URL> <ANY_LOCAL_FOLDER>
cd <ANY_LOCAL_FOLDER>
```

Затем передайте агенту `INSTALL_WITH_AI.md` или запустите штатный установщик.
Внутренние пути строятся от нового корня. Абсолютными могут быть только внешние
пользовательские файлы OpenCode и ссылка `{file:...}` в его конфиге.

Если OpenCode уже подключён к проверенной другой копии, новый установщик переносит
upstream-ссылку и прежний локальный client key после сверки plan/env/plugin. При
любом расхождении выполните штатный rollback из прежней копии. Сохраните usage.db
через backup и не удаляйте прежний каталог до успешного doctor, запуска и одного
пользовательского запроса в новом клоне.
