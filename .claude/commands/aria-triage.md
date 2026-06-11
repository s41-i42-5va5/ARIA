Пакетная обработка входящих PR/Issues от форков. Только для upstream-maintainer'а ARIA.

**PATHS:** Прочитай $ARIA_DOCS/PATHS.yaml — все пути.
**МОДЕЛЬ:** субагенты — только `model: "opus"`.
**Предусловие:** `gh auth login` выполнен, есть права на upstream repo.

**Синтаксис:**
- `/aria-triage` — собрать и проанализировать открытые PR
- `/aria-triage --accept T-001 [T-002 ...]` — принять PR (merge + атрибуция)
- `/aria-triage --decline T-003 "причина"` — отклонить PR (close с комментарием)
- `/aria-triage --discuss T-004 "вопрос/уточнение"` — добавить комментарий в PR с запросом деталей

---

## Режим по умолчанию: сбор и анализ

### ШАГ 1: Сбор входящих

```bash
GH="/c/Program Files/GitHub CLI/gh.exe"  # или просто "gh" на Linux/macOS
cd "$ARIA_CODE"

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

### ШАГ 4: Анализ каждого pending

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
Прочитай все остальные pending PR из TRIAGE.md. Есть ли PR, которые трогают те же файлы? Если да — какой применять первым?

### 4. Проверь совместимость с текущей версией ARIA
Из project_config.yaml форка (PR body должно содержать "ARIA version in fork: X.Y.Z"):
- Если форк на более старой версии — нужен ли rebase?
- Есть ли breaking changes в upstream с момента версии форка?

### 5. Оцени ценность
- Применимость к другим форкам (упомянутым в FORKS.md)
- Обкатанность (из body: "Обкатано в проекте: N задач")
- Качество описания PR (следует ли PR template)

### 6. Проверь перекрытие с уже принятыми артефактами
Прочитай TRIAGE.md архив (accepted). Есть ли принятые PR, которые затрагивали те же файлы?
Если да — определи тип:
- **ENHANCEMENT** — улучшение существующего артефакта (обратная совместимость сохранена)
- **REPLACEMENT** — замена существующего (обратная совместимость нарушена)
- **INDEPENDENT** — независимое изменение (разные части одного файла)

Для ENHANCEMENT: что конкретно улучшено, совместимость с оригиналом.
Для REPLACEMENT: почему замена лучше, что потеряется из оригинала.
Укажи оригинальный T-ID и форк-автор: "улучшение артефакта от {original_fork} (T-{NNN})"

## Выход (кратко)

### PR #{N} — {title}

**Scope:** {core/adapter/command/protocol/hooks/docs}
**Конфликты:** {нет / список PR}
**Совместимость:** {OK / требует rebase / breaking}
**Ценность:** {HIGH/MEDIUM/LOW + обоснование}

**Перекрытие:** {нет / ENHANCEMENT T-{NNN} от {fork} / REPLACEMENT T-{NNN} от {fork}}
**Рекомендация:** ACCEPT / DECLINE / DISCUSS
**Обоснование:** {...}
**План интеграции:** {порядок операций, если accept}
```

### ШАГ 5: Сводный отчёт

Агрегируй результаты всех субагентов:

```
## /aria-triage — сводка {YYYY-MM-DD}

### Новые с последнего triage
- T-{NNN}: PR #{N} от {fork}

### Анализ pending ({M} PR)

| ID | PR | Fork | Scope | Ценность | Рекомендация |
|----|----|------|-------|----------|--------------|

### Конфликты
{перечень или "конфликтов нет"}

### Предлагаемый порядок обработки
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

Для каждого T-ID:

### 1. Получи PR info
```bash
PR_NUM=$(grep -F "T-{NNN}" TRIAGE.md | head -1 | awk -F'|' '{print $5}' | tr -d '#  ')
FORK=$(grep -F "T-{NNN}" TRIAGE.md | head -1 | awk -F'|' '{print $4}' | xargs)
```

### 2. Merge PR
```bash
"$GH" pr merge "$PR_NUM" --squash --auto --delete-branch
```

### 2.1 Обработка ошибки merge
Если `gh pr merge` вернул ошибку (конфликт):
1. НЕ пытаться force-merge
2. Вывести: "PR #{PR_NUM} конфликтует с предыдущим merge. Конфликтные файлы: {список из `gh pr diff`}"
3. Предложить пользователю:
   - Попросить автора PR сделать rebase на актуальный main (`gh pr comment` с просьбой)
   - Или: maintainer вручную мерджит через `gh pr checkout #{PR_NUM}` + ручной resolve + commit
