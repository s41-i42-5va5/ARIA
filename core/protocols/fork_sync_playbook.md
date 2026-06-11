# ARIA Fork Sync Playbook

> **Это промпт-инструкция для AI-агента.** Когда пользователь форка запускает команду синхронизации с upstream ARIA, агент читает этот документ и выполняет протокол.
>
> **Цель:** семантическая (не файловая) синхронизация форка с upstream. Показать "было → станет → почему", получить одобрение, применить.

---

## Когда этот протокол запускается

- Пользователь форка сказал "синхронизируй с ARIA" / "проверь что есть нового в ARIA" / `/aria-sync`.
- Пользователь upstream ARIA сказал "примени это изменение во все форки".

---

## Фаза 1: Инвентаризация

Прочитай следующие артефакты в **upstream ARIA** (`Yura1980Yura/ARIA` или локальный clone, если указан):

### Артефакты upstream

| Группа | Пути |
|--------|------|
| **Philosophy** | `docs/README.md`, `docs/ARIA_GUIDE.md`, `docs/SPEC.md`, `docs/STACK.md` (контрибьюция — в корневом `README.md`) |
| **Core templates** | `core/CLAUDE.md.template`, `core/templates/*.template` (включая STACK.md.template, SPEC.md.template, STATE.yaml.template с расширенным контрактом задачи, .mcp.json.template) |
| **Commands (fork-level)** | `core/commands/{auto,done,spec,review,next-task,research,status,aria-sync,adr-new,aria-docs-audit,e2e-gate,roadmap-sync}.md` — 12 команд, копируются в форк |
| **Commands (upstream-only)** | `core/commands/{aria-init,aria-release,aria-triage}.md` — **НЕ копировать в форк** (maintainer-only) |
| **Protocols** | `core/protocols/*.md` (включая этот файл) |
| **Adapters** | `adapters/{stack}/*` — только для стека конкретного форка |
| **Scripts** | `scripts/validate_spec_e2e.sh`, `scripts/validate_e2e_results.py` (Python-эталон, портированная версия), `scripts/validate_e2e_results.sh` (bash-fallback), `scripts/hooks/install.sh` — копируются в форк |
| **Changelog** | `CHANGELOG.md` — **не копировать** (у форка свой CHANGELOG проекта; читается только для определения upstream версии) |
| **GitHub infra (upstream-only)** | `.github/PULL_REQUEST_TEMPLATE.md`, `.github/labels.yaml`, `TRIAGE.md`, `FORKS.md` — **НЕ копировать в форк** |

**Правило разделения maintainer vs fork (ARIA Phase 1 v2.4, R17):**
- Форк получает через `/aria-sync` только fork-level артефакты
- Upstream-only команды (`aria-init`, `aria-release`, `aria-triage`) и реестры (`TRIAGE.md`, `FORKS.md`, `.github/*`) **остаются только в upstream**
- Форк не может «принимать PR от других форков» в модели ARIA — эта роль только у upstream-maintainer'а

Прочитай соответствующие артефакты в **форке** (путь задаётся пользователем, обычно это корень docs: `$PROJECT_DOCS`).

### Артефакты форка (соответствие)

| Upstream | Форк |
|----------|------|
| `core/CLAUDE.md.template` | `CLAUDE.md` (в корне docs форка) |
| `core/templates/STACK.md.template` | `docs/STACK.md` (если существует) |
| `core/templates/STATE.yaml.template` | `STATE.yaml` |
| `core/templates/PATHS.yaml.template` | `PATHS.yaml` |
| `core/templates/project_config.yaml.template` | `project_config.yaml` |
| `core/commands/*.md` | `.claude/commands/*.md` |
| `core/protocols/*.md` | отсутствуют в форке (референс) |

---

## Фаза 2: Сравнение

Для каждого артефакта upstream определи его **статус** в форке:

