# ARIA

**AI-powered development framework for Claude Code.** Универсальная система управления проектной разработкой с адаптерами под любой стек.

> **Для AI-агента:** этот документ — **внешняя витрина**. Обновляется через `/done` при триггер-событиях (новая команда, адаптер, смена архитектуры, релиз). Пользователь не редактирует руками — только через чат.
> **Для посетителя GitHub:** это стартовая точка.

---

## Что такое ARIA

ARIA — набор команд, протоколов и шаблонов для Claude Code, которые обеспечивают:

- ✅ **Production-grade разработку:** `/spec → /next-task → /review → /done` — полный конвейер с субагентами (Аналитик, Research, Code Readers, Атакер, 4 ревьюера, E2E Verifier).
- ✅ **Замкнутые контуры документов:** AI пишет, AI читает, пользователь общается с системой только через чат.
- ✅ **Адаптеры под стек:** `python-fastapi`, `kotlin-android`, `csharp-avalonia` — готовые настройки команд, антипаттернов, MCP-серверов.
- ✅ **Двустороннюю sync с upstream:** `/aria-sync` для pull, `/aria-sync --contribute` для PR, `/aria-triage` для пакетной обработки входящих.
- ✅ **Трассируемость:** task → spec → commit → files → tests в едином formate (STATE.yaml расширенный + CHANGELOG гибридный).

Полная архитектура — в [docs/SPEC.md](docs/SPEC.md). Технологический стек — в [docs/STACK.md](docs/STACK.md).

---

## Быстрый старт

### Для нового проекта на существующем адаптере

1. Установи Claude Code (Opus 4.6) и gh CLI.
2. Склонируй upstream ARIA:
   ```bash
   gh repo clone Yura1980Yura/ARIA /tmp/aria
   ```
3. В новом проекте запусти:
   ```
   /aria-init
   ```
   Команда интерактивно:
   - спросит имя/описание проекта, путь к docs и code репо, ENV-переменные
   - подберёт адаптер под твой стек
   - подставит шаблоны (CLAUDE.md, PATHS.yaml, project_config.yaml, STATE.yaml, SPEC.md, STACK.md)
   - установит hooks
   - сгенерирует `.mcp.json` из `adapter.yaml`
   - сделает первый Two-Phase Commit

4. Начни первую задачу:
   ```
   /spec {название_первой_задачи}
   /next-task
   ```

### Для существующего форка (обновление)

```
/aria-sync           # pull обновлений из upstream
/aria-sync --contribute {тема}  # PR с улучшением обратно в upstream
```

---

## Команды

| Команда | Назначение |
|---------|------------|
| `/aria-init` | Развёртывание ARIA в новом проекте (интерактивно) |
| `/spec {task}` | Создание спеки задачи (6-шаговый конвейер с субагентами) |
| `/next-task` | Работа над задачей: код → тесты → Quality Gate → /review → E2E Verify → /done |
| `/auto` | То же, но без подтверждения, потоком по задачам |
| `/review` | Ревью текущей задачи (4 параллельных субагента: Verify / CodeReview / Adversarial / E2E Integration) |
| `/done` | Two-Phase Commit (код + docs) + push + STATE/CHANGELOG/ROADMAP update |
| `/research {тема}` | Свободное исследование — обзор, гипотезы, найденные алгоритмы в REFERENCES.md |
| `/adr-new {slug}` | Создание архитектурного решения |
| `/status` | Текущее состояние: фаза, прогресс, блокеры, следующие задачи |
| `/aria-sync [--dry-run / --contribute]` | Синхронизация с upstream |
| `/aria-release v{X.Y.Z} "заголовок"` | Релиз новой версии upstream (maintainer) + уведомление форков из FORKS.md |
| `/aria-triage [--accept / --decline / --discuss]` | Пакетная обработка contribute-back PR (maintainer) |
| `/aria-docs-audit` | Аудит документации: синхронизация, живость документов, разделение репо |
| `/e2e-gate` | Полная E2E регрессия (после завершения фазы) |

Детали — в [docs/ARIA_GUIDE.md](docs/ARIA_GUIDE.md) (если создан) и самих файлах [core/commands/](core/commands/).

---

## Адаптеры

Готовые адаптеры в [adapters/](adapters/):

- **[python-fastapi](adapters/python-fastapi/)** — Python 3.12+ / FastAPI / SQLAlchemy / React / PostgreSQL + Redis. Обкатан в форках.
- **[kotlin-android](adapters/kotlin-android/)** — Kotlin 2.0+ / Jetpack Compose / Hilt / Room. Обкатан в форках.
- **[csharp-avalonia](adapters/csharp-avalonia/)** — C# .NET 10 / Avalonia 11.3 / ReactiveUI. Обкатан в форках.

