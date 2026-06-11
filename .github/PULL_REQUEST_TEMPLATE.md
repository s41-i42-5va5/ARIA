<!--
Шаблон PR для contribute-back от форка в upstream ARIA.
Заполняется автоматически командой /aria-sync --contribute в форке.
Читается upstream-maintainer'ом через /aria-triage.

ВАЖНО: labels contribute-back + scope:* ставятся автоматически /aria-sync --contribute.
      Руками ничего менять не надо.
-->

## Contribute-back from fork

**Fork:** {название проекта}
**Adapter:** {стек: python-fastapi / kotlin-android / csharp-avalonia / custom}
**ARIA version in fork:** {из project_config.yaml → aria.version}
**Обкатано в проекте:** {количество задач, где использовалось это изменение}

---

## Что предлагается

<!-- Описание изменений: что именно меняется в upstream ARIA и зачем. -->
{описание изменений}

---

## Почему это ценно для других форков

<!-- Обоснование универсальности: как это поможет другим форкам. -->
{обоснование универсальности}

---

## Затронутые файлы ARIA

<!-- Список файлов в core/, adapters/, scripts/, docs/ — чтобы /aria-triage правильно определил scope. -->
- `core/...`
- `adapters/...` (если адаптер-специфично)
- `scripts/hooks/...` (если затрагивает hooks)
- `docs/...` (если документация)

---

## Тесты

<!-- Что было проверено в форке. -->
- [ ] Smoke-тест: команда работает на {стек} без ошибок
- [ ] Unit-тесты: прошли (ссылка на commit в форке)
- [ ] E2E Verify: прошёл в форке (если применимо)

---

## Risks / Breaking changes

<!-- Если есть — явно указать. Если ничего не ломается — "нет". -->
{нет / список breaking changes}

---

## Связанные материалы

<!-- Ссылки на issues, research, ADR в форке или в upstream. -->
- Fork commit: {SHA обкатки}
- Related issue (upstream): #...
- ADR (если есть): {ссылка}