| Статус | Описание | Действие |
|--------|----------|----------|
| **NEW** | В upstream есть, в форке нет | Предложить добавить |
| **DIVERGED_UPSTREAM_AHEAD** | Upstream новее по содержанию, форк не обновлялся | Предложить обновить |
| **DIVERGED_FORK_AHEAD** | Форк содержит улучшения, которых нет в upstream | Предложить contribute back в upstream |
| **DIVERGED_BOTH** | И upstream, и форк изменились с момента последней синхронизации независимо | Нужен ручной merge, показать обе стороны |
| **SYNCED** | Содержание эквивалентно (с поправкой на адаптации форка) | Не трогать |
| **INTENTIONALLY_CUSTOM** | Форк сознательно отошёл от upstream (например, шаблон команды `/done` кастомизирован под стек) | Не трогать, отметить в отчёте |

**Правила определения статуса:**

1. **Для шаблонов (`*.template`):** форк сравнивается с шаблоном после подстановки `{{плейсхолдеров}}`. Проект-специфичный контент игнорируется, структурные изменения — значимы.

2. **Для команд (`.claude/commands/*.md`):** сравнивать по структуре и инструкциям. Адаптеры могут дополнять (не замещать) логику команд — это не расхождение, а специализация.

3. **Для протоколов:** upstream — источник истины, форки не должны изменять протоколы, только следовать им.

4. **Для правил в CLAUDE.md (антипаттерны, запрещённые паттерны, протоколы решения проблем):** upstream всегда выигрывает, форк должен подтягивать.

---

## Фаза 3: Отчёт пользователю

Выведи отчёт в следующем формате:

```
# ARIA Sync Report — {{fork_name}} @ {{date}}

## Upstream version: {{version из ARIA/CHANGELOG.md}}
## Last sync: {{дата последней синхронизации, если записана}}

## Summary
- NEW: {{N}} артефактов (будут добавлены)
- UPSTREAM_AHEAD: {{N}} артефактов (будут обновлены)
- FORK_AHEAD: {{N}} артефактов (кандидаты на contribute back)
- BOTH_DIVERGED: {{N}} артефактов (требуют ручного merge)
- INTENTIONALLY_CUSTOM: {{N}} (не трогаются)
- SYNCED: {{N}} (актуальны)

## Changes to apply (NEW + UPSTREAM_AHEAD)

### {{артефакт 1}}: {{статус}}
**Было (форк):** {{краткое описание или "отсутствует"}}
**Станет (upstream):** {{краткое описание}}
**Почему:** {{причина из CHANGELOG или обоснование}}
**Размер изменения:** {{XX строк, новый файл, удаление и т.д.}}

<details>
<summary>Показать diff</summary>
{{unified diff}}
</details>

### {{артефакт 2}}: ...

## Contribute-back candidates (FORK_AHEAD)

### {{артефакт}}: что форк улучшил
**Что добавлено в форке:** {{...}}
**Ценность для upstream:** {{почему полезно и другим проектам}}
**Рекомендация:** внести в upstream как {{обобщённый вариант / адаптер-специфичный}}

## Manual merge required (BOTH_DIVERGED)

### {{артефакт}}
**Upstream изменил:** {{...}}
**Форк изменил:** {{...}}
**Конфликт:** {{описание}}
**Предлагаемый merge:** {{...}}

## Intentionally custom (не трогаю)

- {{артефакт}}: {{причина кастомизации}}

---

**Действия:**
1. Одобрить все изменения из "Changes to apply"? [yes / выборочно / no]
2. Обработать contribute-back сейчас? [yes / позже / no]
3. Обсудить manual merge? [обсудить / пропустить]
```

---

## Фаза 4: Применение

После одобрения пользователя:

