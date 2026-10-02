# ARIA Codex — рабочие правила

## Назначение

ARIA — LLM-first контур для работы Codex над сложными проектами. Codex анализирует,
проектирует, меняет код, запускает реальные проверки и привлекает независимых агентов.
ARIA даёт ему компактный контекст, карту системы, risk-based assurance, неизменяемый
review scope и доказательное закрытие. Git остаётся единственной историей кода.

## Обязательный вход

1. Отвечать пользователю по-русски и объяснять специальные термины простыми словами.
2. Для новой feature-задачи продукта сначала выполнить `aria doctor --project <project>`
   и `aria governance check --project <project>`. Затем выбрать и атомарно claim существующий
   backlog item, запустить один `aria feature --project <project> --task "<реальная задача>"
   --backlog-item <BLG-ID>` и до первой записи выполнить
   `aria preflight --project <project> --operation write --run <RUN-ID>`. Для quick/review
   и legacy-compatible маршрутов допустим `aria run`. Если governance/control plane
   недоступен, preflight требует recovery или отсутствует активный run, product writes
   запрещены до восстановления либо явно утверждённого внешнего аварийного процесса.
   Только roadmap-проект может взять next-task без `--task`.
3. Открыть возвращённые `context_path`, `manifest_path` и, для review, `scope_path`.
4. Следовать `route`, `assurance_plan`, `role_contract` и фактическому `STACK.md`.
5. Изменение самой `aria-codex` выполнять прямо в framework root через framework doctor,
   tests и `aria-review`; продуктовый run для self-change не создавать.

## Режимы

- `quick` — маленькая low-risk задача без spec и лишних ролей.
- `standard` — средняя задача, один план и независимое review.
- `deep` — сложная/high-risk задача: `spec` для design, `next-task-new` для build.
- Намерение независимо: `design`, `build`, `review`.

Явное занижение режима допустимо только с предупреждением о найденном риске.

Standard/deep design и build до основной работы создают `outputs/feature-contract.json`:
outcome, requirements, acceptance oracles, `ambiguities_resolved`, clarifications, plan и
ациклический task graph. Completed standard/deep build создаёт `outputs/convergence.json` и
до изменения product code замораживает контракт через `_lock-feature-contract` на неизменном
Git baseline. Convergence связывает каждое требование с completed task и Git path, а каждый
acceptance criterion — с точным oracle и отдельным proof excerpt из проверенного
class_evidence. Пропуск или `unproven` closure не проходит. Quick не получает этот gate.

`manifest.json`, `contract-anchor.json` и `feature-contract-lock.json` — control artifacts
движка: агент не создаёт и не редактирует их вручную. Его writable surface внутри run —
только объявленные артефакты `outputs/`. Совместная подмена всего runtime процессом того же
OS user находится вне trust model; для hostile worker требуется отдельная ACL/signing boundary.

## Карта и контекст

- `SYSTEM_MAP.yaml` — LLM-кэш семантической архитектуры: компоненты, уровни, домены,
  shared primitives, critical flows, риски и test seams. Код остаётся источником истины.
- Каждый run проверяет Git HEAD и working-tree fingerprint карты. Устаревшую карту нужно
  перепроверить по коду; repository review может опубликовать свежий candidate через closure.
- Изменение shared primitive расширяет blast radius до зависимых компонентов и требует
  integration/E2E/adversarial/full regression.
- `STATE.yaml` — компактная project map; `HISTORY.jsonl` — hash-linked история; `STACK.md`
  задаёт реальные manifests и канонические команды. Архив/legacy читается только по ссылке.
- В `shadow`-режиме пустой frontier является штатным: активная работа живёт в local runtime,
  а STATE/HISTORY не изменяются. Доказательством активной write-задачи служит started run,
  связанный с claimed `BACKLOG.yaml` item, а не сам по себе непустой frontier.

## Тестирование и evidence

- Уровень задаёт `manifest.assurance_plan`, а не привычка агента.
- Если manifest содержит execution contract, настроенные команды запускаются только через
  `aria verify`; verification и convergence ссылаются на его receipt ids. Ручной запуск не
  является trusted evidence.
- Focused используется для локального поведения; cross-layer/deep требует E2E и
  adversarial; concurrency/shared state — concurrency/load/stress; recovery/data-loss —
  recovery/chaos; schema/migration — migration/rollback; release/repository audit —
  full regression и применимый soak.
- Сложный тест проверяет связку действий и содержит initial state, actions, expected и
  forbidden result, side effects, correlation, parallelism/load и actual result.
- После каждого теста прочитать raw output или side effect. В evidence сохранить command,
  classes, exit code, output path/SHA, фактический excerpt, actual result и `class_evidence` с отдельными proof excerpts каждого класса. Один связный extreme-сценарий может доказывать несколько классов; его proof excerpts и load metrics обязаны реально присутствовать в raw output. Один exit code
  не доказывает корректность.
- FAIL означает поиск production root cause. Тесты и fixtures не подгонять под дефект.
- Для каждого runtime-компонента в component/repository review и standard/deep build
  обязателен минимум один **Mission Supertest** с фокусом на тестируемый блок. Это одна
  осмысленная продуктовая миссия через реальные соседние компоненты/ноды и слои, которая
  связывает максимально совместимые функции SUT и пытается сломать его применимыми
  adversarial, concurrency/load/stress и recovery/chaos воздействиями.
