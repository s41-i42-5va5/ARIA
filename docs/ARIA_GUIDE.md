# ARIA — Руководство

**ARIA** (Autonomous Research & Implementation Architecture) — универсальный фреймворк разработки с AI-агентом (Claude Code). Описывает как структурировать проект, вести документацию, делить работу на задачи, коммитить и синхронизировать с upstream.

**Upstream repo:** https://github.com/Yura1980Yura/ARIA
**Текущая версия:** см. [CHANGELOG.md](../CHANGELOG.md)

---

## Что такое ARIA

ARIA — это **шаблон + команды + политики + протоколы** для проекта, где разработку ведёт связка "человек + Claude Code". Цель — предсказуемость, прослеживаемость решений и защита от когнитивных ошибок AI-агента.

### Что даёт ARIA
- Структуру документооборота (SPEC, STACK, STATE, ADR, REFERENCES)
- Набор команд конвейера разработки (`/spec`, `/next-task`, `/auto`, `/done`, ...)
- Протоколы решения проблем (3 whys, 3 files, 3 attempts)
- Запрещённые паттерны и антипаттерны проектирования
- Two-Phase Commit (код отдельно от docs)
- Механизм синхронизации между форками и upstream

### Чего ARIA НЕ делает
- Не выбирает стек за вас (это решение проекта)
- Не пишет код за вас (это делает AI-агент по вашим спекам)
- Не заменяет git/CI/CD (работает поверх них)

---

## Структура репозитория ARIA

```
ARIA/
├── CHANGELOG.md                           — версии ARIA (authoritative)
├── CONTRIBUTIONS.md                       — реестр контрибуций из форков
├── core/                                  — универсальное ядро
│   ├── CLAUDE.md.template                 — шаблон главного файла правил
│   ├── commands/                          — 11 команд конвейера
│   ├── templates/                         — шаблоны документов проекта
│   └── protocols/                         — мета-протоколы (sync, review, ...)
├── adapters/                              — специализация под стек
│   ├── kotlin-android/                    — Android + Kotlin
│   ├── python-fastapi/                    — Python backend
│   └── csharp-avalonia/                   — C# cross-platform
├── docs/                                  — документация самой ARIA
│   ├── ARIA_GUIDE.md                      — этот файл
│   ├── README.md                          — обзор docs/ (контрибьюция — в корневом README.md)
│   └── policies/                          — обязательные политики
│       ├── CHANGELOG_POLICY.md            — что попадает в CHANGELOG
│       ├── COMMIT_POLICY.md               — правила коммитов
│       └── DOCUMENTATION_LIFECYCLE.md     — когда какой документ обновляется
└── scripts/
    └── hooks/                             — git hooks (pre-commit, commit-msg, pre-push)
```

---

## Жизненный цикл проекта на ARIA

### 1. Инициализация форка

```
1. Создать новый репозиторий кода (например, Yura1980Yura/MY-PROJECT)
2. Создать Obsidian-директорию для docs (MY_DOCS)
3. Создать junction project-docs/ → docs-директория
4. Скопировать из ARIA:
   - core/CLAUDE.md.template → CLAUDE.md (заменить {{плейсхолдеры}})
   - core/commands/*.md → .claude/commands/
   - core/templates/STACK.md.template → docs/STACK.md (заполнить)
   - core/templates/STATE.yaml.template → STATE.yaml
   - core/templates/PATHS.yaml.template → PATHS.yaml
   - core/templates/project_config.yaml.template → project_config.yaml
   - adapters/{your-stack}/ — дополнительные правила
5. Установить git hooks из scripts/hooks/install.sh
6. Первый Two-Phase Commit
```

### 2. Разработка задачи

```
1. /next-task          — выбор задачи из STATE.yaml
2. /spec {task}        — формирование спеки (research + архитектура)
3. Ручной кодинг или /auto — реализация
4. /review             — ревью
5. /done               — тесты → коммит → push → STATE → CHANGELOG → ROADMAP
```

### 3. Синхронизация с upstream ARIA

```
/aria-sync             — взять новое из upstream (см. fork_sync_playbook.md)
/aria-sync --contribute {тема} — предложить улучшение в upstream
```

### 4. Релиз новой версии ARIA (только upstream)

```
/aria-release v{X.Y}   — автоматизация релиза (CHANGELOG, тег, пуш)
```

---

## Версионирование ARIA

**Semantic versioning** для upstream:
- **MAJOR** — ломающие изменения протоколов, форматов документов
- **MINOR** — новые команды, новые шаблоны, новые адаптеры
- **PATCH** — bugfixes, уточнения в документации

Форки фиксируют версию ARIA в `project_config.yaml` → `aria.version`.

---

## Команды ARIA (обзор)

| Команда | Назначение | Документ-авторитет |
|---------|-----------|---------------------|
| `/auto` | Автономное выполнение задач из STATE.yaml | `core/commands/auto.md` |
| `/next-task` | Выбрать и выполнить следующую задачу | `core/commands/next-task.md` |
| `/spec {task}` | Сформировать спецификацию задачи | `core/commands/spec.md` |
| `/done` | Завершить задачу (тесты → коммит → docs) | `core/commands/done.md` |
| `/review` | Многостороннее ревью кода | `core/commands/review.md` |
| `/research` | Свободное исследование | `core/commands/research.md` |
| `/status` | Статус проекта | `core/commands/status.md` |
| `/aria-sync` | Синхронизация с upstream | `core/commands/aria-sync.md` + `core/protocols/fork_sync_playbook.md` |
| `/aria-release` | Релиз новой версии ARIA (только upstream) | `core/commands/aria-release.md` |
| `/aria-docs-audit` | Проверка актуальности документации ARIA | `core/commands/aria-docs-audit.md` |
| `/adr-new` | Создание новой ADR записи | `core/commands/adr-new.md` |

---

## Обязательные политики

При работе с ARIA (и upstream, и форк) **обязательны к соблюдению**:

- **[CHANGELOG_POLICY.md](policies/CHANGELOG_POLICY.md)** — что попадает в CHANGELOG, формат записей
- **[COMMIT_POLICY.md](policies/COMMIT_POLICY.md)** — правила коммитов (формат сообщений, two-phase, префиксы)
- **[DOCUMENTATION_LIFECYCLE.md](policies/DOCUMENTATION_LIFECYCLE.md)** — какой документ при каком событии обновляется

Политики применяются **двумя механизмами**:
1. **Семантически** — AI-агент читает CLAUDE.md, который ссылается на политики
2. **Механически** — git hooks (`scripts/hooks/`) валидируют соблюдение при коммите/push

Если оба механизма не сработали — документ мёртв. Это тест на "жив ли документ".

---

## Модель AI-агента

**Только Claude Opus 4.6.** Sonnet и Haiku запрещены для всех агентов и субагентов во всех командах. Правило зафиксировано в `core/CLAUDE.md.template`.

---

## Контрибуции

См. [корневой README.md → секция «Контрибьюция»](../README.md#контрибьюция-для-владельцев-форков) — полный процесс (ценно/не ценно, правила, размещение, регистрация форка).

Автоматизация:
- `/aria-sync --contribute {тема}` в форке — создаёт PR в upstream с auto-labels
- `/aria-triage` в upstream — пакетная обработка PR
- При мердже — атрибуция в [CHANGELOG.md](../CHANGELOG.md) колонкой `from {fork}, PR #{N}` (гибридный формат, заменил удалённый CONTRIBUTIONS.md в Task 12)
