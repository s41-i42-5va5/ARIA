# Первый аудит живости документов ARIA

**Дата:** 2026-04-15
**Выполнен в рамках:** Task 11 Phase 1 (living-doc-rule)
**Методология:** анализ файлов `core/commands/*.md`, `scripts/hooks/*`, таблицы в `CLAUDE.md.template` секция «Структура документов»

---

## Инвентаризация документов ARIA

### Документы в upstream repo (C:/Users/user/Desktop/Projects/ARIA/ + C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W4_ARIA/)

### 1. Корневые (docs-репо `W4_ARIA/`)

| Документ | Писатель | Читатель | Класс | Статус |
|----------|----------|----------|-------|--------|
| `STATE.yaml` | `/done`, `/spec` | каждая сессия + `/auto` шаг 1 | operational | **LIVE** |
| `CHANGELOG.md` | `/done`, `/aria-triage --accept`, `/aria-release` | `/aria-release` (валидация политики) | operational | **LIVE** (после Task 12 — гибридный формат) |
| `ROADMAP.md` | `/roadmap-sync`, `/done` | pre-commit валидатор согласованности с SPEC.md | operational | **LIVE-hook** (исключение по правилу 1, после Task 3) |
| `SYS_CHANGELOG.md` | `/aria-sync` (append) | `/aria-sync` следующий запуск (baseline) + `/aria-sync --contribute` | operational | **LIVE** (после Task 11 — контракт зафиксирован) |
| `PATHS.yaml` | `/aria-init` | каждая команда при старте | operational | **LIVE** |
| `project_config.yaml` | `/aria-init` (с секцией `commands:`) | `/auto`, `/aria-sync` (aria.version) | operational | **LIVE** (после Task 1 — секция commands, R6) |
| `CLAUDE.md` | `/aria-init` (подстановка шаблона) + AI через чат | каждая сессия | policy | **LIVE-policy** (правило 3) |
| `README.md` | `/done` при триггерах | внешние посетители GitHub | external-audience | **LIVE-external** (после Task 14) |

### 2. docs/ (доктрины и проектные артефакты)

| Документ | Писатель | Читатель | Класс | Статус |
|----------|----------|----------|-------|--------|
| `docs/SPEC.md` (upstream) | `/aria-init` (для форков — из шаблона), `/done` при смене архитектуры (upstream — вручную через чат) | `/spec` ШАГ 0 | operational | **LIVE** (после Task 8) |
| `docs/STACK.md` (upstream) | `/aria-init` (для форков), `/done` при смене стека | `/spec`, `/review`, `/aria-docs-audit` | operational | **LIVE** (после Task 9) |
| `docs/REFERENCES.md` | `/spec` ШАГ 2 Research, `/research` | Аналитик `/spec` ШАГ 4 + кодер через read_docs spec'и | operational | **LIVE** (после Task 13) |
| `docs/ARIA_GUIDE.md` | вручную через чат при добавлении команд/адаптеров | `/aria-docs-audit` (валидация) + новые пользователи ARIA | external-audience | **LIVE-external** |
| `docs/ADR/*.md` | `/adr-new` | `/review`, `/spec` | operational | **LIVE** |
| `docs/spec/{task}.md` | `/spec` | `/auto`, `/review`, кодер через read_docs | operational | **LIVE** |
| `docs/research/{topic}.md` | `/research`, `/spec` Research при развёрнутом обзоре | `/spec` ШАГ 0 (агрегирует список) + ШАГ 4 Аналитик + ШАГ 5 Атакер по релевантности | operational | **LIVE** (после Task 13 — контракт зафиксирован) |
| `docs/policies/CHANGELOG_POLICY.md` | AI через чат | `/done`, `/aria-release`, pre-commit hook | policy | **LIVE-policy** (правило 3) |
| `docs/policies/COMMIT_POLICY.md` | AI через чат | commit-msg hook | policy | **LIVE-policy** (правило 3) |
| `docs/policies/DOCUMENTATION_LIFECYCLE.md` | AI через чат | `/aria-docs-audit`, pre-commit hook | policy | **LIVE-policy** (правило 3) |

### 3. core/ (код-репо ARIA)

