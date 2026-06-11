# ARIA — Генеральная спецификация

> Генеральная спецификация самого upstream-проекта ARIA.
> Это **не** проектный продукт — это инструмент управления разработкой под Claude Code.
> Обновляется через чат с AI при значимых изменениях архитектуры/стека/roadmap.
> Читается `/spec` ШАГ 0 оркестратором при планировании задач Phase 1+.

**Версия:** 1.0
**Дата:** 2026-04-15
**Статус:** Accepted

---

## 1. Продукт

### 1.1 Назначение
ARIA — **AI-powered development framework для Claude Code**. Универсальная система управления проектной разработкой: протоколы, команды, шаблоны, адаптеры под конкретные стеки. Цель — обеспечить, чтобы AI-агент вёл разработку production-grade, с замкнутыми контурами документации и процесса.

### 1.2 Целевая аудитория
- Разработчики, использующие Claude Code как основной инструмент
- Команды, желающие стандартизировать работу AI-агента в проектах
- Мейнтейнеры форков — получают обновления, отдают contribute-back

### 1.3 Ключевые принципы

1. **Замкнутый контур каждого документа.** Документ жив только если AI пишет и AI читает. Пользователь не редактирует файлы вручную — только через чат.
2. **Production-grade без MVP.** Каждое решение — production-grade. Запрещены заглушки, костыли, «потом доделаем».
3. **Стандартные решения.** Claude Code `.mcp.json`, GitHub PRs, gh CLI, Git hooks, YAML/Markdown. Никакого кастомного ПО.
4. **Разделение репозиториев.** ARIA-инфраструктура — в docs-репо. Код приложения — в code-репо. Два репо, две ответственности.
5. **Необходимость документа.** Срезы и обзоры запрещены, заменяются командами, генерирующими срез по запросу.

### 1.4 Ценностное предложение
- Готовый конвейер `/spec → /next-task → /review → /done` с субагентами
- Адаптеры под стек (python-fastapi, kotlin-android, csharp-avalonia) — полный настроенный набор команд и шаблонов
- Двусторонняя синхронизация upstream↔форк через `/aria-sync` + `/aria-triage`
- Гарантия трассируемости задача→commit→файлы→тесты (через STATE.yaml расширенный контракт + CHANGELOG гибридный)

---

## 2. Архитектура

### 2.1 Обзор модулей

ARIA состоит из 4 крупных частей:

```
upstream ARIA
├── core/
│   ├── CLAUDE.md.template          — правила для форков
│   ├── commands/                   — 11+ команд: spec, auto, next-task, review, done, research, status, adr-new, aria-sync, aria-release, aria-docs-audit, aria-triage, e2e-gate, aria-init
│   ├── protocols/                  — fork_sync_playbook
│   └── templates/                  — CLAUDE, PATHS, STATE, project_config, STACK, SPEC, .mcp.json
├── adapters/                       — стек-специфичные дополнения
│   ├── python-fastapi/             — adapter.yaml (+ mcp_servers + commands + antipatterns), hooks/
│   ├── kotlin-android/
│   └── csharp-avalonia/
├── scripts/                        — bash-утилиты
│   ├── hooks/                      — pre-commit, commit-msg, pre-push
│   ├── validate_spec_e2e.sh        — 13-point E2E Testing Plan validator
│   └── validate_e2e_results.sh     — валидатор YAML результатов E2E
├── docs/                           — документация upstream
│   ├── SPEC.md                     — этот документ
│   ├── STACK.md                    — технологический стек
│   ├── REFERENCES.md               — карта референсов (заполняется /spec Research)
│   ├── ADR/                        — архитектурные решения
│   ├── research/                   — обзоры и гипотезы
│   ├── policies/                   — CHANGELOG_POLICY, COMMIT_POLICY, DOCUMENTATION_LIFECYCLE
│   └── ARIA_GUIDE.md               — гайд для новых пользователей
├── TRIAGE.md                       — буфер входящих PR от форков
├── FORKS.md                        — реестр известных форков
└── .github/
    └── PULL_REQUEST_TEMPLATE.md    — шаблон contribute-back PR
```

### 2.2 Data flow: fork lifecycle

```
Новый проект → /aria-init → форк клонируется из upstream ARIA
                          → интерактивно заполняются SPEC.md, CLAUDE.md, PATHS.yaml, STATE.yaml, STACK.md, .mcp.json
                          → Two-Phase Commit первичной инициализации
                          → запись в upstream FORKS.md (с согласия пользователя)

Разработка → /spec → создаёт spec/{task}.md
           → /next-task → читает spec, кодит, тестирует, /review, E2E VERIFY
           → /done → Two-Phase Commit (код + docs), обновляет STATE/CHANGELOG/ROADMAP

Sync с upstream → /aria-sync → pull изменений из upstream ARIA, применение NEW/UPSTREAM_AHEAD
                → SYS_CHANGELOG.md обновляется (baseline для следующего sync)

Contribute back → /aria-sync --contribute → PR в upstream с auto-labels
                → upstream /aria-triage → merge → CHANGELOG строка с атрибуцией

Релиз upstream → /aria-release v{X.Y.Z} → CHANGELOG валидация → tag + push
              → читает FORKS.md → gh issue create в каждом форке
```

