Синхронизация форка с upstream ARIA. Полный протокол — в `core/protocols/fork_sync_playbook.md` (upstream ARIA).

**ARIA upstream:** https://github.com/Yura1980Yura/ARIA

**PATHS:** Прочитай `${{ENV_DOCS}}/PATHS.yaml` — все пути.

**МОДЕЛЬ:** субагенты — только `model: "opus"`.

## Режимы работы

| Режим | Синтаксис | Что делает |
|-------|-----------|------------|
| **Pull (по умолчанию)** | `/aria-sync` | Взять новое из upstream |
| **Dry-run** | `/aria-sync --dry-run` | Показать что изменится, ничего не применять |
| **Contribute back** | `/aria-sync --contribute {тема}` | Предложить отправить улучшение форка в upstream |

---

## Контракт SYS_CHANGELOG.md (ARIA Phase 1, Task 11 — закрытие протечки)

`SYS_CHANGELOG.md` в docs-репо форка — **системный журнал синхронизации с upstream ARIA**. Источник истины о том, что уже применено из upstream и что изменилось в форке относительно последнего sync.

### Формат записи (append-only)

```markdown
## [YYYY-MM-DD HH:MM] sync <direction>: upstream v{X.Y.Z} ↔ fork

**Направление:** pull | contribute-back
**Upstream версия до:** v{A.B.C}
**Upstream версия после:** v{X.Y.Z}

**Применено артефактов:** N из M предложенных
- <путь_артефакта_1>: <статус: NEW|UPSTREAM_AHEAD|INTENTIONALLY_CUSTOM>
- <путь_артефакта_2>: <статус>

**Пропущено:**
- <путь>: <причина — BOTH_DIVERGED / INTENTIONALLY_CUSTOM / ручное решение пользователя>

**Contribute-back кандидаты** (если pull обнаружил изменения в форке относительно last sync):
- <путь>: <краткое описание>
```

### Писатель

`/aria-sync` (любой режим — pull, dry-run, contribute) — **append-запись** после каждого успешного sync или создания contribute-back PR.

### Читатели (обязательные контракты)

1. **`/aria-sync` при следующем запуске (baseline-detection):**
   - Читает последнюю запись направления `pull` → определяет upstream версию baseline
   - Сравнивает с текущей upstream HEAD → определяет что изменилось с момента last sync
   - Это заменяет простое сравнение «upstream vs fork» на точное «upstream since last sync vs fork»

2. **`/aria-sync --contribute` (дельта форка):**
   - Читает все записи с момента last `pull` — что изменилось в форке ПОСЛЕ синхронизации
   - Фильтрует только артефакты, которые появились/изменились в форке (не пришли из upstream)
   - Формирует обобщённое PR-предложение на эти артефакты

3. **`/aria-docs-audit`:**
   - Проверяет что `SYS_CHANGELOG.md` существует и пополняется (есть хотя бы одна запись за последние 30 дней если форк активен)

---

---

## Процедура (Pull)

### ШАГ 1: Получить upstream ARIA

```bash
git clone --depth 1 https://github.com/Yura1980Yura/ARIA.git /tmp/aria-upstream 2>/dev/null || \
  git -C /tmp/aria-upstream pull
```

### ШАГ 2: Прочитать playbook синхронизации

Прочитай `/tmp/aria-upstream/core/protocols/fork_sync_playbook.md` — это авторитетная инструкция как выполнять sync. Она описывает:
- Какие артефакты сравнивать (upstream ↔ fork)
- Как определять статус (NEW / UPSTREAM_AHEAD / FORK_AHEAD / BOTH_DIVERGED / SYNCED / INTENTIONALLY_CUSTOM)
- Формат отчёта для пользователя
- Правила применения изменений

### ШАГ 3: Определить адаптер форка

Прочитай `project_config.yaml` в форке → `aria.adapter` (если задан). Это определяет какие файлы из `adapters/{name}/` в upstream релевантны для этого форка.

