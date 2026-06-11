Автогенерация ROADMAP.md из docs/SPEC.md секция Roadmap + STATE.yaml прогресс.

**Синтаксис:** `/roadmap-sync`

**PATHS:** Прочитай $ARIA_DOCS/PATHS.yaml.

---

## Что делает

ROADMAP.md — **производный документ**, не источник истины. Источник — `docs/SPEC.md` секция 6 (Roadmap) + `STATE.yaml` прогресс задач.

`/roadmap-sync` читает оба, генерирует ROADMAP.md с чекбоксами и SHA из STATE.yaml.

**Пользователь не редактирует ROADMAP.md вручную** (правило живого документа + исключение для hook-читателя). Правит SPEC.md секцию 6, затем `/roadmap-sync` регенерирует.

---

## Процедура

### 1. Прочитать SPEC.md секцию 6 (Roadmap)

Извлечь структуру фаз и задач:

```markdown
## 6. Roadmap

### Phase 1: {Название}
**Цель:** ...
**Результат:** ...

| # | Задача | Scope | Приоритет | Зависимости | Статус |
|---|--------|-------|-----------|-------------|--------|
| 11 | living-doc-rule | [DOCS]+[POLICY] | P1 | — | done 2026-04-15 |
| ... | ... | ... | ... | ... | ... |
```

### 2. Прочитать STATE.yaml

Извлечь для каждой задачи:
- `status` (not_started / in_progress / done / blocked)
- `completed` (YYYY-MM-DD) если done
- `commit` (SHA) если done

### 3. Сгенерировать ROADMAP.md

Структура:

```markdown
# ARIA — Roadmap

> **Автогенерируется** командой `/roadmap-sync` из `docs/SPEC.md` секция Roadmap + `STATE.yaml`.
> **Пользователь не редактирует этот файл вручную** — правит SPEC.md, затем запускает `/roadmap-sync`.
> **Валидатор:** pre-commit hook проверяет согласованность с SPEC.md (правило живого документа, исключение hook-читателя).

Последнее обновление: {YYYY-MM-DD}

---

## Phase 1: {Название из SPEC секция 6}

**Цель:** {...}
**Результат:** {...}
**Прогресс:** {N}/{M} задач ({процент}%)

### Статус задач

- [x] {task_11} — {description} ✓ {completed_date} {commit_short_SHA}
- [ ] {task_8} — {description} (P1, зависит от: {deps})
- [ ] {task_2} — {description} (P1)
- ...

### Блокеры
{из STATE.yaml blockers, если есть}

---

## Phase 2: {Название} (видение)

*Задачи появятся при старте фазы. Сейчас — см. раздел "Evolution & Vision" в SPEC.md.*

---

## История релизов

*Автогенерируется из CHANGELOG.md заголовков `## vX.Y.Z`.*

- v3.3 (2026-04-14) — Documentation lifecycle + Mechanical policy enforcement
- v3.0 (2026-04-14) — Первичное ядро (from forks)
```

### 4. Pre-commit валидация

pre-commit hook читает SPEC.md секцию 6 + STATE.yaml и проверяет что ROADMAP.md согласован:
- Каждая задача из SPEC.md представлена в ROADMAP.md
- Статус в ROADMAP совпадает с STATE.yaml
- Нет задач в ROADMAP, которых нет в SPEC.md

Если рассогласование — блокирует коммит с инструкцией «запусти /roadmap-sync».

---

## Когда запускать

- Автоматически в рамках `/done` шага Phase 2 (обновление docs)
- Вручную после правки SPEC.md секции 6 (добавление/изменение задач)
- Перед `/aria-release` (синхронизация roadmap с финальной версией)

---

## Замкнутый контур

- **Источник истины:** `docs/SPEC.md` секция 6 (пишется через чат с AI)
- **Прогресс:** `STATE.yaml` (пишется `/done`, `/spec`)
- **Писатель ROADMAP.md:** `/roadmap-sync` (эта команда), вызывается также в `/done`
- **Читатель ROADMAP.md:** pre-commit валидатор согласованности (механический читатель уровня hook, признан в правиле 1 CLAUDE.md.template)
- **Внешняя аудитория:** GitHub-посетители (читают ROADMAP.md как витрину прогресса)
