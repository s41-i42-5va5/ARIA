Релиз новой версии ARIA upstream. Только для maintainer'а ARIA.

**Синтаксис:** `/aria-release v{X.Y.Z} "{заголовок версии}"`

**PATHS:** Прочитай $ARIA_DOCS/PATHS.yaml.
**МОДЕЛЬ:** субагенты — только `model: "opus"`.

## Что делает

1. Проверяет что все коммиты с последнего тега имеют записи в CHANGELOG.md (гибридный формат scope+таблицы).
2. Агрегирует CHANGELOG: `v{X.Y.Z}-dev` → `v{X.Y.Z} ({дата})` с финальным заголовком.
3. Создаёт git tag `v{X.Y.Z}` и пушит.
4. Читает `FORKS.md` — предлагает уведомить форки через `gh issue create --repo`.

---

## ШАГ 1: Валидация pre-release

```bash
cd "$ARIA_CODE"

# Последний тег
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")

# Коммиты с последнего тега
if [ -n "$LAST_TAG" ]; then
    COMMITS=$(git log "$LAST_TAG"..HEAD --oneline)
else
    COMMITS=$(git log --oneline)
fi
```

Для каждого коммита определить scope из первой строки (`[CORE]`, `[DOCS]`, ...) и затронутые файлы.

---

## ШАГ 2: Проверка политики

Для каждого критичного scope (Core, Adapters, Docs, Hooks, Breaking, Fix):
- В `v{X.Y.Z}-dev` секции CHANGELOG.md есть соответствующая строка таблицы?
- Если нет — блокировать релиз, предложить добавить.

Проверить формат строк (6 колонок: дата/task/SHA/файлы/тесты/атрибуция).

---

## ШАГ 3: Автогенерация финального заголовка

Прочитай CHANGELOG.md секцию `## v{X.Y.Z}-dev`. Замени на:

```markdown
## v{X.Y.Z} ({YYYY-MM-DD}) — {заголовок версии}

### Core
| строки таблицы остаются как есть |
...
```

Заголовок версии (`{заголовок}`) — одно-два предложения, подчеркивают главное в релизе.

---

## ШАГ 4: Интерактивная доработка (AskUserQuestion)

Показать пользователю черновик финального заголовка. Пользователь может:
- Уточнить формулировку заголовка
- Изменить порядок scope-секций (если важно подчеркнуть приоритет)
- Добавить секцию "Migration notes" если есть breaking changes

---

## ШАГ 5: Применение

```bash
# Обновить CHANGELOG.md (замена v{X.Y.Z}-dev → v{X.Y.Z})

# Коммит релиза
git -C "$ARIA_CODE" add CHANGELOG.md
git -C "$ARIA_CODE" commit -m "[META] release v{X.Y.Z}: {заголовок}

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"

# Тег
git -C "$ARIA_CODE" tag -a "v{X.Y.Z}" -m "{заголовок}"

# Push + push tag
git -C "$ARIA_CODE" push origin main
git -C "$ARIA_CODE" push origin "v{X.Y.Z}"
```

---

## ШАГ 6: Уведомление форков (опционально, через FORKS.md)

> Исторический контекст (Task 10 FIX): раньше ШАГ 7 пытался читать CONTRIBUTIONS.md как источник форков — это не работало (в CONTRIBUTIONS.md не было колонки Repo). Task 10 исправил это: теперь читаем FORKS.md.
> ШАГ 6 из исходной версии (CONTRIBUTIONS update) — удалён, атрибуция теперь пишется через /done и /aria-triage --accept в табличные строки CHANGELOG.

### 6.1 Прочитай FORKS.md

Выбери из таблицы «Активные форки» строки, где поле `Repo` непустое (не `—`).

### 6.2 Спросить пользователя (AskUserQuestion)

```
Уведомить форки о релизе v{X.Y.Z}?
Найдено форков с Repo: N
  - {Project} (python-fastapi) → owner/repo
  - {Project} (kotlin-android) → owner/repo
  ...

Варианты:
  1. Уведомить все
  2. Уведомить выборочно (выбрать из списка)
  3. Пропустить уведомление
```

### 6.3 Создать issues в выбранных форках

```bash
CHANGELOG_URL="https://github.com/Yura1980Yura/ARIA/blob/v{X.Y.Z}/CHANGELOG.md#v{XYZ}"

for REPO in $SELECTED_REPOS; do
  "$GH" issue create --repo "$REPO" \
    --title "ARIA v{X.Y.Z} выпущена — {заголовок версии}" \
    --body "Новая версия ARIA доступна.

Changelog: $CHANGELOG_URL

Запустите \`/aria-sync\` для обновления вашего форка. Команда проанализирует изменения и предложит применить совместимые с вашим кодом (статусы NEW / UPSTREAM_AHEAD).

Если обнаружите несовместимость или баг — открывайте issue в upstream https://github.com/Yura1980Yura/ARIA/issues" \
    --label "aria-release"
done
```

### 6.4 Обновить FORKS.md

Для каждого уведомлённого форка — обновить поле `ARIA version` в FORKS.md до `v{X.Y.Z}` НЕ обновляется сейчас (это сделает `/aria-sync` в форке при успешном pull). Вместо этого — добавить WARN в колонку с датой уведомления если нужно.

Commit FORKS.md пропускается — /aria-release не редактирует FORKS.md.

---

## Правила

- **Не релизить** если pre-commit/pre-push hooks не прошли для всех коммитов в range
- **Не релизить** если CHANGELOG не заполнен для критичных scopes (все изменения core/adapters/docs/hooks должны иметь строки в таблице)
- **MAJOR релиз** (vX.0.0) — только после обсуждения breaking changes, явная секция `### Breaking` в CHANGELOG
- **PATCH** (vX.Y.Z) — только для bugfix'ов и правок типографики
- **ШАГ 6** опционален — можно пропустить, форки увидят обновление при очередном `/aria-sync` (без автоуведомления)