Если `aria.adapter` не задан — попытаться определить по стеку (`project_config.yaml` → стек), или спросить пользователя.

### ШАГ 4: Инвентаризация и сравнение

По протоколу из playbook:
1. Прочитать upstream артефакты (core templates, commands, protocols, adapter).
2. Прочитать соответствующие артефакты форка.
3. Для каждого определить статус.

**ВАЖНО — фильтр команд (правило разделения maintainer vs fork, Phase 1 v2.4 R17):**

При сравнении `core/commands/*.md` НЕ включать в инвентаризацию upstream-only команды:
- `aria-init.md` — не нужна форку после инициализации
- `aria-release.md` — maintainer-only (релиз upstream)
- `aria-triage.md` — maintainer-only (обработка входящих PR)

Только 12 fork-level команд сравниваются и при необходимости обновляются:
`auto, done, spec, review, next-task, research, status, aria-sync, adr-new, aria-docs-audit, e2e-gate, roadmap-sync`

Также НЕ сравнивать upstream-only артефакты:
- `TRIAGE.md`, `FORKS.md` — реестры upstream
- `.github/PULL_REQUEST_TEMPLATE.md`, `.github/labels.yaml` — GitHub-конфигурация upstream
- `CHANGELOG.md` upstream (у форка свой CHANGELOG проекта)
- `README.md` upstream (у форка свой README продукта)

### ШАГ 4.5: Declined cache — фильтрация уже отклонённых contribute-back кандидатов (P-012)

При определении contribute-back кандидатов (FORK_AHEAD) — проверить `SYS_CHANGELOG.md` на записи с `declined-contribute-back`:

```
Если в SYS_CHANGELOG.md есть запись:
  declined-contribute-back: {путь_артефакта}, reason: {причина}, date: {дата}
То НЕ предлагать этот артефакт как contribute-back кандидат повторно.
```

**Формат записи declined в SYS_CHANGELOG.md** (append-запись при `/aria-sync --contribute` отклонении):
```markdown
## [{YYYY-MM-DD HH:MM}] declined-contribute-back

**Артефакт:** {путь}
**Причина:** {причина из upstream — из PR comment или из /aria-triage --decline}
**PR:** #{N} (если был открыт)
**Действие:** не предлагать повторно до следующего существенного изменения артефакта в форке
```

**Сброс declined cache:** если артефакт был существенно изменён в форке ПОСЛЕ даты declined (новый коммит затрагивает файл после declined date) — кандидат снова может быть предложен.

### ШАГ 5: Вывести отчёт пользователю

Формат — по playbook (Фаза 3). Должен содержать:
- Summary (сколько артефактов в каждой категории)
- Changes to apply (с краткими diff)
- Contribute-back candidates (что форк может дать upstream), **исключая declined cache**
- Manual merge required (конфликты)
- Intentionally custom (что не трогаем)

### ШАГ 6: Получить одобрение и применить

По протоколу (Фаза 4):
- Применить NEW и UPSTREAM_AHEAD после одобрения (можно выборочно).
- Two-Phase Commit в форке: код отдельно, docs отдельно.
- **Append-запись в `SYS_CHANGELOG.md`** по формату выше (направление: pull, перечень применённых/пропущенных артефактов, contribute-back кандидаты если есть).
- Обновить `aria.version` в `project_config.yaml` форка.

---

## Процедура (Contribute Back)

`/aria-sync --contribute {тема}` — запускает обратный поток:

