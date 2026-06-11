# ARIA — Changelog

> **Формат:** гибридный — scope-секции по версиям + табличные строки задач внутри.
> Подробности — в `docs/policies/CHANGELOG_POLICY.md`.
> **Внимание:** старые версии (v3.0-v3.3) до Phase 1 могут использовать исходный scope-только формат (без табличных строк) — исторический.
> Новые записи (v3.4+) — только гибридный формат.

---

## v3.4-dev (in progress) — Phase 1: Стабилизация и автоматизация

> Накопительная версия — `/aria-triage --accept` и `/done` пополняют таблицы ниже.
> При релизе `/aria-release v3.4.0 "Phase 1 стабилизация"` эта `-dev` → `v3.4.0 ({дата})`.

### Core
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | spec-md-infrastructure (Task 8) | TBD | core/templates/SPEC.md.template, core/templates/STATE.yaml.template (расширен files/tests/acceptance/commit/sessions R1), core/CLAUDE.md.template (расширенная таблица структуры документов) | — | — |
| 2026-04-15 | mcp-infrastructure (Task 2) | TBD | core/templates/.mcp.json.template, adapters/python-fastapi/adapter.yaml (+mcp_servers 4 сервера), adapters/kotlin-android/adapter.yaml (+mcp_servers {}), adapters/csharp-avalonia/adapter.yaml (+mcp_servers {}), core/templates/STACK.md.template (убрана мёртвая ссылка TOOLS.md) | — | — |
| 2026-04-15 | commands-regression-fix (Task 15 — R7-R12 + R3-R4) | TBD | core/commands/spec.md (409→≥460 строк с универсализацией), core/commands/review.md (34→≥230 с Adversarial Tester + E2E Integration), core/commands/next-task.md (≥135 с E2E VERIFY шаг 6), core/commands/done.md (Two-Phase + гибридный CHANGELOG), core/commands/research.md (+REFERENCES контракт), core/commands/status.md, core/commands/auto.md, core/commands/e2e-gate.md (создан) | — | — |
| 2026-04-15 | aria-triage (Task 4) | TBD | core/commands/aria-triage.md (создан, режимы --accept/--decline/--discuss) | — | — |
| 2026-04-15 | aria-release-notify (Task 10 FIX) | TBD | core/commands/aria-release.md (ШАГ 6 удалён, ШАГ 7 читает FORKS.md) | — | — |
| 2026-04-15 | aria-init (Task 1) | TBD | core/commands/aria-init.md (создан, 14 шагов с SPEC интерактивно + .gitignore с запретами + валидация R6) | — | — |
| 2026-04-15 | roadmap-populate (Task 3) | TBD | core/commands/roadmap-sync.md (создан) | — | — |
| 2026-04-15 | aria-init/aria-sync доработка (post-Phase 1) | TBD | core/commands/aria-init.md (+селективное копирование fork-level/upstream-only команд, +валидация), core/commands/aria-sync.md (+расширения), core/protocols/fork_sync_playbook.md (+доработки) | — | — |
| 2026-04-16 | Phase 2 аудит БЛОК 1+2 | 3709b24 | core/commands/auto.md (38→156 строк: +/review, +E2E Verify, +инфра-проверка, +Quality Gate, +e2e-gate триггер, +лимит попыток), core/commands/aria-triage.md (+P-001 конфликты gh pr diff, +P-002 PR body валидация, +P-003 merge order, +P-004 Repo валидация, +P-005/P-006 --discuss множественные T-ID, +P-014 amendment detection), core/commands/aria-release.md (+WARN >50% без Repo), FORKS.md (+Repo для форков) | — | — |
| 2026-04-16 | Phase 2 аудит БЛОК 4 | 51fe7e2 | core/protocols/command_contracts.md (новый — контракт минимальной функциональности команд), core/protocols/fork_sync_playbook.md (+раскрытие плейсхолдеров R11, +контракт custom P-003, +атрибуция P-009/P-015, +метаданные P-013), core/commands/aria-sync.md (+declined cache P-012, +метаданные contribute-back P-013), core/commands/aria-docs-audit.md (+проверка 7 контракт команд), docs/research/docker-mcp-investigation.md (R8) | — | — |
| 2026-04-16 | Phase 2 fixup: битые плейсхолдеры + validate_spec_e2e | TBD | scripts/validate_spec_e2e.sh (проверки 11-13 переведены на $E2E_SECTION), core/templates/project_config.yaml.template (+infrastructure.check_command/start_command, +commands.typecheck/dev_server), core/protocols/fork_sync_playbook.md (+маппинг INFRA_CHECK/START_COMMAND), core/commands/aria-init.md (+infrastructure в подстановку) | — | — |

