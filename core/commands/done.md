Завершить текущую задачу (current.task из STATE.yaml). Two-Phase Commit и push.

**PATHS: Прочитай ${{ENV_DOCS}}/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**Все тесты (unit, Quality Gate, E2E Verify) уже пройдены в /next-task или /auto.**
**Здесь ТОЛЬКО коммит, обновление документации и push.**

**МОДЕЛЬ: ВСЕ субагенты ОБЯЗАНЫ запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**

---

## 1. TWO-PHASE COMMIT

### Phase 1 — Code Commit (только код проекта)

Компонент из пути: {{CODE_COMPONENT_MAPPING}}

```bash
# Stage ТОЛЬКО код (НЕ project-docs/)
git -C "${{ENV_CODE}}" add {{CODE_DIRS}}

# НЕ добавляй project-docs/

# Commit
git -C "${{ENV_CODE}}" commit -m "[КОМПОНЕНТ] описание"

# Save hash
git -C "${{ENV_CODE}}" rev-parse --short HEAD > "${{ENV_CODE}}/.commit_id_phase1"
cat "${{ENV_CODE}}/.commit_id_phase1"
```

### Phase 2 — Docs Commit (только документация)

```bash
COMMIT_ID=$(cat "${{ENV_CODE}}/.commit_id_phase1")

# Обновить docs-файлы:
# - STATE.yaml: задача → status: done, commit: $COMMIT_ID, completed: сегодня, current.task → next
# - STATE.yaml: добавить запись в секцию sessions за сегодня: "{task} ({SHA}) — краткий итог, тесты N/M"
# - CHANGELOG.md: строка в таблице соответствующей scope-секции v{X.Y.Z}:
#   | дата | task | $COMMIT_ID | files | tests | атрибуция? |
# - SYS_CHANGELOG.md: если менялась система (commands, CLAUDE.md, templates)
# - ROADMAP.md: автоген из SPEC.md (через /roadmap-sync) — [ ] → [x] с датой и $COMMIT_ID
#
# ПРОВЕРИТЬ: REFERENCES.md актуален (если /spec добавлял референсы)
#
# ТРИГГЕР-СОБЫТИЯ (DOCUMENTATION_LIFECYCLE.md):
# - Изменена архитектура → обновить SPEC.md секцию
# - Изменён стек → обновить STACK.md секцию
# - Новая команда/адаптер/политика → обновить ARIA_GUIDE.md
# - Значимые изменения API/фич → обновить README.md (внешняя витрина)
#
# Все записи НА РУССКОМ

git -C "${{ENV_CODE}}" add project-docs/
git -C "${{ENV_CODE}}" commit -m "[DOCS] registry update — code commit $COMMIT_ID"
```

### Phase 3 — Push + Verification

```bash
git -C "${{ENV_CODE}}" push origin main

COMMIT_ID=$(cat "${{ENV_CODE}}/.commit_id_phase1")
git -C "${{ENV_CODE}}" show --oneline --no-patch $COMMIT_ID
grep "$COMMIT_ID" "${{ENV_DOCS}}/CHANGELOG.md"
grep "$COMMIT_ID" "${{ENV_DOCS}}/STATE.yaml"

rm "${{ENV_CODE}}/.commit_id_phase1"
```

### Recovery при FAIL

| Check | FAIL | Recovery |
|-------|------|----------|
| Hash не в CHANGELOG | Исправить → amend Phase 2 |
| Hash не в STATE | Исправить → amend Phase 2 |
| Push rejected | pull --rebase && push |

**ЗАПРЕЩЕНО:** amend для Phase 1 (code). Только Phase 2 (docs) в recovery.

---

## 2. Выведи: {task} done ({SHA}). Следующая: {next_task}

## 3. /clear — очистка контекста для следующей задачи.
