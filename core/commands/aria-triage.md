Пакетная обработка входящих PR/Issues от форков. Только для upstream-maintainer'а ARIA.

**PATHS:** Прочитай ${{ENV_DOCS}}/PATHS.yaml — все пути.
**МОДЕЛЬ:** субагенты — только `model: "opus"`.
**Предусловие:** `gh auth login` выполнен, есть права на upstream repo.

**Синтаксис:**
- `/aria-triage` — собрать и проанализировать открытые PR
- `/aria-triage --accept T-001 [T-002 ...]` — принять PR (merge + атрибуция)
- `/aria-triage --decline T-003 "причина"` — отклонить PR (close с комментарием)
- `/aria-triage --discuss T-004 [T-005 ...] "вопрос/уточнение"` — добавить комментарий в PR с запросом деталей (поддерживает множественные T-ID)

---

## Режим по умолчанию: сбор и анализ

### ШАГ 1: Сбор входящих

```bash
GH="/c/Program Files/GitHub CLI/gh.exe"  # или просто "gh" на Linux/macOS
cd "${{ENV_CODE}}"

# PR с label contribute-back
"$GH" pr list --label contribute-back --state open --json number,title,author,headRefName,body,labels,updatedAt > /tmp/aria-triage-prs.json

# Issues (любые, для контекста — могут быть запросы от форков)
"$GH" issue list --state open --json number,title,author,body,labels > /tmp/aria-triage-issues.json
```

### ШАГ 2: Чтение текущего TRIAGE.md

Прочитай `TRIAGE.md` — секция «Активные предложения». Определи:
- Какие PR уже зарегистрированы (по номеру PR)
- Какие новые (в `gh pr list`, но не в TRIAGE.md)

### ШАГ 3: Регистрация новых PR в TRIAGE.md

Для каждого нового PR — добавь строку в «Активные предложения»:

```
| T-{NNN} | {YYYY-MM-DD} | {fork} | #{pr_number} | {scope from labels} | {title} | pending |
```

ID `T-{NNN}` — последовательный номер (глобальный, не сбрасывается между версиями ARIA).

### ШАГ 4: Валидация PR body (P-002)

Для каждого нового PR проверь обязательные поля в body (по шаблону `.github/PULL_REQUEST_TEMPLATE.md`):

**Обязательные поля:**
- `ARIA version in fork:` — версия ARIA в форке
- `Обкатано в проекте:` — сколько задач обкатано
- `Scope:` — затронутые области

**Опциональные но рекомендуемые:**
- `Repo:` — `owner/repo` для регистрации в FORKS.md
- `Breaking changes:` — есть ли несовместимые изменения

Если обязательные поля отсутствуют — добавить WARN в анализ, предложить `--discuss` с запросом.

### ШАГ 5: Формальное обнаружение конфликтов PR (P-001)

Для каждой пары pending PR определи пересечение затронутых файлов:

```bash
# Для каждого PR — получить список файлов
"$GH" pr diff {PR_NUM} --name-only > /tmp/aria-triage-files-{PR_NUM}.txt
```

Конфликт фиксируется если два или более PR трогают одинаковые файлы. Результат — матрица конфликтов в отчёте.

### ШАГ 6: Анализ каждого pending

Для каждого PR со статусом `pending` в TRIAGE.md запусти параллельных субагентов (Task tool, `model: "opus"`):

