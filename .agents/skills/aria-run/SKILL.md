---
name: aria-run
description: Универсально маршрутизировать и выполнять задачи проекта через quick, standard или deep с намерением design, build или review.
---

# ARIA Run

## ARIA 1.3 public lifecycle

Если run содержит execution contract, настроенные команды запускаются только через
`aria verify`; verification и convergence ссылаются на полученные receipt ids.

Для новой feature-задачи использовать `aria feature --project <project> --task "<task>"` и
проходить публичные `specify`, `clarify`, `plan`, `tasks`, `implement`, `converge` из
`next_action`. Пользователь не редактирует JSON, не вычисляет SHA и не вызывает команды с
префиксом `_`. `aria run` сохраняется для quick/review и совместимости 1.1.

При импорте GitHub Spec Kit использовать `aria contract import-spec-kit`; при изменении
locked требований — только `aria contract amend` с конкретной причиной. Прямое изменение
locked Feature Contract запрещено.

1. Выполнить `py -3.12 -B -m aria doctor --project <project>`.
2. Запустить один `py -3.12 -B -m aria run --project <project> --task "<реальная задача>"`; intent/mode задавать только если пользователь явно выбрал их или auto-route ошибся.
3. Открыть `context_path` и manifest. Они содержат STATE, STACK, LLM SYSTEM_MAP с freshness, assurance plan, Git baseline, релевантную spec и точный scope для review.
4. Для standard/deep design или build до основной работы создать
   `outputs/feature-contract.json`: outcome, material requirements, acceptance oracles,
   `ambiguities_resolved`, закрытые clarifications, implementation plan и ациклический task
   graph. Каждое требование покрыть согласованными acceptance, plan и tasks. До изменения
   product code вызвать публичный `aria implement`; изменённый Git baseline блокирует lock.
   Quick этот контракт не создаёт.
5. Применить `aria-quick`, `aria-standard`, `aria-spec` или `aria-implement` по route. Роли из `manifest.role_contract.required_roles` вызвать автоматически как независимых read-only подагентов; нельзя подменять их самооценкой дирижёра.
6. Не создавать документы, если результат можно надёжно отдать ответом или кодом.
7. При deep/design явно решить, нужен ли внешний research и ADR. Сохранять только material sources, а не все просмотренные ссылки; связанные документы провести через trace spec/research/ADR/reference.
8. Исполнить все `required_execution_classes`; для `required_assessment_classes` выполнить тест либо дать честное конкретное обоснование неприменимости. Для каждого runtime-компонента в component/repository review и standard/deep build выполнить минимум один Mission Supertest: осмысленную продуктовую миссию с фокусом на SUT, реальными соседями/слоями, максимально совместимыми возможностями, применимыми adversarial/load/recovery воздействиями, oracle/invariants и side-effect read-back. Он дополняет, а не заменяет разные focused/integration векторы; невозможная внешняя часть остаётся `UNPROVEN`, а не имитируется. Связать каждый выполненный class с proof excerpts реального raw output, а нагрузочные classes — с JSON-метриками из него. Для completed standard/deep build создать `outputs/convergence.json`: requirement связать с completed task/path, а каждый acceptance — с exact contract oracle и отдельным существующим proof excerpt из class_evidence. Перед завершением подтвердить фактический output, read-back, review diff/scope и closure. Для deep/build создать traced commit по правилам `aria-implement`.
9. Каждый обязательный подагент получает только зафиксированный context/scope и возвращает JSON в `outputs/roles/<role>.json`: `schema_version`, `run_id`, `role`, независимый `agent_id`, `context_sha256`, флаги `independent: true`, `changed_code/documents: false`, `git_operations: false`, `verdict`, `summary`, `findings[{finding,evidence,resolution}]`. В result передать path и SHA каждого артефакта; отсутствие роли блокирует closure.
10. После review/аудита или тестов немедленно дать пользователю problem report, а не только PASS/FAIL: отдельно подтверждённые дефекты, риски/unknown, проблемы окружения/конфигурации и не-баги. Для каждой проблемы указать evidence, impact, доказанный production root cause либо честно отметить, что он ещё не установлен, предложить промышленный продуктовый root fix без костылей и формализма ради формализма и назвать проверки приёмки. Если проблем нет, явно назвать проверенный scope и подтверждённое отсутствие дефектов.
11. Записать один runtime result и для managed feature вызвать публичный `aria converge`; пользователь не выполняет внутренние команды вручную. Legacy-compatible run закрывает сама orchestration-обвязка. Deep/design сначала закрывается как `proposed`: показать пользователю spec и вывод attacker, остановиться и ждать явного решения. Только после фразы пользователя об утверждении создать точный approval artifact и завершить тот же design-run; build запускается отдельным run.
