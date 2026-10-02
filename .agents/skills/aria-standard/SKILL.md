---
name: aria-standard
description: Выполнять обычные средние задачи через standard-режим ARIA с полезным контекстом, одним независимым review и реальными тестами.
---

# ARIA Standard

## ARIA 1.3

Если run содержит execution contract, все настроенные команды запускаются через
`aria verify`, а closure использует только выданные receipt ids.

Если run создан через `aria feature`, сохранить фазовые Markdown-артефакты публичными
командами lifecycle. После `tasks` вызвать `aria implement`, а итоговый result передать в
`aria converge`. Внутренние `_lock-feature-contract` и `_close-run` не использовать.

1. Использовать manifest/context, созданные `aria run`; проверить Git HEAD и релевантную spec, если она подключена.
2. Сформировать короткий измеримый план без документов, если отдельный документ не нужен продукту.
3. Создать `outputs/feature-contract.json` по manifest: outcome, requirements, acceptance,
   закрытые clarifications, plan и task graph. Это runtime-контракт работы, а не новый
   постоянный документ проекта. Для managed feature перед product delta вызвать публичный
   `aria implement`; lock обязан видеть исходный Git baseline.
4. Дирижёру реализовать изменение и выполнить все classes из assurance plan по `STACK.md`; для runtime-компонента добавить минимум один осмысленный Mission Supertest с реальными соседями, применимыми adversarial/load/recovery воздействиями и side-effect read-back, не заменяя им focused/integration векторы. Raw output сохранить в `outputs/` run, сложные E2E/load сценарии описать по scenario contract.
5. Автоматически вызвать независимого `independent_reviewer` из role contract: передать зафиксированный context и итоговый diff, запретить изменения кода/документов/Git, сохранить его проверяемый JSON в `outputs/roles/independent_reviewer.json`. При находке дирижёру исправить корневую причину, повторить затронутые проверки и получить closure-ready verdict.
6. Прочитать реальный output и изменённый deliverable. После review/тестов сразу сообщить пользователю проблемы с evidence, impact, production root cause и промышленным root fix; отдельно отметить unknown, environment/config blockers и не-баги. Для build устранить причину, а не маскировать симптом, и повторить проверки приёмки.
7. Перед финальным независимым review создать `outputs/convergence.json`: все requirements должны иметь
   status `proven`, completed task и changed path; каждый acceptance result повторяет exact
   oracle контракта и содержит отдельный реальный proof excerpt. Затем закрыть run одним JSON-result по
   `manifest.closure_contract.result_fields`, включая path+SHA contract/convergence и role
   artifacts, через публичный `aria converge`. Не использовать score, acknowledgments и `complete-stage`.
8. При schema/security/concurrency/migration/public API/cross-layer риске перейти в `deep`.