```
Ты — triage-аналитик upstream ARIA.

## Входные данные
- PR number: {N}
- Fork: {fork_name} (из FORKS.md если есть — прочитай строку)
- Scope labels: {scope:*}
- Title: {title}
- Body: {body}

## Задачи

### 1. Прочитай diff PR
"$GH" pr diff {N}

### 2. Определи scope (сверь с labels, предложи корректировки)
- core/ — влияет на инфраструктуру
- adapters/{name}/ — адаптер-специфично
- core/commands/*.md — команды
- core/protocols/*.md — протоколы
- scripts/hooks/* — hooks
- docs/ — документация

### 3. Проверь конфликты с другими pending PR
Список файлов конфликтующих PR: {из ШАГ 5}

### 4. Проверь совместимость с текущей версией ARIA
Из PR body ("ARIA version in fork: X.Y.Z"):
- Если форк на более старой версии — нужен ли rebase?
- Есть ли breaking changes в upstream с момента версии форка?

### 5. Оцени ценность
- Применимость к другим форкам (упомянутым в FORKS.md)
- Обкатанность (из body: "Обкатано в проекте: N задач")
- Качество описания PR (следует ли PR template)

### 6. Проверка amendment (P-014)
Прочитай архив TRIAGE.md. Определи, является ли этот PR улучшением уже принятого артефакта:
- Ищи в архиве accepted PR от этого же форка, затрагивающие те же файлы
- Если найдено — пометить как AMENDMENT с ссылкой на исходный T-ID
- Рекомендация: принимать amendment если качество выше, отклонять если деградация

## Выход (кратко)

### PR #{N} — {title}

**Scope:** {core/adapter/command/protocol/hooks/docs}
**Конфликты:** {нет / список PR с пересечением файлов}
**Совместимость:** {OK / требует rebase / breaking}
**Ценность:** {HIGH/MEDIUM/LOW + обоснование}
**Amendment:** {нет / AMENDMENT от T-{XXX} — улучшение {файлов}}
**PR body валидация:** {OK / WARN: отсутствуют поля {список}}

**Рекомендация:** ACCEPT / DECLINE / DISCUSS
**Обоснование:** {...}
**План интеграции:** {порядок операций, если accept}
```

### ШАГ 7: Сводный отчёт

Агрегируй результаты всех субагентов:

```
## /aria-triage — сводка {YYYY-MM-DD}

### Новые с последнего triage
- T-{NNN}: PR #{N} от {fork}

### Анализ pending ({M} PR)

| ID | PR | Fork | Scope | Ценность | Amendment | Конфликты | Рекомендация |
|----|----|------|-------|----------|-----------|-----------|--------------|

### Конфликты (матрица файлов)
| PR A | PR B | Пересечение файлов |
|------|------|--------------------|
{перечень или "конфликтов нет"}

### Предлагаемый порядок обработки
Учитывает конфликты — PR без конфликтов первыми, конфликтующие в порядке ценности:
1. T-001: ACCEPT (high value, no conflicts)
2. T-003: DISCUSS (нужны уточнения по scope)
3. T-002: DECLINE (избыточен — функционал есть в T-001)

### Команды для выполнения
/aria-triage --accept T-001
/aria-triage --discuss T-003 "Уточни, нужен ли ...?"
/aria-triage --decline T-002 "Функционал уже в T-001 (PR #{N})"
```

Обнови TRIAGE.md: статусы `pending` → `analyzed`.

---

## Режим --accept

Синтаксис: `/aria-triage --accept T-001 [T-002 ...]`

**При множественных T-ID — определение порядка merge (P-003):**

Если указано несколько T-ID:
1. Прочитать файлы каждого PR (`gh pr diff --name-only`)
2. Построить граф зависимостей по пересечению файлов
3. Если пересечение найдено — merge в порядке:
   a) PR с наименьшим scope (меньше файлов) первым
   b) При равном scope — PR с более высокой ценностью первым
   c) Сообщить пользователю порядок и получить подтверждение
4. Если пересечение не найдено — merge параллельно (или последовательно без конфликтов)

Для каждого T-ID (в определённом порядке):

### 1. Получи PR info
```bash
PR_NUM=$(grep -F "T-{NNN}" TRIAGE.md | head -1 | awk -F'|' '{print $5}' | tr -d '#  ')
FORK=$(grep -F "T-{NNN}" TRIAGE.md | head -1 | awk -F'|' '{print $4}' | xargs)
```

### 2. Merge PR
```bash
"$GH" pr merge "$PR_NUM" --squash --auto --delete-branch
```

### 3. Получи SHA мерджа и список файлов
```bash
MERGE_SHA=$(git -C "${{ENV_CODE}}" rev-parse --short HEAD)
FILES_CHANGED=$(git -C "${{ENV_CODE}}" diff --name-only "$MERGE_SHA^..$MERGE_SHA" | tr '\n' ',' | sed 's/,$//')
```

### 4. Добавь атрибуцию в CHANGELOG.md (гибридный формат, Task 12)

Найди или создай секцию `## v{X.Y.Z}-dev` (текущая версия в разработке) → scope-подсекцию. Добавь строку:

```markdown
| {YYYY-MM-DD} | {task_from_pr_title} | {MERGE_SHA} | {FILES_CHANGED} | — | from {FORK}, PR #{PR_NUM} |
```

`tests` ставь `—` (upstream не запускает тесты форка). При следующем `/aria-release` эта строка попадёт в релизную версию.

