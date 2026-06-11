# ARIA — Технологический стек

> **Назначение документа:** decisions стека ARIA upstream и обоснование решений.
> **Принцип:** decisions, не versions. Версии библиотек (там где есть) — в их native package manager.
> ARIA — набор файлов (Bash/Markdown/YAML), у неё нет package manager, значит версии не применимы.
>
> Этот документ — **не алгоритмические референсы**. Источники и заимствованные алгоритмы — в `REFERENCES.md`.

**Версия:** 1.0
**Дата:** 2026-04-15

---

## 1. Платформа и железо

**Целевая платформа:** кроссплатформенная — Windows, macOS, Linux
**Почему:** ARIA работает в Claude Code, который сам кроссплатформенный. Разработчики на всех трёх ОС должны работать одинаково.

**Минимальные требования:**
- Git (любая современная версия)
- gh CLI (для `/aria-sync --contribute`, `/aria-release`, `/aria-triage`)
- Claude Code (Opus 4.6 обязательно)
- Bash — встроен в macOS/Linux, на Windows — через Git Bash (идёт с Git for Windows)

**Почему не PowerShell на Windows:** Bash-скрипты универсальны для трёх ОС. Отдельная поддержка PowerShell удвоила бы объём hooks/scripts.

---

## 2. Языки

| Язык | Где используется | Почему |
|------|------------------|--------|
| **Bash** | `scripts/hooks/*`, `scripts/validate_*.sh` | Универсальная shell для всех ОС (Git Bash на Windows). Стандартный инструмент для git-hooks. |
| **Markdown** | Документация, команды Claude Code (`core/commands/*.md`) | Стандарт Claude Code для slash-commands. Читаемо людьми и AI-агентом. |
| **YAML** | `adapter.yaml`, `PATHS.yaml`, `STATE.yaml`, `project_config.yaml`, front-matter в спеках | Человекочитаемая сериализация. Нативно поддерживается Python/Ruby/Kotlin/C# парсерами (для hooks и валидаций в форках). |
| **JSON** | `.mcp.json`, `settings.local.json` | Стандарт Claude Code для конфигурации MCP и локальных настроек. |

**Почему не Python / Node для скриптов ARIA:** создаёт зависимость от рантайма. ARIA должна работать «из коробки» без установки дополнительного софта. Bash справляется.

**Почему не TOML вместо YAML:** YAML более читаем для многострочных значений (описания спек, acceptance criteria). Git/GitHub уже используют YAML широко.

---

## 3. SDK / рантайм / сборка

**Не применимо.** ARIA — набор файлов, а не приложение. Нет сборки, нет рантайма.

Единственная сборочная зависимость — **git hooks** копируются в форки командой `/aria-init` шаг 6 (`bash scripts/hooks/install.sh`).

---

## 4. Ключевые категории библиотек

**Не применимо.** ARIA не имеет библиотек-зависимостей.

В форках библиотеки определяются выбранным адаптером (`adapters/{name}/adapter.yaml` поле `stack`) и native package manager проекта.

---

## 5. Протоколы

| Протокол | Использование | Где детали |
|----------|---------------|------------|
| **Two-Phase Commit** | Код + docs разделены, docs коммитятся вторым коммитом с ссылкой на код | `core/commands/done.md`, `policies/COMMIT_POLICY.md` |
| **Fork Sync Playbook** | Правила синхронизации upstream↔форк, статусы NEW/UPSTREAM_AHEAD/FORK_AHEAD/BOTH_DIVERGED/SYNCED | `core/protocols/fork_sync_playbook.md` |
| **Contribute-back** | Форк → upstream через `/aria-sync --contribute`, PR с auto-labels | `core/commands/aria-sync.md` + `.github/PULL_REQUEST_TEMPLATE.md` |
| **Triage** | Пакетная обработка PR от форков через `/aria-triage` | `core/commands/aria-triage.md` + `TRIAGE.md` |
| **E2E Verify Gate** | Субагент-верификатор + machine-validation после каждой задачи | `core/commands/next-task.md` шаг 6, `scripts/validate_e2e_results.sh` |

---

## 6. Референсные проекты