| Документ | Писатель | Читатель | Класс | Статус |
|----------|----------|----------|-------|--------|
| `core/CLAUDE.md.template` | AI через чат | `/aria-init` (подстановка в форк) | template | **LIVE** |
| `core/templates/STACK.md.template` | AI через чат | `/aria-init` | template | **LIVE** |
| `core/templates/PATHS.yaml.template` | AI через чат | `/aria-init` | template | **LIVE** |
| `core/templates/project_config.yaml.template` | AI через чат | `/aria-init` | template | **LIVE** |
| `core/templates/STATE.yaml.template` | AI через чат | `/aria-init` | template | **LIVE** (после Task 8 — расширенный контракт задачи) |
| `core/templates/SPEC.md.template` | AI через чат | `/aria-init` | template | **LIVE** (создаётся в Task 8) |
| `core/templates/.mcp.json.template` | AI через чат | справочный документ (читается людьми при настройке MCP) | external-audience | **LIVE-external** (создаётся в Task 2) |
| `core/commands/*.md` | AI через чат | Claude Code runtime при `/<command>` | runtime | **LIVE** |
| `core/protocols/fork_sync_playbook.md` | AI через чат | `/aria-sync` в форках | operational | **LIVE** |

### 4. Upstream-only реестры

| Документ | Писатель | Читатель | Класс | Статус |
|----------|----------|----------|-------|--------|
| `TRIAGE.md` (upstream корень) | `/aria-triage` | `/aria-triage` | operational | **LIVE** (после Task 5) |
| `FORKS.md` (upstream корень) | `/aria-init` (в форках — запись о себе в upstream FORKS через PR), `/aria-sync` (обновление версии), вручную через чат | `/aria-release` (уведомления через `gh issue create --repo`), `/aria-triage` (контекст автора PR) | operational | **LIVE** (после Task 6) |
| `.github/PULL_REQUEST_TEMPLATE.md` | AI через чат | GitHub UI при создании PR | external-audience | **LIVE-external** (после Task 7) |

---

## Выявленные проблемы (до реализации Phase 1)

### 1. Удаляется в Task 12
- `CONTRIBUTIONS.md` — 100% функций дублируется в CHANGELOG после гибридного формата. Удалить, мигрировать 4 записи в CHANGELOG v3.0.

### 2. Регрессии без Phase 1 (закрываются в v2.4)

| ID | Проблема | Закрывает задача |
|----|----------|------------------|
| R1 | STATE.yaml не содержит files/tests/acceptance/commit по задачам | Task 8 |
| R2 | CHANGELOG scope-only без табличной детализации | Task 12 |
| R3 | REFERENCES формат без «Антиреференс» | Task 13 |
| R4 | REFERENCES группировка по дате вместо компоненту | Task 13 |
| R5 | Трассируемость коммит→задача→файлы→тесты нарушена | Task 12 |
| R6 | project_config.yaml без секции commands | Task 1 |
| R7-R12 | Команды spec/review/next-task упрощённые vs форки | Task 15 |

### 3. Протечки контуров (закрываются в v2.2)

| Документ | Проблема | Закрытие |
|----------|----------|----------|
| `research/*.md` | нет механического читателя в конвейере `/spec` | Task 13 (добавлен контракт чтения Аналитиком и Атакером) |
| `SYS_CHANGELOG.md` | не было явного читателя | Task 11 (этот документ — контракт зафиксирован в `/aria-sync`) |
| policies | не было правила «через чат с AI» | Task 11 (правило 3 в CLAUDE.md.template) |
| `ROADMAP.md` | не было признания pre-commit валидатора читателем | Task 11 (правило 1 исключение) |
| `README.md` upstream | отсутствовал в Phase 1 | Task 14 |

### 4. Халатность миграции 6 апреля (закрывается в v2.4)

В code-repo форков найдены ARIA-реликты, не удалённые при миграции:
- `.claude/commands/auto.md` (устарел с 31 марта) — **удалён 2026-04-15**
- `.dev/` (рассинхронизированное зеркало) — **удалён 2026-04-15**
- `CLAUDE.md` (устарел с 6 апреля) — **удалён 2026-04-15**

Защита добавлена:
- `.gitignore` в code-repo форков — запрет ARIA-артефактов
- W2 auto.md шаг 0.2 — проверка чистоты code-repo
- Правило 4 «Разделение репозиториев» в CLAUDE.md.template (Task 11 — этот документ)