### 5. Переведи T-ID в архив TRIAGE.md

Перемести строку из «Активные предложения» в «Архив»:

```markdown
| T-{NNN} | {YYYY-MM-DD} | {fork} | #{PR_NUM} | accepted | v{X.Y.Z}-dev | merged |
```

### 6. Обнови FORKS.md (регистрация Repo)

**6.1** Если в body PR есть `Repo: owner/repo` — обнови/создай строку в FORKS.md (колонка Repo).

**6.2 Валидация формата Repo (P-004):**
```bash
# Формат owner/repo: 1+ символов / 1+ символов, допускаются a-z A-Z 0-9 - _ .
if echo "$REPO_VALUE" | grep -qE '^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$'; then
  echo "OK: Repo формат валиден"
else
  echo "WARN: Repo формат невалиден: $REPO_VALUE — ожидается owner/repo"
  # Не блокирует, но добавляет WARN в отчёт
fi
```

**6.3** Если форк новый (нет строки в FORKS.md) — создать строку с Repo, адаптером (из PR labels), ARIA version (из PR body).

### 7. Коммит
```bash
git -C "${{ENV_CODE}}" add TRIAGE.md CHANGELOG.md FORKS.md
git -C "${{ENV_CODE}}" commit -m "[META] triage accepted: T-{NNN} (PR #{PR_NUM} from {FORK})"
```

---

## Режим --decline

Синтаксис: `/aria-triage --decline T-003 "причина"`

### 1. Добавь комментарий в PR
```bash
"$GH" pr comment "$PR_NUM" --body "Спасибо за предложение. В текущем виде не можем принять: {причина}.

Возможные действия:
- Рассмотреть альтернативный подход (если применимо)
- Оставить как custom в вашем форке

Закрываем PR без merge. При изменении подхода — открывайте новый PR."
```

### 2. Закрой PR
```bash
"$GH" pr close "$PR_NUM"
```

### 3. Архив в TRIAGE.md
```markdown
| T-{NNN} | {YYYY-MM-DD} | {fork} | #{PR_NUM} | declined | v{X.Y.Z}-dev | {причина} |
```

### 4. Коммит
```bash
git -C "${{ENV_CODE}}" add TRIAGE.md
git -C "${{ENV_CODE}}" commit -m "[META] triage declined: T-{NNN} — {краткая причина}"
```

---

## Режим --discuss

Синтаксис: `/aria-triage --discuss T-004 [T-005 ...] "вопрос/уточнение"`

Поддерживает множественные T-ID — один и тот же вопрос/уточнение добавляется в каждый из перечисленных PR.

Для каждого T-ID:

### 1. Добавь комментарий с вопросом
```bash
"$GH" pr comment "$PR_NUM" --body "{вопрос/уточнение}

Жду ответа для продолжения triage."
```

### 2. Обнови статус в TRIAGE.md
```
| T-{NNN} | ... | ... | ... | ... | ... | discussing |
```

### 3. Добавь label
```bash
"$GH" pr edit "$PR_NUM" --add-label "status:discussing"
```

---

## Замкнутый контур

- **Писатель TRIAGE.md:** `/aria-triage` (этот файл)
- **Читатель TRIAGE.md:** `/aria-triage` (следующий запуск, для определения зарегистрированных PR + amendment detection)
- **Писатель CHANGELOG.md:** `/aria-triage --accept` (добавление строки с атрибуцией)
- **Читатель FORKS.md:** `/aria-triage` (контекст автора PR — ARIA version, Repo)
- **Писатель FORKS.md:** `/aria-triage --accept` если PR содержит новую Repo-информацию

---

## Правила

- **Пользователь не редактирует TRIAGE.md вручную** — только через команду
- **Атрибуция ТОЛЬКО в CHANGELOG.md**, не в CONTRIBUTIONS.md (тот удалён в Task 12)
- **Merge через `--squash`** — один коммит на один PR, упрощает атрибуцию
- **`--delete-branch`** — не оставляем мёртвые ветки после merge
- **Amendment detection обязателен** — при каждом анализе проверять архив на принятые PR от этого форка к тем же файлам
- **Конфликты определяются формально** — через `gh pr diff --name-only`, не визуальная оценка
- **PR body валидируется** — обязательные поля должны присутствовать, иначе WARN
- **Repo формат валидируется** — `owner/repo`, невалидный формат = WARN