Основной: **форки** (обкатанные команды `/spec` (409 строк), `/review` (243 строки), `/next-task` (98 строк), `/e2e-gate`, гибридный CHANGELOG, расширенный STATE.yaml, REFERENCES формат.
Обкатаны на 37+ задачах и 4 фазах разработки. Доказали жизнеспособность практик.

Форки обкатывают стек-специфичные адаптеры. Минорные добавления: safety_review, compatibility_review.

**Рассмотрено и отклонено:**
- **Makefile как альтернатива Bash-скриптам** — работает хуже на Windows, тянет зависимость
- **JSON Schema для валидации спек** — избыточно, bash-регексы покрывают 13 правил `validate_spec_e2e.sh`

Детали алгоритмов-референсов (не самих проектов) — в `REFERENCES.md`.

---

## 7. Инструменты разработки

| Инструмент | Назначение | Статус |
|------------|------------|--------|
| **Claude Code (Opus 4.6)** | Основной разработческий агент, запуск команд | **Обязателен** |
| **Git** | Версионирование, Two-Phase Commit, hooks | **Обязателен** |
| **gh CLI** | `/aria-sync --contribute`, `/aria-release`, `/aria-triage` — работа с GitHub | **Обязателен** для upstream/форков с GitHub |
| **Yandex.Disk / Obsidian / любое облако** | Хранение docs-репо | **Опционально**, выбор пользователя |
| **Docker** | Не используется | **Исключён** из upstream ARIA (в форках — зависит от адаптера, `python-fastapi` требует, остальные — нет) |
| **VSCode / JetBrains / любой IDE** | Редактор файлов приложения в code-репо (не для ARIA-файлов — их редактирует AI) | **На выбор разработчика** |

---

## 8. MCP-серверы

ARIA upstream сама **не требует** MCP-серверов — `core/commands/*.md` работают через встроенные инструменты Claude Code (Read, Write, Bash, Grep, Task, gh CLI).

**Для форков** через адаптер:
- `python-fastapi` рекомендует: playwright (E2E), postgres (БД-дебаг), context7 (docs), fetch (запасной WebFetch)
- `kotlin-android`: пусто (adb через Bash)
- `csharp-avalonia`: пусто

Конфигурация MCP: `.mcp.json` в корне проекта (source of truth для Claude Code). Генерируется `/aria-init` из секции `mcp_servers` выбранного адаптера.

Справочный шаблон формата: `core/templates/.mcp.json.template`.

---

## 9. Окружение

```
ARIA_DOCS = Yandex.Disk/Obsidian/W4_ARIA  (docs-репо upstream)
ARIA_CODE = Desktop/Projects/ARIA          (code-репо upstream, GitHub)

Для форков:
{FORK}_DOCS = docs-репо форка
{FORK}_CODE = code-репо форка
```

**Особенности:**
- `project-docs/` — junction из code-репо в docs-репо (`mklink /J` на Windows, `ln -s` на macOS/Linux). Требует admin-прав на части версий Windows
- `.mcp.json` — дублируется в docs и code репо по стандарту Claude Code (хэши идентичны)

**Исторический контекст:**
W4_ARIA заменил W2_Logic Converter в роли docs-репо для upstream ARIA. W2 остался docs-репо форка. Upstream ARIA команды частично наследованы оттуда (см. REFERENCES.md).

---

## 10. Обязательные правила

- **Модель AI:** только Claude Opus 4.6. Sonnet/Haiku запрещены. Нарушение = CRITICAL.
- **Коммиты:** Two-Phase Commit. Формат `[SCOPE] описание`. Scopes в `policies/COMMIT_POLICY.md`.
- **Разделение репо:** ARIA-инфраструктура только в docs-репо. Code-репо чист от `.claude/`, `.dev/`, `CLAUDE.md`, `STATE.yaml` и т.д.
- **Документация:** policies редактируются только через чат с AI. Пользователь не открывает файлы в редакторе.
- **Тесты:** каждый AC имеет тест (валидирует `validate_spec_e2e.sh`). Покрытие 100% AC → спека валидна.

Детали — в `CLAUDE.md.template` (секция «Правила документов»).

---

## 11. Явно исключено из стека

| Что | Почему |
|-----|--------|
| **Docker** | ARIA — это файлы, не сервис. Docker тянет сложность без пользы. |
| **CI/CD runner** (GitHub Actions, GitLab CI) | Хватает git-hooks локально. CI добавим когда будет реальная потребность (например, автотесты валидационных скриптов). |
| **База данных** | Всё состояние — в YAML/Markdown файлах. БД не нужна. |
| **REST API / сервер** | ARIA не клиент-сервер. Всё — локальные команды Claude Code. |
| **Python / Node / любой рантайм** для ARIA | Создаёт зависимость. Bash + стандартные Unix-утилиты справляются. |
| **Тяжёлые тест-фреймворки** (pytest, JUnit) для ARIA | Валидация через bash-скрипты (`validate_spec_e2e.sh`). Смоук тесты вручную через команды. |
| **Кастомные CLI-обёртки** (`aria init`, `aria sync`) | Команды живут в Claude Code через slash-commands. Отдельный CLI — избыточен. Может появиться в Phase 2+. |

---

## 12. Связанные документы

| Документ | Назначение |
|----------|-----------|
| [SPEC.md](SPEC.md) | Функциональная архитектура ARIA (модули, data flow) |
| [REFERENCES.md](REFERENCES.md) | Конкретные алгоритмы из референсных проектов |
| [ADR/](ADR/) | Архитектурные решения ARIA |
| `policies/CHANGELOG_POLICY.md` | Формат CHANGELOG |
| `policies/COMMIT_POLICY.md` | Scopes и формат коммитов |
| `policies/DOCUMENTATION_LIFECYCLE.md` | Какой документ при каком событии обновляется |
| [../CLAUDE.md](../CLAUDE.md) | Правила работы с AI-агентом upstream ARIA |
| [../ROADMAP.md](../ROADMAP.md) | Roadmap фаз |
| [../STATE.yaml](../STATE.yaml) | Текущее состояние |