### 2.3 Платформа и окружение
Кроссплатформенная: Windows, macOS, Linux (через Git Bash на Windows). Требования — Git, gh CLI, Claude Code (Opus 4.6).

### 2.4 Связь с адаптерами
- ARIA — upstream без адаптера (адаптер = N/A)
- Форки — выбирают один из 3 адаптеров при `/aria-init` (или работают без адаптера для кастомного стека)

---

## 3. Ключевые сущности / Domain Model

| Сущность | Описание | Ключевые атрибуты |
|----------|----------|-------------------|
| **Форк (fork)** | Конкретный проект, работающий по правилам ARIA | project_slug, adapter, aria.version, docs_repo, code_repo |
| **Адаптер (adapter)** | Стек-специфичный набор дополнений к ARIA | name, stack, commands, mcp_servers, antipatterns, commit_format |
| **Задача (task)** | Единица работы в рамках фазы | name, priority, depends_on, spec, files, tests, acceptance, commit |
| **Фаза (phase)** | Группа связанных задач с единой целью | number, title, tasks[], completion_criteria |
| **Спека (spec)** | Детальная спецификация задачи | YAML-заголовок (read_docs/read_code) + секции (Цель, Архитектура, AC, Тесты, E2E Testing Plan) |
| **Замкнутый контур** | Документ с AI-писателем и AI-читателем | writer, reader, class (operational/external/hook/policy) |
| **Контрибуция** | Улучшение форка, предложенное upstream'у | source_fork, pr_number, scope_labels, acceptance_state |

### 3.2 Инварианты

1. **Один документ — одна функция.** Если документ выполняет 2+ функций — разделить.
2. **Каждая задача имеет spec.** `/auto` и `/next-task` останавливаются если spec отсутствует.
3. **Каждый AC имеет тест.** `validate_spec_e2e.sh` блокирует спеку без покрытия AC тестами.
4. **ARIA-инфраструктура только в docs-репо.** `/auto` шаг 0.2 блокирует если code-репо содержит ARIA-артефакты.
5. **Policies редактируются только через чат с AI.**

### 3.3 Жизненные циклы

**Задача:** `not_started → in_progress (/next-task) → done (/done) | blocked (STATE.blockers)`
**Фаза:** `planned → in_progress (первая задача started) → completed (все задачи done + /e2e-gate PASS)`
**Форк:** `created (/aria-init) → active (регулярные /aria-sync) → paused (нет sync 90+ дней) → archived (manual)`
**Контрибуция:** `pending (в TRIAGE.md) → discussed | declined | accepted (→ CHANGELOG атрибуция)`

---

## 4. Нефункциональные требования

| Категория | Требование | Метрика |
|-----------|------------|---------|
| Производительность | `/spec` одна задача | ≤5 минут на standard задачу |
| Масштабируемость | Форков на upstream | Без верхнего предела (реестр в FORKS.md, пакетная обработка `/aria-triage`) |
| Надёжность | Целостность Two-Phase Commit | Phase 2 amend-able при FAIL, Phase 1 immutable |
| Безопасность | Секреты | Никогда не коммитятся (`.gitignore` + pre-commit hook) |
| Наблюдаемость | Трассируемость задачи | task→spec→commit→files→tests (STATE + CHANGELOG + git log) |
| Развёртывание | Установка в новый проект | Одна команда `/aria-init` (≤15 минут с интерактивом) |
| Кросс-платформенность | Поддержка ОС | Windows / macOS / Linux через Git Bash |

---

## 5. Стек

**Источник истины:** [STACK.md](STACK.md)

Краткий обзор:
- **Языки:** Bash (hooks, scripts), Markdown (документация, команды-промпты), YAML (конфигурация)
- **Инструменты:** Claude Code Opus 4.6, Git, gh CLI
- **MCP:** опционально (в форках через adapter.yaml mcp_servers)
- **Зависимости:** нет package manager — ARIA это набор файлов, не приложение
- **Явно исключено:** Docker, серверы, CI/CD runner, базы данных, REST API

---

## 6. Roadmap

### Phase 1: Стабилизация и автоматизация

**Цель:** Новый пользователь разворачивает ARIA за одну команду. Владелец upstream обрабатывает contribute-back и уведомляет форки.
**Результат:** production-grade инструмент с замкнутыми контурами, живыми документами, разделением репо, гарантией не-регрессии относительно форков.

