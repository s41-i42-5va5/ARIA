# Модель памяти

ARIA разделяет память по назначению, чтобы не загружать LLM архивом при каждой задаче.

| Память | Назначение | Загрузка |
|---|---|---|
| `STATE.yaml` | Единая project map: frontier или roadmap | Каждый run получает компактную проекцию, полный файл fingerprint-ится |
| `STACK.md` | Технологии, manifests, команды и verification contract | Каждый run полностью |
| `SYSTEM_MAP.yaml` | LLM-карта компонентов, shared primitives и critical flows | Каждый run получает проверенную карту и freshness |
| `HISTORY.jsonl` | Полная hash-linked история полезных событий | Chain/checkpoint всегда; старые события по запросу |
| Active specs/research/ADR | Контракт, evidence и решения сложной работы | Только по trace релевантной standard/deep задачи |
| Archive/legacy | История решений и pipeline | Только по точной ссылке/исследовательскому запросу |
| Git | История кода | Через Git; ARIA хранит base/head, commit refs/trailers и identity изменённых файлов, но не diff/log |
| Behavior lessons | Ошибки, коррекции и работающие приёмы с countermeasure | До 12 project/task-релевантных активных lessons |
| Runtime | Raw manifests, context, scope, results, snapshots | Для текущего run и диагностики |

Feature Contract является переносимой памятью намерения для standard/deep работы: outcome,
requirements, acceptance, clarifications, plan и tasks сохраняются в trace. Convergence
сохраняет доказанный итог requirement coverage. Raw JSON остаётся в runtime, а portable
STATE/HISTORY trace содержит контракт, SHA и компактный verdict без копирования test logs.

Одна schema STATE содержит `focus`, `current`, `stages`, `issues` и history checkpoint. Профиль `frontier` разрешает постепенно достраивать pipeline; профиль `roadmap` включает проверенные зависимости и автоматический next-task. Отдельный ROADMAP не хранится. Старые большие STATE сохранены sealed snapshot в project `legacy-import`; они не копируются в активный контекст и не объявляются текущей истиной.

`STATE.task.trace` — текущий индекс цепочки documents → run → commits → HISTORY. Сам `HISTORY` содержит portable trace события: она сохраняет changed file identities, ADR/research/reference links и commit range даже после удаления local runtime. Подробный diff остаётся в Git, текст решения — в spec/ADR/research.

Для импортированной истории допустим `legacy_implementation` с честной полнотой `pointer_only`/`listed_commits`. Такие SHA сохраняются как исторические ссылки и не выдаются за объекты зарегистрированного Git. Новый implementation trace всегда проверяется против текущего code root.

User approval и независимые role artifacts сохраняются в portable HISTORY result для нового deep-контура. Raw JSON остаётся в runtime, но HISTORY содержит достаточные SHA, agent roles, verdict и approval identity, чтобы позднее доказать границу design/build без зависимости от этой машины.

Долговременная память `%LOCALAPPDATA%\ARIA-Codex\behavior\lessons.jsonl` не использует score и milestone. Lesson создаётся только для повторно полезного паттерна и обязан содержать trigger, finding, countermeasure и evidence. События append-only и hash-linked; resolution выключает исправленный lesson из будущего контекста.