1. **Применение NEW + UPSTREAM_AHEAD:**
   - Применить изменения по одному артефакту за раз.
   - Для шаблонов: сохранить плейсхолдеры форка, обновить структуру.
   - Для команд: полная замена, если форк не кастомизировал.
   - Для CLAUDE.md секций "Правила" и "Запрещённые паттерны": merge — добавить недостающее, сохранить проект-специфичное.

   **КРИТИЧНО — раскрытие плейсхолдеров (R11):**
   При копировании команд из upstream в форк — ВСЕ плейсхолдеры `{{ENV_CODE}}`, `{{ENV_DOCS}}`, `{{PROJECT_NAME}}` и др. ОБЯЗАНЫ быть раскрыты в значения из `project_config.yaml` форка:
   - `{{ENV_CODE}}` → значение `paths.code` из `project_config.yaml` (например `$PROJECT_CODE`)
   - `{{ENV_DOCS}}` → значение `paths.docs` из `project_config.yaml` (например `$PROJECT_DOCS`)
   - `{{PROJECT_NAME}}` → значение `project` из `project_config.yaml`
   - `{{TEST_COMMAND}}` → значение `commands.test` из `project_config.yaml`
   - `{{LINT_COMMAND}}` → значение `commands.lint` из `project_config.yaml`
   - `{{TYPECHECK_COMMAND}}` → значение `commands.typecheck` из `project_config.yaml`
   - `{{DEV_SERVER_COMMAND}}` → значение `commands.dev_server` из `project_config.yaml`
   - `{{INFRA_CHECK_COMMAND}}` → значение `infrastructure.check_command` из `project_config.yaml` (источник: adapter.yaml)
   - `{{INFRA_START_COMMAND}}` → значение `infrastructure.start_command` из `project_config.yaml` (источник: adapter.yaml)

   **Валидация после раскрытия:**
   ```bash
   grep -rE '\{\{[A-Z_]+\}\}' "$DOCS_DIR/.claude/commands/" && echo "FAIL: остались нераскрытые плейсхолдеры"
   ```
   Если найдены нераскрытые плейсхолдеры — СТОП, исправить. bash с `${{ENV_CODE}}` НЕ ЗАПУСТИТСЯ.

   **Контракт минимальной функциональности (P-003 задача 3):**
   Если артефакт помечен как INTENTIONALLY_CUSTOM — проверить `core/protocols/command_contracts.md`:
   - Для каждого обязательного элемента команды — grep в custom-версии
   - Если элемент отсутствует — WARN пользователю с объяснением что потеряно

   **Атрибуция "моё vs чужое" при pull (P-009):**
   В отчёте для пользователя для каждого UPSTREAM_AHEAD артефакта указать происхождение:
   - `[upstream]` — артефакт полностью из upstream ARIA
   - `[fork-adapted]` — артефакт из upstream, адаптированный под стек форка
   - `[fork-originated → upstream]` — артефакт, изначально пришедший из этого форка через contribute-back, затем улучшенный в upstream

   Определение `[fork-originated → upstream]`: проверить CHANGELOG.md upstream на наличие атрибуции `from {fork_name}` для файлов этого артефакта.

   **Атрибуция при pull улучшенного протокола (P-015):**
   Если upstream артефакт содержит улучшения, изначально пришедшие от другого форка — указать:
   ```
   [upstream, includes contribution from {other_fork} PR #{N}]
   ```
   Это помогает форку понять происхождение изменений и контекст.

2. **Two-Phase Commit в форке:**
   - Phase 1 (код): `.claude/commands/*.md` и прочее в репе кода.
   - Phase 2 (docs): `docs/*.md`, `CLAUDE.md`, шаблоны. Сообщение: `[DOCS] ARIA sync: upstream v{{X.Y}} → apply NEW({{N}}) + UPDATE({{M}})`.

3. **Запись в `SYS_CHANGELOG.md` форка (стандарт ARIA):**

   `SYS_CHANGELOG.md` — существующий de-facto стандарт для форков . Формат — таблица записей:

   ```markdown
   | {{date}} | aria_sync_v{{X.Y}} | {{commit_hash}} | **ARIA sync v{{X.Y}}.** NEW: {{список}}. UPDATE: {{список}}. Skipped custom: {{список}}. Contributed back: {{список, если было}}. |
   ```

   Если `SYS_CHANGELOG.md` отсутствует (новый форк) — создать по шаблону:
   ```markdown
   # {{PROJECT}} — Системный Changelog

   > Записи системы разработки: ARIA обновления, команды, инфраструктура.
   > Код проекта — в CHANGELOG.md. Здесь — только система.

   | Дата | Задача | Коммит | Описание |
   |------|--------|--------|----------|
   ```