**Статус:** запланирована (16 задач, 1 done).
**Spec:** [docs/spec/phase-1-stabilization.md](spec/phase-1-stabilization.md)
**STATE:** [../STATE.yaml](../STATE.yaml)

**Задачи (сводка):**

| # | Задача | Scope | Приоритет | Статус |
|---|--------|-------|-----------|--------|
| 11 | living-doc-rule | [DOCS]+[POLICY] | P1 | done 2026-04-15 |
| 8 | spec-md-infrastructure | [CORE]+[DOCS] | P1 | not_started |
| 2 | mcp-infrastructure | [CORE]+[ADAPTER] | P1 | not_started |
| 13 | references-regression-fix | [CORE]+[FIX] | P1 | not_started |
| 15 | commands-regression-fix | [CORE]+[FIX] | P1 | not_started |
| 1 | aria-init | [CORE] | P1 | not_started |
| 3 | roadmap-populate | [DOCS]+[CORE] | P1 | not_started |
| 9 | aria-stack-md | [DOCS]+[CORE]+[HOOKS] | P2 | not_started |
| 5 | triage-md | [DOCS] | P2 | not_started |
| 6 | forks-md | [DOCS] | P2 | not_started |
| 7 | github-infrastructure | [META]+[CORE] | P2 | not_started |
| 4 | aria-triage | [CORE] | P2 | not_started |
| 12 | contributions-md-delete | [DOCS]+[FIX] | P2 | not_started |
| 10 | aria-release-notify | [FIX]+[CORE] | P2 | not_started |
| 14 | readme-upstream | [DOCS] | P2 | not_started |
| 16 | forks-cleanup-migration | [FIX]+[POLICY] | P2 | in_progress |

### Phase 2: (видение, детали — при старте фазы)

Пока не стартует (по запрету пользователя откладывать задачи — Phase 1 решает всё до перехода).

Направления для будущего:
- Worktrees-интеграция (ускорение независимых задач)
- MCP-сервер ARIA (экспонирует команды через MCP)
- CLI-обёртка (`aria init`, `aria sync` без Claude Code)
- Автогенерация spec/{task}.md из спеки фазы (снижение ручной работы)

---

## 7. Implementation Guide

Не применимо для Phase 1 (задачи — инфраструктурные, не требуют реализации чек-листом). При переходе к фазам с промышленной разработкой (например, если ARIA получит свой product-component) — секция появится.

---

## 8. Evolution & Vision

**EVO_1 (проект):** отдельный форк разрабатывает свой продукт, используя ARIA как инструмент. ARIA остаётся стабильным.

**EVO_2 (система):** upstream ARIA эволюционирует благодаря контрибьюциям форков. Каждый форк привносит обкатанные практики — они становятся частью upstream и распространяются на все форки через `/aria-sync`.

**Текущее состояние:**
- EVO_1 работает: форки ведут разработку (см. FORKS.md)
- EVO_2 частично: форки стали источником практик (Research-субагент, Атакер, REFERENCES формат, STATE расширенный контракт, Two-Phase Commit). Большинство форков ещё не контрибьютили обратно.

Phase 1 создаёт инфраструктуру для EVO_2: `/aria-triage`, FORKS.md, TRIAGE.md, GitHub labels + PR template, атрибуция в CHANGELOG.

---

## 9. Связанные документы

| Документ | Назначение |
|----------|-----------|
| [STACK.md](STACK.md) | Технологический стек ARIA (decisions) |
| [REFERENCES.md](REFERENCES.md) | Референсы внешних проектов (создаётся при первом /spec) |
| [ADR/](ADR/) | Архитектурные решения ARIA |
| [ARIA_GUIDE.md](ARIA_GUIDE.md) | Guide для новых пользователей ARIA |
| [policies/CHANGELOG_POLICY.md](policies/CHANGELOG_POLICY.md) | Политика CHANGELOG |
| [policies/COMMIT_POLICY.md](policies/COMMIT_POLICY.md) | Политика коммитов |
| [policies/DOCUMENTATION_LIFECYCLE.md](policies/DOCUMENTATION_LIFECYCLE.md) | Жизненный цикл документов |
| [spec/phase-1-stabilization.md](spec/phase-1-stabilization.md) | Спека Phase 1 |
| [../CLAUDE.md.template](../core/CLAUDE.md.template) | Шаблон CLAUDE.md для форков |
| [../README.md](../README.md) | Внешняя витрина upstream |
| [../STATE.yaml](../STATE.yaml) | Текущее состояние |
| [../CHANGELOG.md](../CHANGELOG.md) | История задач ARIA |
| [../ROADMAP.md](../ROADMAP.md) | Срез Roadmap с прогрессом |
| [../TRIAGE.md](../TRIAGE.md) | Входящие PR от форков |
| [../FORKS.md](../FORKS.md) | Реестр форков |