### 5. Мёртвые документы (после Phase 1)

**0 мёртвых документов.** Все документы имеют либо AI-писателя + AI-читателя, либо признанное исключение (external-audience / hook / policy).

---

## Итоговая классификация (после реализации Phase 1 v2.4)

| Класс | Количество | Документы |
|-------|-----------|-----------|
| **LIVE** (AI↔AI) | 13 | STATE, CHANGELOG, SYS_CHANGELOG, PATHS, project_config, SPEC, STACK, REFERENCES, ADR/*, spec/*, research/*, TRIAGE, FORKS |
| **LIVE-hook** (валидатор-hook как читатель) | 1 | ROADMAP.md |
| **LIVE-external** (внешняя аудитория) | 4 | README.md, ARIA_GUIDE.md, .mcp.json.template, .github/PULL_REQUEST_TEMPLATE.md |
| **LIVE-policy** (через чат с AI) | 3 | CHANGELOG_POLICY, COMMIT_POLICY, DOCUMENTATION_LIFECYCLE, CLAUDE.md |
| **template** (шаблоны для форков) | 6 | CLAUDE.md.template, STACK.md.template, PATHS.yaml.template, project_config.yaml.template, STATE.yaml.template, SPEC.md.template |
| **runtime** (команды Claude Code) | N≥11 | core/commands/*.md |
| **DEAD** | 0 | — |

---

## Замкнутость контуров (сводка)

| Контур | Источник | Потребитель | Замкнут |
|--------|----------|-------------|---------|
| Задачи проекта | `/spec` → spec/{task}.md | `/auto` кодер + `/review` | ✓ |
| Архитектурные решения | `/adr-new` → ADR/*.md | `/spec`, `/review` | ✓ |
| Алгоритмы-референсы | `/spec` ШАГ 2 → REFERENCES.md | Аналитик + кодер | ✓ (после Task 13) |
| Обзоры-гипотезы | `/research` → research/*.md | `/spec` ШАГ 4 + ШАГ 5 | ✓ (после Task 13) |
| История задач | `/done` → CHANGELOG/STATE | `/aria-release`, каждая сессия | ✓ (после Task 12) |
| Sync с upstream | `/aria-sync` → SYS_CHANGELOG | `/aria-sync` следующий запуск + `/aria-sync --contribute` | ✓ (после Task 11) |
| Contribute-back | `/aria-sync --contribute` → PR | `/aria-triage` | ✓ (после Task 4) |
| Приём PR от форков | `/aria-triage` → TRIAGE + CHANGELOG | `/aria-triage` последующие запуски | ✓ (после Task 4+12) |
| Уведомление форков | `/aria-release` → `gh issue create` | внешние пользователи форков | ✓ (после Task 10) |
| Разделение репо | CLAUDE.md.template правило 4 | `/auto` шаг 0.2 + `/aria-docs-audit` + `.gitignore` | ✓ (Task 11 + Task 16) |

---

## Рекомендации (следующие задачи)

1. **Task 2** (mcp-infrastructure) — добавить `.mcp.json.template` + `mcp_servers` в adapter.yaml
2. **Task 13** (references-regression-fix) — замкнуть контур REFERENCES и research/
3. **Task 8** (spec-md-infrastructure) — SPEC.md.template + расширенный STATE.yaml.template
4. **Task 15** (commands-regression-fix) — перенос полных команд из форков

После Task 15 провести **второй аудит** — проверить что все контуры остались замкнутыми после глубоких изменений команд.

---

## Методика аудита (для будущих запусков `/aria-docs-audit`)

1. Собрать список всех `.md`/`.yaml` в `docs/` и корне docs-репо
2. Для каждого — grep по имени файла в `core/commands/*.md` (писатели: `append`, `write`, `cp`, `mv`; читатели: `read`, `Прочитай`, `cat`)
3. Классифицировать по таблице выше
4. Проверить разделение репо (правило 4) в code-repo
5. Проверить .mcp.json идентичность между repo
6. Проверить hooks покрывают политики
7. Сформировать отчёт с рекомендациями

---

**Аудит выполнил:** AI в рамках Task 11 Phase 1 (2026-04-15).
**Следующий плановый аудит:** после завершения Task 15 (commands-regression-fix).