4. **Contribute-back (если одобрено):**
   - Открыть PR в `Yura1980Yura/ARIA` с выделенными изменениями.
   - Использовать ветку `contrib/{{fork_name}}/{{тема}}`.
   - В PR описании: контекст из форка, обоснование обобщения, ссылка на sync report.
   - Атрибуция при мердже — через `/aria-triage --accept` в CHANGELOG.md (CONTRIBUTIONS.md удалён).

   **Метаданные происхождения в contribute-back PR (P-013):**
   PR body ОБЯЗАН содержать секцию метаданных:
   ```markdown
   ## Метаданные происхождения
   - **Fork:** {fork_name}
   - **Adapter:** {adapter_name}
   - **ARIA version in fork:** {version}
   - **Обкатано в проекте:** {N задач / N дней}
   - **Оригинальный контекст:** {краткое описание — зачем форк создал этот артефакт}
   - **Обобщение:** {что изменено для универсализации — убран стек-специфичный код, etc.}
   - **Затронутые файлы upstream:** {список файлов}
   ```
   Эти метаданные используются `/aria-triage` для оценки ценности и совместимости.

---

## Фаза 5: Применение по направлениям

### Upstream → Forks (pull)

Пользователь в форке запускает `/aria-sync`. Работает по фазам 1-4 выше.

### Fork → Upstream (push / contribute back)

Пользователь в форке запускает `/aria-sync --contribute {{тема}}`. Протокол:

1. Определить изменения в форке относительно последней синхронизации.
2. Отфильтровать проект-специфичное (не подлежит upstream).
3. Обобщить: убрать stack/project-specific подробности, оставить паттерн.
4. Предложить место в upstream: `core/`, `adapters/{stack}/`, новый протокол.
5. Показать предлагаемый PR пользователю.
6. После одобрения — открыть PR в upstream.

### Upstream-wide propagation (user в ARIA)

Пользователь в upstream после внесения изменения говорит "распространи в форки". Протокол:

1. Определить список известных форков (из `CONTRIBUTIONS.md`).
2. Для каждого форка — выполнить dry-run фаз 1-3 (без применения).
3. Показать сводный отчёт: какие форки затронуты, какие конфликты ожидаются.
4. После одобрения — отправить уведомления (Issues или комментарии) в каждый форк: "Новое в ARIA v{{X.Y}}, запустите /aria-sync".

---

## Обязательные правила протокола

- **Никогда не применять изменения без показа diff и явного одобрения пользователя.**
- **Никогда не трогать `INTENTIONALLY_CUSTOM` артефакты без прямого указания пользователя.**
- **Никогда не заглушать ошибки синхронизации.** Если какой-то артефакт не получилось сравнить — явно сообщить.
- **Всегда логировать результат синхронизации** в `SYS_CHANGELOG.md` форка.
- **Two-Phase Commit обязателен** при применении изменений в форке (код отдельно от docs).

---

## Шаблон отчёта для пользователя upstream (bonus)

Когда пользователь upstream вносит изменение и хочет понять, затронет ли оно форки, прогнать этот playbook в режиме "dry-run по всем форкам" и дать сводку:

```
# Upstream change impact analysis

Change: {{описание}}
Affected files: {{список}}

## Per-fork impact

| Fork | Status in fork | Action needed |
|------|---------------|---------------|
| {fork} | UPSTREAM_AHEAD (clean apply) | /aria-sync |
| {fork} | UPSTREAM_AHEAD (clean apply) | /aria-sync |
| {{другой форк}} | BOTH_DIVERGED | manual review |

## Recommendation
- {{форк с clean apply}}: можно сразу применить
- {{форк с конфликтом}}: связаться с maintainer форка
```
