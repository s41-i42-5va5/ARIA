# ARIA — Реестр форков

> **Писатели:** `/aria-init` (новый форк — добавляет запись в upstream FORKS.md через PR), `/aria-sync` (обновляет `Последний sync` и `ARIA version` форка).
> **Читатели:** `/aria-release` (уведомления через `gh issue create --repo`), `/aria-triage` (контекст автора входящего PR).
> Пользователь не редактирует этот файл вручную — только через команды.

**Формат Repo:** `owner/repo` (GitHub convention, используется для `gh issue create --repo`). Пустая ячейка `—` — форк без публичного GitHub-репо.

---

## Активные форки

| Проект          | Стек                     | Адаптер         | Repo | ARIA version | Последний sync |
| --------------- | ------------------------ | --------------- | ---- | ------------ | -------------- |
| SOLAR AUTOPILOT | Kotlin + Android         | kotlin-android  | Yura1980Yura/SOLAR-AUTOPILOT | 3.3          | 2026-04-14     |

---

## Архивированные форки (неактивные 90+ дней)

| Проект | Стек | Адаптер | Repo | ARIA version | Дата архивации | Причина |
|--------|------|---------|------|--------------|----------------|---------|

*(Пусто.)*

---

## Политика поля Repo

1. **Заполнение:** при `/aria-init` пользователь опционально указывает `owner/repo`. Если указал — upstream-maintainer получает PR в upstream FORKS.md.
2. **Нужно для:** `/aria-release` ШАГ 7 — уведомление форков через `gh issue create --repo {owner/repo}`.
3. **Без Repo:** форк не получит автоматические уведомления. Владелец форка сам следит через `/aria-sync`.

## Политика поля ARIA version

1. **Формат:** semver `X.Y.Z` или `X.Y` (если patch неважен).
2. **Обновление:** автоматически `/aria-sync` после успешного pull (читает upstream CHANGELOG.md последнюю версию).
3. **Используется:** `/aria-triage` для определения совместимости входящего PR (PR от форка на ARIA 3.1 в upstream 3.4 может конфликтовать — нужна проверка).

## Политика поля Последний sync

1. **Формат:** `YYYY-MM-DD`.
2. **Обновление:** `/aria-sync` при каждом успешном pull.
3. **Используется:** `/aria-release` может отфильтровать неактивные форки (>90 дней) — предложить архивацию.

---

## Связанные документы

- [TRIAGE.md](TRIAGE.md) — входящие PR от форков
- [CHANGELOG.md](CHANGELOG.md) — атрибуция при мердже PR (`from {Fork}, PR #{N}`)
- [core/commands/aria-sync.md](core/commands/aria-sync.md) — команда обновления версии/даты sync
- [core/commands/aria-release.md](core/commands/aria-release.md) — команда уведомления форков
- [core/commands/aria-init.md](core/commands/aria-init.md) — команда инициализации нового форка