Каждый адаптер содержит: `adapter.yaml` (стек, commands, mcp_servers, антипаттерны, commit_format), `hooks/pre-commit` (стек-специфичная валидация).

Для проекта без готового адаптера — `/aria-init` позволяет пропустить выбор адаптера (работа в режиме «без адаптера», ограниченная функциональность).

---

## Документация

| Документ | Назначение |
|----------|-----------|
| [docs/SPEC.md](docs/SPEC.md) | Генеральная архитектура ARIA |
| [docs/STACK.md](docs/STACK.md) | Технологический стек (decisions) |
| [docs/REFERENCES.md](docs/REFERENCES.md) | Алгоритмы из референсных проектов |
| [docs/ADR/](docs/ADR/) | Архитектурные решения |
| [docs/ARIA_GUIDE.md](docs/ARIA_GUIDE.md) | Гайд для новых пользователей |
| [docs/policies/](docs/policies/) | CHANGELOG / COMMIT / DOCUMENTATION_LIFECYCLE policies |
| [ROADMAP.md](ROADMAP.md) | Срез roadmap с прогрессом (автоген из SPEC.md) |
| [FORKS.md](FORKS.md) | Реестр известных форков |
| [TRIAGE.md](TRIAGE.md) | Входящие contribute-back PR (maintainer) |
| [CHANGELOG.md](CHANGELOG.md) | История задач по версиям |

---

## Контрибьюция (для владельцев форков)

### Что является ценной контрибуцией

| Ценно | Не ценно |
|-------|----------|
| Новый универсальный антипаттерн (встреченный в 3+ задачах) | Проект-специфичный баг |
| Улучшение команды (лучший flow, новый субагент) | Косметические изменения |
| Новый адаптер для нового стека | Копия существующего адаптера |
| Найденная проблема в протоколе/шаблоне | Проект-специфичная конфигурация |
| Новое расширение (hw-diag, safety-review, e2e-gate) | Конкретные пути/имена вашего проекта |

### Процесс (полностью автоматический через ARIA-команды)

1. **В своём форке запусти `/aria-sync --contribute {тема}`:**
   - Команда определит изменения в форке относительно последнего sync
   - Отфильтрует проект-специфику (заменит конкретные имена на `{{плейсхолдеры}}`)
   - Обобщит до upstream-уровня
   - Создаст PR в `Yura1980Yura/ARIA` с auto-labels `contribute-back` + `scope:*` по затронутым путям
2. **PR попадает в upstream `/aria-triage`** — пакетная обработка maintainer'ом.
3. **При мердже** — атрибуция в CHANGELOG (гибридный формат): `| дата | task | SHA | файлы | тесты | from {Fork}, PR #{N} |`.

### Правила контрибуций

1. **Не ломай существующее.** Новые плейсхолдеры в шаблонах — ок. Удаление/переименование существующих — требует ADR и `breaking` label.
2. **Обобщай.** Решение только для одного проекта — это не контрибуция, а расширение вашего `adapter.yaml`.
3. **Документируй причину.** PR template требует поля «Почему это ценно для других форков».
4. **Обкатанность обязательна.** PR template поле «Обкатано в проекте: N задач» — минимум 3-5 реальных задач.

### Куда размещаются контрибуции

| Тип | Куда |
|-----|------|
| Универсальное | `core/commands/`, `core/templates/*.template`, `core/CLAUDE.md.template`, `core/protocols/*` |
| Стек-специфичное | `adapters/{adapter}/adapter.yaml` → `extensions` или `antipatterns` |
| Новый стек | `adapters/{new-adapter}/` — новый адаптер (сначала обсудить через Issue) |
| Документация / правила | `docs/policies/` или `docs/ARIA_GUIDE.md` (через чат с AI, не вручную) |

Шаблон PR: [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md). Labels: [.github/labels.yaml](.github/labels.yaml).

### Регистрация нового форка

При `/aria-init` — опционально указать `owner/repo` для записи в [FORKS.md](FORKS.md). Это даёт:
- Автоуведомления при `/aria-release` через `gh issue create --repo`
- Контекст автора PR для `/aria-triage` (версия ARIA в форке, адаптер)

---

## Стек

Bash, Markdown, YAML. Git + gh CLI + Claude Code (Opus 4.6). Никакого рантайма, Docker'а, БД, CI/CD серверов.

Детали — в [docs/STACK.md](docs/STACK.md).

---

## Текущий статус

**Версия:** 3.x (в стадии Phase 1 стабилизации)
**Статус:** Phase 1 в разработке (см. [STATE.yaml](STATE.yaml) и [ROADMAP.md](ROADMAP.md))

Полный список известных форков — в [FORKS.md](FORKS.md).

---

## Лицензия

{{LICENSE}} (см. LICENSE файл).