1. **Прочитать `SYS_CHANGELOG.md`** — найти последнюю запись направления `pull`, определить baseline.
2. Определить изменения в форке относительно baseline (что появилось/изменилось в форке после last sync).
3. Отфильтровать проект-специфику.
4. Обобщить до уровня upstream (стек-нейтрально или адаптер-специфично).
5. Предложить PR в `Yura1980Yura/ARIA` с **auto-labeling** (Task 7) и **метаданными происхождения** (P-013):

   **5.1 Сгенерировать PR body с метаданными:**
   PR body ОБЯЗАН содержать секцию метаданных (см. `core/protocols/fork_sync_playbook.md`):
   ```markdown
   ## Метаданные происхождения
   - **Fork:** {FORK_NAME}
   - **Adapter:** {adapter из project_config.yaml}
   - **ARIA version in fork:** {aria.version из project_config.yaml}
   - **Repo:** {owner/repo если есть}
   - **Обкатано в проекте:** {количество задач где артефакт использовался}
   - **Оригинальный контекст:** {зачем форк создал/изменил этот артефакт}
   - **Обобщение:** {что изменено для универсализации}
   - **Затронутые файлы upstream:** {список}
   ```

   **5.2 Создание PR:**
   ```bash
   GH="/c/Program Files/GitHub CLI/gh.exe"
   "$GH" repo fork Yura1980Yura/ARIA --clone /tmp/aria-contrib

   # Внести обобщённые изменения в /tmp/aria-contrib
   cd /tmp/aria-contrib
   # ... файловые операции ...

   # commit + push
   git add <files>
   git commit -m "[scope] обобщение: {тема}"
   git push origin HEAD:contribute/{тема}

   # Создание PR — GitHub автоматически применит .github/PULL_REQUEST_TEMPLATE.md
   PR_URL=$("$GH" pr create \
     --repo Yura1980Yura/ARIA \
     --title "[contribute-back from {FORK_NAME}] {тема}" \
     --body-file /tmp/aria-contrib-pr-body.md \
     --label "contribute-back")

   # Auto-labeling scope по затронутым файлам (Task 7)
   CHANGED_FILES=$(git diff --name-only main..HEAD)
   for f in $CHANGED_FILES; do
     case "$f" in
       core/commands/*)  "$GH" pr edit "$PR_URL" --add-label "scope:command" ;;
       core/protocols/*) "$GH" pr edit "$PR_URL" --add-label "scope:protocol" ;;
       core/*)           "$GH" pr edit "$PR_URL" --add-label "scope:core" ;;
       adapters/*)       "$GH" pr edit "$PR_URL" --add-label "scope:adapter" ;;
       scripts/hooks/*)  "$GH" pr edit "$PR_URL" --add-label "scope:hooks" ;;
       docs/*)           "$GH" pr edit "$PR_URL" --add-label "scope:docs" ;;
     esac
   done
   ```
6. **Append-запись в `SYS_CHANGELOG.md`** (направление: contribute-back, с ссылкой на PR).
7. Атрибуция в CHANGELOG upstream происходит при мердже через `/aria-triage --accept` (не вручную).

---

## Установка hooks в форк

При первом `/aria-sync` (или если hooks отсутствуют):

1. Определить адаптер форка (`project_config.yaml` → `aria.adapter`)
2. Скопировать `adapters/{adapter}/hooks/pre-commit` в форк (в репо кода, например `scripts/hooks/`)
3. Настроить `git config core.hooksPath scripts/hooks` в репо кода форка
4. Для репо docs (если отдельный) — те же soft hooks

Hooks в форках — всегда SOFT (предупреждают, не блокируют). Для жёстких правил — только upstream ARIA.

## Важно

- **Всегда dry-run сначала** если форк давно не синхронизировался или пользователь не уверен.
- **Никогда не применять без явного одобрения** — см. playbook.
- **Two-Phase Commit обязателен** при применении.
- **При конфликте (BOTH_DIVERGED)** — не пытайся мерджить автоматически, покажи обе стороны и дай пользователю решить.
- **После sync — установи/обнови hooks** если пришли новые версии в adapters/{adapter}/hooks/.

Playbook в upstream — источник истины протокола. Если он обновился — читай новую версию, не полагайся на эту краткую команду.