4. Перевести T-ID в статус `blocked` в TRIAGE.md (не `declined` — PR не отклонён, ждёт rebase)
5. Продолжить с остальными `--accept` из списка (не прерывать пакетную обработку)

### 3. Получи SHA мерджа и список файлов
```bash
MERGE_SHA=$(git -C "$ARIA_CODE" rev-parse --short HEAD)
FILES_CHANGED=$(git -C "$ARIA_CODE" diff --name-only "$MERGE_SHA^..$MERGE_SHA" | tr '\n' ',' | sed 's/,$//')
```

### 4. Добавь атрибуцию в CHANGELOG.md (гибридный формат, Task 12)

Найди или создай секцию `## v{X.Y.Z}-dev` (текущая версия в разработке) → scope-подсекцию. Добавь строку:

```markdown
| {YYYY-MM-DD} | {task_from_pr_title} | {MERGE_SHA} | {FILES_CHANGED} | — | from {FORK}, PR #{PR_NUM} |
```

**Цепочная атрибуция** (если PR улучшает/заменяет уже принятый артефакт — определено в ШАГ 4 п.6):
```markdown
| {YYYY-MM-DD} | {task_from_pr_title} | {MERGE_SHA} | {FILES_CHANGED} | — | from {FORK2}, PR #{PR_NUM} (ENHANCEMENT of T-{NNN} from {FORK1}) |
```

`tests` ставь `—` (upstream не запускает тесты форка). При следующем `/aria-release` эта строка попадёт в релизную версию.

### 5. Переведи T-ID в архив TRIAGE.md

Перемести строку из «Активные предложения» в «Архив»:

```markdown
| T-{NNN} | {YYYY-MM-DD} | {fork} | #{PR_NUM} | accepted | v{X.Y.Z}-dev | merged |
```

### 6. Обнови FORKS.md (если форк указал Repo при создании PR)
Если в body PR есть `Repo: owner/repo` — обнови/создай строку в FORKS.md (колонка Repo, возможно новый форк).

### 7. Коммит
```bash
git -C "$ARIA_CODE" add TRIAGE.md CHANGELOG.md FORKS.md
git -C "$ARIA_CODE" commit -m "[META] triage accepted: T-{NNN} (PR #{PR_NUM} from {FORK})"
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
git -C "$ARIA_CODE" add TRIAGE.md
git -C "$ARIA_CODE" commit -m "[META] triage declined: T-{NNN} — {краткая причина}"
```

---

## Режим --discuss

Синтаксис: `/aria-triage --discuss T-004 "вопрос/уточнение"`

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
- **Читатель TRIAGE.md:** `/aria-triage` (следующий запуск, для определения зарегистрированных PR)
- **Писатель CHANGELOG.md:** `/aria-triage --accept` (добавление строки с атрибуцией)
- **Читатель FORKS.md:** `/aria-triage` (контекст автора PR — ARIA version, Repo)
- **Писатель FORKS.md:** `/aria-triage --accept` если PR содержит новую Repo-информацию

---

## Правила

- **Пользователь не редактирует TRIAGE.md вручную** — только через команду
- **Атрибуция ТОЛЬКО в CHANGELOG.md**, не в CONTRIBUTIONS.md (тот удалён в Task 12)
- **Merge через `--squash`** — один коммит на один PR, упрощает атрибуцию
- **`--delete-branch`** — не оставляем мёртвые ветки после merge
