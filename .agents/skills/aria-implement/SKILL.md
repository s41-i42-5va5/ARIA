---
name: aria-implement
description: Выполнять сложную реализацию по утверждённой spec через сокращённый deep/next-task-new с независимым review и adversarial tests.
---

# ARIA Implement

## ARIA 1.3

При наличии execution contract все настроенные команды выполняются через `aria verify`, а
verification/convergence используют только выданные receipt ids.

Предпочтительный вход новой реализации — `aria feature`. Зафиксировать contract командой
`aria implement`; закрыть проверенную реализацию командой `aria converge`. Если material
requirement меняется после lock, создать полную новую версию контракта и вызвать
`aria contract amend --reason <конкретная причина>` до closure.

1. Запустить `aria next-task-new --project <project> --task "<задача>" --spec <path>` либо использовать deep/build run; открыть context и проверить Git HEAD/spec.
2. Разбить реализацию на крупные проверяемые блоки, не превращая каждый шаг в отдельный документ или stage.
3. До реализации создать `outputs/feature-contract.json` с outcome, requirements,
   acceptance, закрытыми clarifications, plan и зависимыми tasks. Для новой ARIA 1.3 spec
   должна содержать точный `feature_contract_sha256`; для legacy spec контракт строится из
   spec и фактического кода без переписывания immutable документа. До product delta вызвать
   публичный `aria implement`; lock отклоняется после изменения Git baseline.
4. Дирижёру реализовать код. Git-операции, общая test-infra и итоговое решение остаются у него.
5. Автоматически вызвать независимого `c1_reviewer` на зафиксированном context и полном diff. Подагент read-only: он не меняет код, документы и Git. Дирижёр устраняет root cause находок и получает closure-ready C1 artifact.
6. Затем автоматически вызвать отдельного `adversarial_test_designer` по acceptance criteria и assurance plan. Для каждого runtime-компонента он проектирует минимум один Mission Supertest: осмысленную продуктовую миссию с фокусом на SUT, реальными соседями/слоями, максимально совместимыми возможностями, failure hypotheses, применимыми adversarial/load/recovery воздействиями, oracle/invariants и read-back всех значимых side effects. Супертест не заменяет отдельные диагностические векторы; mock не доказывает live external path. Дирижёр исполняет все required classes, сохраняет raw output, per-class proof excerpts и scenario contract в `outputs/`. После финального diff/output получить `_role-target`, затем вызвать третьего `c2_reviewer`. C1 и C2 имеют разные `agent_id` и фиксируют этот target SHA.
7. Прочитать фактический output, итоговый deliverable, diff и production wiring.
8. До closure закоммитить полную run-дельту при чистом baseline/final tree. Каждый commit снабдить `ARIA-Task: <task_id>`; последний также `ARIA-Run: <run_id>`, `ARIA-Spec: <relative path>` и `ARIA-ADR: <id>` для каждого ADR из spec.
9. До финальных ролей создать `outputs/convergence.json`: каждое requirement связано с
   completed task и реальным changed path, а каждый acceptance criterion — с exact oracle и
   отдельным proof excerpt из проверенного class_evidence. Каждый подагент возвращает `outputs/roles/<role>.json` по
   схеме role contract. Закрыть run одним result по manifest, передав path+SHA Feature
   Contract, convergence и всех трёх role artifacts.
10. Добавлять behavior lesson только при повторно полезной ошибке/коррекции/успешном приёме и только с trigger/finding/countermeasure/evidence.
11. Не использовать score, milestone, ручные SHA acknowledgments или `complete-stage` в новом проектном пути.
