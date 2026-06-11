# ARIA Git Hooks

Механическая валидация политик ARIA. Запускается автоматически при коммите и push.

## Установка

```bash
bash scripts/hooks/install.sh
```

Скрипт настраивает `core.hooksPath=scripts/hooks` — hooks живут в репе, а не в `.git/hooks/`.

## Hooks

| Hook | Что проверяет | Документ-политика |
|------|---------------|-------------------|
| `pre-commit` | Изменения в core/adapters/policies/hooks → CHANGELOG обновлён | `CHANGELOG_POLICY.md` |
| `commit-msg` | Формат первой строки коммита `[SCOPE] описание` | `COMMIT_POLICY.md` |
| `pre-push` | При push в `Yura1980Yura/ARIA` — CHANGELOG был обновлён в range | `CHANGELOG_POLICY.md` |

## Обход (только в экстренных случаях)

```bash
git commit --no-verify    # обход pre-commit и commit-msg
git push --no-verify      # обход pre-push
```

Если использовано — указать причину в сообщении коммита/PR.

## Отключение

```bash
git config --unset core.hooksPath
```

## Синхронизация с политиками

Если меняются hook-правила — ДОЛЖНЫ быть обновлены и `docs/policies/*.md`. Это проверяется вручную, см. `scripts/hooks/tests/` (тесты на соответствие).

## Тесты

```bash
bash scripts/hooks/tests/run_all.sh
```

Проверяет что hooks блокируют/пропускают коммиты согласно политикам.