- Mission Supertest не заменяет разные focused/integration тесты. Functional coverage
  связывает каждую существенную возможность либо с супертестом, либо с отдельным вектором;
  взаимоисключающие режимы и локализующие negative cases не вталкиваются искусственно в
  один сценарий. Минимум один супертест обязателен, дополнительные определяются critical
  flows и рисками.
- Сложность, нагрузка и отказы выводятся из реальной задачи, SLO/ёмкости и failure
  hypotheses, а не добавляются ради масштаба. Сценарий фиксирует purpose, SUT focus,
  covered capabilities, соседей, oracle/invariants, initial state, actions, expected и
  forbidden results, side effects, correlation, load profile, faults, read-backs, actual
  result и честные ограничения доказательства.
- Browser/UI-компонент проверяется через настоящий браузер. Детерминированный provider или
  controlled service допустим для воспроизводимого load/fault теста, но не доказывает live
  external integration: для неё нужен отдельный live canary либо явный `UNPROVEN`.
- Если обязательный супертест невозможно выполнить из-за внешней инфраструктуры, он всё
  равно проектируется, а результат остаётся `BLOCKED/UNPROVEN`; его нельзя подменить mock,
  unit-loop или изменением security policy.

## Обязательный отчёт после review и тестов

- Сразу после review, аудита или выполнения тестов вывести пользователю итоговый перечень
  проблем; не ограничиваться PASS/FAIL, closure или ссылкой на evidence. Если дефектов нет,
  явно сообщить об этом и назвать проверенный scope.
- Разделять подтверждённые дефекты, риски/неопределённости, проблемы окружения или
  конфигурации и события, которые не являются багами. Не выдавать симптом или гипотезу за
  production root cause: если причина ещё не доказана, так и написать и указать следующий
  диагностический шаг.
- Для каждой проблемы дать severity, затронутый компонент, фактическое evidence, влияние,
  production root cause и рекомендуемое исправление причины. Рекомендация должна быть
  промышленным продуктовым решением, согласованным с архитектурой, безопасностью,
  целостностью данных, совместимостью/миграцией, наблюдаемостью, эксплуатацией и масштабом
  проекта. Локальный обход, подавление ошибки или подгонка теста не считаются ремонтом.
- Предлагать минимально достаточное полное решение без лишней платформы и формализма. Для
  ремонта заранее назвать проверки приёмки: regression и применимые integration/E2E,
  adversarial, load/recovery/migration проверки.
- Review-only не изменяет код без запроса, но всё равно предлагает лучший root fix. В build
  подтверждённая проблема устраняется по корневой причине, затем повторяются затронутые и
  соседние проверки.

## Review и аудит

- Review фиксирует root, точный список файлов и scope SHA до анализа; scope после старта
  не меняется. Новый scope требует нового run.
- Coverage artifact обязан отметить `reviewed` каждый captured source-файл, точно учесть excluded inventory с reason/disposition и пройти каждое измерение assurance plan. `skipped` для captured source не допускается. Пустой findings допустим только при полном coverage.
- Runtime-sensitive вывод подтверждается реальным тестом. Repository review — assurance
  campaign с E2E/adversarial/concurrency/load/stress/recovery/full regression и явной
  оценкой soak/chaos/migration/rollback/security-isolation.
- Findings содержат severity, file:line, конкретное evidence, влияние и минимальный root fix.
- Review не исправляет код без отдельного запроса.

## Независимые агенты

- Роли из `manifest.role_contract.required_roles` вызываются автоматически и read-only.
- Они получают фиксированный context/scope, не меняют код/docs/Git и возвращают
  `outputs/roles/<role>.json` с независимым agent id, verdict, findings/evidence/resolution. Артефакт создаётся после финального предмета проверки и фиксирует `target_kind`/`target_sha256` из `_role-target`.
- C1 и C2 выполняют разные агенты. Самооценка главного агента не заменяет роль.

## Закрытие и границы

- Полезная цепочка: изменение/результат → фактические проверки → read-back → review
  итогового diff/scope → один closure result.
- Пользователь не выполняет `_close-run` или `_lock-feature-contract` вручную: публичные
  `aria implement` и `aria converge` вызывают gates. Score, milestones, ручные SHA-ack и
  `complete-stage` не используются.
- Deep/build требует clean baseline/final tree и traced commits; quick/standard не получают
  искусственный commit gate.
- Код продукта остаётся в одном Git root. Machine registry и runtime хранятся только в
  `%LOCALAPPDATA%\ARIA-Codex`; framework, product docs и code root физически разделены.
- Shadow не меняет project docs. Active пишет STATE/HISTORY, approved design artifacts и
  проверенный SYSTEM_MAP recoverably через lock, preimage, atomic replace и read-back.
- Imported legacy-копии не редактировать. `context.git_ignore_prefixes` имеет узкий allowlist только для boundary `project-docs`; им нельзя скрыть source/runtime каталоги.
- SYSTEM_MAP публикует только deep repository review; новая карта обязана сохранять уже известные компоненты, shared primitives, critical flows и dimensions.

## Проверка framework

```powershell
$env:PYTHONUTF8 = '1'
py -3.12 -B -m aria doctor
py -3.12 -B -m unittest discover -s tests -v
py -3.12 -B -m aria doctor --project my-project
py -3.12 -B -m aria doctor --project solar-autopilot
py -3.12 -B -m aria history --project my-project --verify
```

Архитектура и пользовательская инструкция: `docs/ARCHITECTURE.md` и `docs/GUIDE.md`.
