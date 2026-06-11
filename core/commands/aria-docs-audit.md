Аудит документации ARIA: синхронизация содержимого, живость документов, разделение репозиториев.

**Синтаксис:** `/aria-docs-audit`

**PATHS:** Прочитай `${{ENV_DOCS}}/PATHS.yaml` — все пути.

**МОДЕЛЬ:** субагенты — только `model: "opus"`.

---

## Что проверяет

### 1. Синхронизация контента (ARIA_GUIDE vs реальность)

**Команды:**
```bash
ACTUAL_COMMANDS=$(ls $ARIA_CODE/core/commands/*.md | xargs -n1 basename | sed 's/\.md$//')
CLAIMED_COMMANDS=$(grep -E '^\| `/' $ARIA_CODE/docs/ARIA_GUIDE.md | grep -oE '/[a-z-]+' | sort -u)
```
- `actual - claimed` — команды в репо, не описаны в GUIDE (**ERROR**)
- `claimed - actual` — описаны в GUIDE, файлов нет (**ERROR**)

**Адаптеры:**
```bash
ACTUAL_ADAPTERS=$(ls -d $ARIA_CODE/adapters/*/ | xargs -n1 basename)
CLAIMED_ADAPTERS=$(grep -E 'adapters/' $ARIA_CODE/docs/ARIA_GUIDE.md | ...)
```

**Политики:**
```bash
ACTUAL_POLICIES=$(ls $ARIA_CODE/docs/policies/*.md | xargs -n1 basename)
CLAIMED_POLICIES=$(grep -E 'docs/policies/' $ARIA_CODE/docs/ARIA_GUIDE.md)
```

### 2. Живость каждого документа в docs/

Для каждого `.md` в `${{ENV_DOCS}}/docs/` и `${{ENV_DOCS}}/*.yaml|*.md` в корне:

```bash
# Писатель
WRITERS=$(grep -rln "$(basename $DOC)" $ARIA_CODE/core/commands/ $ARIA_CODE/scripts/hooks/ 2>/dev/null)

# Читатель (тот же grep — команды, которые читают документ)
READERS=$(grep -rln "read.*$(basename $DOC)\|Прочитай.*$(basename $DOC)" $ARIA_CODE/core/commands/ 2>/dev/null)
```

Классификация:
- **LIVE** — есть писатель-автомат И читатель-автомат (команда/hook)
- **LIVE-external** — есть писатель-автомат, читатель — внешняя аудитория (README.md, LICENSE)
- **LIVE-hook** — есть писатель-автомат, читатель — git-hook валидатор (ROADMAP.md — исключение по правилу 1)
- **POLICY** — policy-документ, редактируется через чат с AI (исключение по правилу 3)
- **DEAD** — нет писателя-автомата ИЛИ нет читателя-автомата/hook'а/внешней аудитории

Для каждого DEAD — в отчёт с рекомендацией:
- «Реализовать контур: добавить запись в команду X» ИЛИ
- «Удалить документ: функция дублируется с Y» ИЛИ
- «Переклассифицировать в external-audience с триггером обновления в `/done`»

### 3. Разделение репозиториев (правило 4 из CLAUDE.md.template)

В `${{ENV_CODE}}` (code-repo) НЕ должно быть ни одного из запрещённых путей:

```bash
FORBIDDEN=(.claude .dev CLAUDE.md STATE.yaml CHANGELOG.md ROADMAP.md \
  SYS_CHANGELOG.md SPEC.md STACK.md REFERENCES.md \
  project_config.yaml PATHS.yaml docs reports backlog)

VIOLATIONS=()
for p in "${FORBIDDEN[@]}"; do
  if [ -e "$ENV_CODE/$p" ]; then
    VIOLATIONS+=("$ENV_CODE/$p")
  fi
done
```

Если VIOLATIONS не пуст — **FAIL** с инструкцией:
```
ARIA-инфраструктура в code-repo ЗАПРЕЩЕНА. Найдено:
  - <path1>
  - <path2>
Действие: удалить из code-repo. Всё ARIA живёт только в $ENV_DOCS.
См. правило 4 «Разделение репозиториев» в CLAUDE.md.template.
```

### 4. Синхронизация .mcp.json

```bash
if [ -e "$ENV_CODE/.mcp.json" ] && [ -e "$ENV_DOCS/.mcp.json" ]; then
  H1=$(md5sum "$ENV_CODE/.mcp.json" | cut -d' ' -f1)
  H2=$(md5sum "$ENV_DOCS/.mcp.json" | cut -d' ' -f1)
  if [ "$H1" != "$H2" ]; then
    echo "WARNING: .mcp.json расходится между docs-repo и code-repo"
    echo "  $ENV_DOCS/.mcp.json: $H2"
    echo "  $ENV_CODE/.mcp.json: $H1"
    echo "Рекомендация: скопировать из docs-repo (source of truth) в code-repo"
  fi
fi
```

### 5. Hooks покрывают политики

Проверить что каждое правило из `CHANGELOG_POLICY.md` упомянуто в `scripts/hooks/pre-commit`.
Проверить что каждый scope из `COMMIT_POLICY.md` упомянут в `scripts/hooks/commit-msg`.

Если политика введена, но hook не обновлён — политика "мертва" (только документ без механического применения).

### 6. Template содержит обязательные ссылки и правила

`core/CLAUDE.md.template` должен содержать:
- Ссылку на `CHANGELOG_POLICY.md`, `COMMIT_POLICY.md`, `DOCUMENTATION_LIFECYCLE.md`
- Секцию «Правила документов» с 5 правилами (живой документ / необходимость / policies через чат / разделение репо / контракт чтения)

Если правил нет — template не навязывает AI-агенту соблюдение → семантический контур разорван.

### 7. Контракт минимальной функциональности команд (задача 3 аудита Phase 2)

**Только для форков** (upstream ARIA пропускает эту проверку — там эталонные команды).

Прочитать `core/protocols/command_contracts.md` (из upstream).
Для каждой fork-level команды в `.claude/commands/`:

1. Найти контракт команды
2. Для каждого обязательного элемента — grep по содержимому команды в форке
3. Классификация:
   - **CONTRACT_OK** — все обязательные элементы присутствуют
   - **CONTRACT_WARN** — 1-49% элементов отсутствуют
   - **CONTRACT_FAIL** — ≥50% элементов отсутствуют

```
## 7. Контракт минимальной функциональности
| Команда | Элементов | Найдено | Отсутствует | Статус |
|---------|-----------|---------|-------------|--------|
| /auto   | 7         | 7       | 0           | OK     |
| /spec   | 5         | 4       | read_docs   | WARN   |
| /review | 5         | 2       | Adversarial, Verify, VERDICT | FAIL |
```

При CONTRACT_FAIL — **ERROR** в отчёте с рекомендацией принять upstream-версию или добавить недостающие элементы.

---

## Формат отчёта

```markdown
# ARIA Docs Audit — {YYYY-MM-DD}

## 1. Синхронизация контента
- Команды actual/claimed: 11/11 — SYNC
- Адаптеры actual/claimed: 3/3 — SYNC
- Политики actual/claimed: 3/3 — SYNC

## 2. Живость документов
Всего документов: 18
- LIVE (AI↔AI): 13
- LIVE-external (README, LICENSE): 1
- LIVE-hook (ROADMAP): 1
- POLICY (через чат): 3
- DEAD: 0

## 3. Разделение репозиториев
- code-repo чист: PASS
ИЛИ
- ❌ VIOLATIONS: .claude/, CLAUDE.md, .dev/ в code-repo
  Действие: удалить из code-repo.

## 4. .mcp.json
- Идентичен в обоих репо: OK

## 5. Hooks vs Policies
- pre-commit покрывает CHANGELOG_POLICY: OK
- commit-msg покрывает COMMIT_POLICY: OK

## 6. Template
- Ссылки на политики: OK
- Секция «Правила документов» с 5 правилами: OK

## 7. Контракт минимальной функциональности (только для форков)
- /auto: OK (7/7 элементов)
- /spec: OK (5/5)
- /review: OK (5/5)
- ... (все команды)

## Итог
SYNC — документация актуальна, все контуры замкнуты, разделение репо соблюдено, контракты выполнены.
```

Или при нарушениях — DRIFT/FAIL с перечнем действий.

---

## Правила

- Аудит **не делает изменения автоматически** — только отчёт
- Пользователь решает что обновить, AI правит файлы в том же диалоге (правило 3 — через чат)
- На CI: блокирует merge в main при любом FAIL (нарушение разделения репо, мёртвые документы, расхождение контента)
- WARNING не блокирует merge (например, .mcp.json расхождение — мягкая проверка)