### Adapters
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | mcp_servers декларация | TBD | adapters/python-fastapi/adapter.yaml, adapters/kotlin-android/adapter.yaml, adapters/csharp-avalonia/adapter.yaml | — | — |

### Docs
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | living-doc-rule (Task 11) | TBD | core/CLAUDE.md.template (+5 правил), docs/policies/DOCUMENTATION_LIFECYCLE.md (+запрет ручн. редактирования, +исключения), core/commands/aria-docs-audit.md (+6 секций), core/commands/aria-sync.md (+контракт SYS_CHANGELOG), docs/research/living-docs-audit.md | — | — |
| 2026-04-15 | aria-stack-md (Task 9) | TBD | docs/STACK.md (12 секций для upstream ARIA) | — | — |
| 2026-04-15 | spec-md-upstream (Task 8) | TBD | docs/SPEC.md (9 секций для upstream ARIA) | — | — |
| 2026-04-15 | triage-md (Task 5) | TBD | TRIAGE.md | — | — |
| 2026-04-15 | forks-md (Task 6) | TBD | FORKS.md (3 форка начальных) | — | — |
| 2026-04-15 | github-infrastructure (Task 7) | TBD | .github/PULL_REQUEST_TEMPLATE.md, .github/labels.yaml (12 labels) | — | — |
| 2026-04-15 | contributions-md-delete (Task 12) | TBD | CONTRIBUTIONS.md (УДАЛЁН), CHANGELOG.md (гибридный формат + v3.0 миграция), docs/policies/CHANGELOG_POLICY.md (переписана), docs/policies/DOCUMENTATION_LIFECYCLE.md (обновлён) | — | — |
| 2026-04-15 | readme-upstream (Task 14) | TBD | README.md (внешняя витрина, 7+ секций) | — | — |
| 2026-04-15 | roadmap-populate (Task 3) | TBD | ROADMAP.md (наполнен 16 задачами Phase 1 + прогресс) | — | — |

### Hooks
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | commands-regression-fix (Task 15) validate scripts | TBD | scripts/validate_spec_e2e.sh (13 проверок E2E Testing Plan), scripts/validate_e2e_results.sh (6 проверок YAML результатов) | — | — |
| 2026-04-16 | Phase 2 аудит БЛОК 3 validate scripts | TBD | scripts/validate_e2e_results.sh (6→10 проверок: +screenshot >5KB, +console log критические, +verdict соответствие, +count match), scripts/validate_spec_e2e.sh (TCM поиск: grep по файлу → regex внутри E2E секции, устранены ложные срабатывания) | — | — |

### Fix
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | forks-cleanup-migration (Task 16, завершён) | — | Форки: очистка ARIA-реликтов из code-repo, .gitignore (+запрет .claude/ CLAUDE.md .dev/ STATE.yaml PATHS.yaml SYS_CHANGELOG.md). Механизмы защиты: /aria-init ШАГ 11, /aria-docs-audit секция 3, /auto шаг 0.2 | — | — |

### Breaking
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-15 | CONTRIBUTIONS.md удалён (Task 12) | TBD | CONTRIBUTIONS.md | — | — |

---

## v3.0 (2026-04-14) — Первичное ядро (from forks)

> Миграция атрибуции из удалённого CONTRIBUTIONS.md в гибридный CHANGELOG (Task 12).
> Исходные 4 записи в CONTRIBUTIONS.md переписаны в табличные строки ниже.

