# Commit Policy

Обязательная политика для upstream ARIA и рекомендованная для форков.

---

## Формат сообщения коммита

```
[SCOPE] краткое описание

Подробное описание (необязательно, но рекомендовано для Core/Breaking изменений):
- что именно сделано
- почему
- как проверить

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

**Первая строка:** `[SCOPE] описание` — до 72 символов, с глаголом в прошедшем времени ("добавил", "исправил", "обновил").

---

## Scopes (префиксы) для ARIA upstream

| Scope | Что покрывает | Обязательное CHANGELOG обновление |
|-------|---------------|-----------------------------------|
| `[CORE]` | `core/commands/`, `core/templates/`, `core/CLAUDE.md.template` | Да |
| `[PROTOCOL]` | `core/protocols/` | Да |
| `[ADAPTER]` | `adapters/{name}/` | Да |
| `[POLICY]` | `docs/policies/` | Да |
| `[DOCS]` | `docs/ARIA_GUIDE.md`, `docs/README.md`, `docs/SPEC.md`, `docs/STACK.md`, `README.md` корневой (включая секцию «Контрибьюция») | Да |
| `[HOOKS]` | `scripts/hooks/`, `scripts/validate_*.sh` | Да |
| `[FIX]` | Баг-фиксы | Да (в категории Fix) |
| `[META]` | `.gitignore`, `LICENSE`, `.github/` | Нет |

---

## Two-Phase Commit (для форков)

Форки, использующие junction `project-docs/` → Obsidian, разделяют коммиты:

- **Phase 1 (код):** `git add <code-files>` → `[КОМПОНЕНТ] описание`
- **Phase 2 (docs):** `git add project-docs/` → `[DOCS] описание`

**Никогда** не смешивать код и docs в одном коммите.

Для upstream ARIA Two-Phase необязателен (нет junction), но рекомендован разделять infra/docs.

---

## Обязательные правила

1. **Один коммит — одна логическая цель.** Не смешивать несвязанные изменения.
2. **Не использовать `--no-verify`** кроме экстренных случаев. Если использовано — в сообщении объяснить причину.
3. **Не использовать `--amend`** после push. Создать новый коммит.
4. **Не использовать `git push --force`** в `main` / `develop`.
5. **Co-Authored-By** обязателен для коммитов, созданных с участием Claude.
6. **Сообщение коммита — на русском**, если проект двуязычный — в соответствии с CLAUDE.md проекта.

---

## Для upstream ARIA дополнительно

- Любой коммит в `main` требует соответствующей записи в `CHANGELOG.md` (см. [CHANGELOG_POLICY.md](CHANGELOG_POLICY.md))
- Изменения в `core/protocols/` — обычно MAJOR версия, требуют PR с обсуждением
- Изменения в `adapters/` из форка через contribute-back — PR с ссылкой на sync report

---

## Механическая валидация

`scripts/hooks/commit-msg` проверяет:
1. Формат первой строки: `^\[[A-Z]+\] .+` (блокирует)
2. Длина первой строки ≤ 72 символов (предупреждение)
3. Scope из списка стандартных (предупреждение)
4. Наличие `Co-Authored-By` (предупреждение)

`scripts/hooks/pre-commit` проверяет:
1. CHANGELOG обновлён для scopes помеченных "Да" в таблице выше (блокирует)
2. ARIA_GUIDE.md актуален при изменениях в core/adapters/policies (предупреждение)