### Core
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-14 | initial_core | — | core/* (протоколы, команды, документооборот, /auto, /done, /spec, /next-task, /review, /research, /status, Two-Phase Commit, STATE, spec/{task}, ADR, read_docs, E2E Gate, Adversarial review, Progressive testing) | 672+ | обкатка в форках (37+ задач, 4 фазы) |

### Adapters
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-14 | python-fastapi adapter | — | adapters/python-fastapi/* (Docker postgres+redis, Python антипаттерны type:ignore/noqa/**kwargs, React антипаттерны useEffect-deps, E2E Gate через MCP browser) | — | — |
| 2026-04-14 | kotlin-android adapter | — | adapters/kotlin-android/* (Kotlin антипаттерны !!/GlobalScope, hardware hw-diag/safety-review/field-test, safety: watchdog/emergency-stop/GPS-loss, compatibility review) | — | from SOLAR AUTOPILOT |
| 2026-04-14 | csharp-avalonia adapter | — | adapters/csharp-avalonia/* (C# антипаттерны #pragma-warning-disable, мультиплатформенный commit format Desktop/iOS/Android) | — | — |

---

## v3.3.1 (2026-04-14) — Docs audit fix + cross-platform

### Fix
| дата | task | SHA | файлы | тесты | атрибуция |
|------|------|-----|-------|-------|-----------|
| 2026-04-14 | docs audit fix + cross-platform | bcdfce3 | README.md, ARIA_GUIDE.md (8→11 команд, убрана examples/, добавлен commit-msg), COMMIT_POLICY.md (валидация: commit-msg + pre-commit), scripts/hooks/commit-msg (Co-Authored-By soft warning), scripts/hooks/install.sh (cross-platform case), scripts/hooks/tests/run_all.sh, CLAUDE.md.template (v3.1→v3.3), project_config.yaml.template (aria.version 3.1→3.3), adapters/csharp-avalonia/hooks/pre-commit (создан) | — | — |
| 2026-04-14 | .gitignore project-docs/ | 707ded4 | .gitignore | — | — |

---

## v3.3 [2026-04-14] — Documentation lifecycle + Mechanical policy enforcement

### Docs
- **`docs/ARIA_GUIDE.md`** — полное руководство по ARIA как проекту. Жизненный цикл форка, структура репо, версионирование, обзор команд.
- **`docs/policies/CHANGELOG_POLICY.md`** — обязательная политика: что и как попадает в CHANGELOG. Формат, категории, версионирование.
- **`docs/policies/COMMIT_POLICY.md`** — формат коммитов для upstream и форков. Scopes, Two-Phase, обязательные правила.
- **`docs/policies/DOCUMENTATION_LIFECYCLE.md`** — матрица "событие → какой документ обновить". Принцип живого документа (семантический + механический потребитель).

### Core
- **`core/CLAUDE.md.template`** обновлён — обязательные ссылки на все три политики в секции "Обязательные политики ARIA". Семантическая валидация через AI-агента теперь гарантирована.
- **`core/protocols/fork_sync_playbook.md`** — исправлен критичный баг: `docs/aria_sync_log.md` заменён на `SYS_CHANGELOG.md` (соответствует de-facto стандарту форков).
- **`core/commands/aria-sync.md`** — обновлён, то же исправление + секция "Установка hooks в форк".

### Commands
- **`core/commands/aria-release.md`** — команда релиза новой версии ARIA. Автогенерация черновика CHANGELOG из коммитов с scope-группировкой. Создание тега и push.
- **`core/commands/aria-docs-audit.md`** — аудит соответствия ARIA_GUIDE реальному содержимому репо. Проверка hooks vs политики ("не стала ли политика мёртвой").
- **`core/commands/adr-new.md`** — команда создания ADR. Шаблон, нумерация, интеграция со SPEC.md.

### Hooks
- **`scripts/hooks/pre-commit`** — механическая валидация CHANGELOG_POLICY. Блокирует коммит если изменены core/adapters/policies/hooks без обновления CHANGELOG.md.
- **`scripts/hooks/commit-msg`** — валидация формата первой строки коммита согласно COMMIT_POLICY. Scopes: CORE, PROTOCOL, ADAPTER, POLICY, DOCS, HOOKS, FIX, META, CONTRIB.
- **`scripts/hooks/pre-push`** — финальная проверка: при push в upstream CHANGELOG должен быть обновлён в push range.
- **`scripts/hooks/install.sh`** — установщик через `core.hooksPath=scripts/hooks`. Hooks живут в репе, не в `.git/hooks/`.
- **`scripts/hooks/README.md`** — документация hooks, правила обхода (`--no-verify`).
- **`scripts/hooks/tests/run_all.sh`** — smoke-тесты: синтаксис bash + соответствие hooks политикам (если политика не покрыта hook'ом — тест падает).

### Adapters
- **`adapters/kotlin-android/hooks/pre-commit`** — SOFT warnings для Kotlin-форков. STACK без CHANGELOG, SPEC без ADR, Two-Phase нарушение, libs.versions.toml изменения.
- **`adapters/python-fastapi/hooks/pre-commit`** — SOFT warnings для Python-форков. Миграции без spec, pyproject без STACK.

### Замкнутый контур актуализации документации

Теперь работают **два независимых потребителя** для каждой политики:
1. **Семантический** — AI-агент читает CLAUDE.md, который ссылается на политику
2. **Механический** — git hook применяет те же правила на уровне команды

Если один из потребителей не работает — документ мёртв. Это проверяется командой `/aria-docs-audit`.

---

## v3.2 [2026-04-14] — Stack documentation + Fork sync protocol

### Core
- **`core/templates/STACK.md.template`** — шаблон документа технологического стека.
  Принцип: **decisions, не versions**. Версии живут в native package manager проекта.
  Разделы: платформа, языки, SDK, категории библиотек, протоколы, референсы, инструменты, MCP, окружение, правила, явно исключённое.

- **`core/protocols/fork_sync_playbook.md`** — формализованный протокол синхронизации форка с upstream.
  Фазы: инвентаризация → сравнение → отчёт → применение.
  Статусы артефактов: NEW / UPSTREAM_AHEAD / FORK_AHEAD / BOTH_DIVERGED / SYNCED / INTENTIONALLY_CUSTOM.
  Поддерживает три направления: pull (upstream → fork), contribute back (fork → upstream), upstream-wide propagation.

### Commands
- **`/aria-sync`** переписана под новый playbook. Добавлены режимы `--dry-run` и `--contribute {тема}`.
  Команда теперь ссылается на playbook как на источник истины протокола — это позволяет эволюционировать логику sync без правки команды.

### Документационная философия
- Введено чёткое разделение документов:
  - `STACK.md` — что и почему (decisions)
  - `SPEC.md` — функциональная архитектура (модули, data flow)
  - `REFERENCES.md` — конкретные алгоритмы из референсных проектов
  - `ADR/*.md` — архитектурные решения
  - `gradle/libs.versions.toml` (или аналог) — версии (authoritative)
  - `CLAUDE.md` — правила работы агента, коммиты, запреты

---

## v3.1 [2026-04-14] — Standalone Release

ARIA выделена в самостоятельный проект.

### Core
- Универсальный CLAUDE.md.template с {{плейсхолдерами}}
- 7 команд (/auto, /done, /spec, /next-task, /review, /research, /status) в универсальной форме
- Шаблоны: PATHS.yaml, project_config.yaml, STATE.yaml
- Протокол решения проблем (3-3-3)
- Запрещённые паттерны (универсальные + стек-специфичные)
- Критические антипаттерны проектирования

### Adapters
- python-fastapi (обкатан в форках, 37+ задач опыта)
- kotlin-android (из SOLAR AUTOPILOT)
- csharp-avalonia (обкатан в форках)

### Infrastructure
- Upstream repo: https://github.com/Yura1980Yura/ARIA
- CONTRIBUTIONS.md — реестр контрибуций из проектов
- /aria-sync — команда синхронизации с upstream

---

## v3.0 [2025–2026] — Обкатка в форках

Обкатка в форках (37+ задач, 4 фазы).
- E2E Gate через MCP Playwright
- Progressive testing (3 уровня)
- Adversarial review (атакер)
- read_docs механизм в спеках
- Two-Phase Commit
