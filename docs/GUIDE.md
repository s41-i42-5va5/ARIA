# Руководство пользователя ARIA Codex 1.5

Полный базовый manual test ARIA 1.5.2 и отдельный протокол работы двух компьютеров
через Google Drive находятся в
[MANUAL_TEST_1_5_2.md](MANUAL_TEST_1_5_2.md).

## Первый пользователь и доступ

После `aria init` создайте device identity и один раз активируйте access policy:

```powershell
aria identity enroll --actor local-owner --device my-pc `
  --output .\owner-enrollment.json
aria --identity-actor local-owner --identity-device my-pc `
  access bootstrap --project example
aria access status --project example
```

Для участника сначала добавьте его actor id и роли в `ARIA_TEAM.yaml`. Он выполняет
`identity enroll` на своей машине и передаёт JSON enrollment request. Maintainer разрешает
точные действия и scopes через `access grant`. Закрытый ключ не передаётся.

Рабочий backlog:

```powershell
aria --identity-actor alice backlog list --project example --version 1.5.5
aria --identity-actor alice backlog claim --project example `
  --item BLG-ID --expected-revision 4
aria --identity-actor alice backlog done --project example `
  --item BLG-ID --evidence run:RUN_ID --expected-revision 5
aria --identity-actor alice backlog audit --project example
```

`claim` отклоняется, пока хотя бы одна указанная dependency не имеет status `done`.
Ручной `backlog done` принимает только доказательство, которое ARIA может прочитать:
локальный завершённый run этого проекта (`run:RUN_ID`) либо полный 40-символьный SHA
текущего Git HEAD (`git:COMMIT`). Старый, сокращённый или отсутствующий commit и произвольная
текстовая ссылка не закрывают задачу; рабочее дерево должно быть чистым. Git evidence
фиксирует точное состояние checkout,
но для разработки предпочтителен `run:RUN_ID`: только run содержит проверенные тесты,
changed files и convergence. Автоматический `backlog sync` закрывает связанный run item
только после чтения `result.json` и совпадения его SHA с completed manifest.

Для проекта с `PROJECT.yaml.governance` build использует уже существующую задачу:

```powershell
aria governance check --project example
aria --identity-actor alice --identity-device my-pc backlog claim `
  --project example --item BLG-ID --expected-revision CURRENT
aria --identity-actor alice --identity-device my-pc feature `
  --project example --task 'Реальный результат' --backlog-item BLG-ID
aria preflight --project example --operation write --run RUN-ID
```

До успешного write preflight продуктовые файлы не изменяют. Dirty checkout без started
bound run, несколько active build runs, несогласованный terminal run/backlog или принятое
решение при открытой clarification дают `RECOVERY_REQUIRED`. `governance plan` только
показывает изменения; `governance reconcile` применяет явно перечисленные `--item` с
`--expected-revision`, `--confirm-acceptance` и подписанной identity.

ARIA проверяет backlog-доступ по фактической текущей ветке зарегистрированного Git
checkout. `--branch` можно передать явно, но несовпадение с текущей веткой блокирует
команду. Это не позволяет выдать `main` за разрешённую `feature/*`.

Legacy `feature`/`run` автоматически добавляют run-derived элементы. Governed build связан
с claimed каноническим item и не создаёт его дубликат. Повторный `backlog sync` находит
review findings, failed verification и blockers без дубликатов.

## Команда и Trusted CI

Новый проект получает `ARIA_TEAM.yaml` и пустую fail-closed `TRUST.yaml`. Сначала добавьте
реальных участников с уникальными actor ids и ролями, затем создайте Ed25519-ключи и внесите
публичные ключи в policies. Приватные ключи хранятся вне project docs и Git.

Параллельные задачи координируются через revision и lease:

```powershell
aria team status --project example
aria team claim --project example --task T-001 --actor alice `
  --expected-revision 0 --ttl-seconds 3600
aria team release --project example --task T-001 --actor alice `
  --token LEASE_TOKEN --expected-revision 1
```

Два claim одной задачи с одинаковой revision дают ровно одного владельца. Устаревшая
revision и неверный token блокируются. Несколько людей работают в отдельных runs/ветках;
live-редактирование одного run не поддерживается.

Trusted CI:

```powershell
aria ci prepare --project example --run RUN_ID --output .\aria-ci-job.json `
  --private-key .\job-authorizer.pem --actor alice
aria ci execute --job .\aria-ci-job.json --checkout D:\Projects\example `
  --output .\unsigned.zip --job-trust-policy .\TRUST.yaml --job-policy job
aria ci attest --job .\aria-ci-job.json --result .\unsigned.zip `
  --output .\result.aria-evidence --private-key .\ci-private.pem `
  --actor github-actions --job-trust-policy .\TRUST.yaml --job-policy job
aria ci import --project example --run RUN_ID --job .\aria-ci-job.json `
  --package .\result.aria-evidence --trust-policy .\TRUST.yaml --policy ci `
  --job-trust-policy .\TRUST.yaml --job-policy job
```

`execute` запускает команды в отдельном Git worktree точного commit и сверяет исходный
checkout до/после. Import отвергает другой/просроченный job, nonce, commit, contract,
неизвестный/revoked/expired key и повторный import.

Для объединения параллельных результатов подготовьте новый CI job на итоговом commit,
передав каждый исходный package через повторяемый `--integration-source`. Затем gate требует
точно этот набор source hashes и независимого reviewer:

```powershell
aria evidence review-attest --project example --target-commit COMMIT `
  --source-package .\alice.aria-evidence --source-package .\bob.aria-evidence `
  --integration-package .\integration.aria-evidence --reviewer carol `
  --private-key .\carol-private.pem --output .\review.aria-evidence
aria gate integration --project example `
  --source-package .\alice.aria-evidence `
  --source-package .\bob.aria-evidence `
  --integration-package .\integration.aria-evidence `
  --target-commit COMMIT --trust-policy .\TRUST.yaml `
  --review-package .\review.aria-evidence --review-policy reviewer `
  --output .\integration-verdict.json
```

Обычный PASS отдельных runs не заменяет integration run на итоговом commit.

Для GitHub Actions:

```powershell
aria ci github --project example --output .\.github\workflows\aria-ci.yml
```

Workflow устанавливает ARIA и зависимости только из hash-pinned wheelhouse archive,
заданного через `ARIA_WHEELHOUSE_URL` и `ARIA_WHEELHOUSE_SHA256`.
Job `execute` не получает signing secret; отдельный защищённый job `attest` использует
`ARIA_SIGNING_KEY_PEM`. ARIA не привязана
к GitHub как к формату evidence.

## Проверяемое исполнение

Если в `PROJECT.yaml` настроен документ `verification`, run фиксирует его snapshot и не
принимает вручную составленные test evidence. После реализации выполните:

```powershell
aria verify --project <project> --run <run-id> --links .\verification-links.json
```

В `verification-links.json` ключи внутри `commands` — id команд из `VERIFY.yaml`; значения
содержат `requirement_ids` и `acceptance_ids`. Для повторного принудительного запуска используйте
`--no-resume`, для выбора части команд — повторяемый `--command <id>`. Выбранные команды всё
равно должны покрыть все обязательные execution classes run. После PASS используйте receipt
id из результата в `verification[].execution_id` и acceptance proof convergence.

Не редактируйте `execution/contract.json`, receipts, bundle или raw logs. Изменение product
Git после проверки намеренно инвалидирует Evidence Bundle: сначала завершите изменения,
затем повторите `aria verify`.

## Рекомендуемый путь 1.2

Для нового Git-проекта:

```powershell
aria init --project example --code-root 'D:\Projects\example'
aria identity enroll --actor local-owner --device workstation `
  --output .\owner-enrollment.json
aria --identity-actor local-owner --identity-device workstation `
  access bootstrap --project example
aria doctor --project example
aria --identity-actor local-owner --identity-device workstation `
  feature --project example --task 'Добавить проверяемую возможность'
```

После `feature` Codex следует `next_action` и выполняет публичные фазы `specify`, `clarify`,
`plan`, `tasks`, `implement`, `converge`. Это команды агента, а не ручная анкета пользователя.
Пользователь формулирует outcome и отвечает только на действительно материальное уточнение.

Текущий этап можно прочитать без изменения run:

```powershell
aria lifecycle --project example --run RUN_ID
```

Если Feature Contract изменился после lock, Codex вызывает `aria contract amend` с новой
валидной версией и конкретной причиной. Прямое редактирование locked artifact без amendment
ломает SHA-chain и блокирует closure.

Spec Kit import/export:

```powershell
aria contract import-spec-kit --project example --run RUN_ID --spec-dir .\specs\001-feature
aria contract export-spec-kit --project example --run RUN_ID --output-dir .\spec-kit-export
```

Для безопасного interchange каждое acceptance scenario, plan и task должно содержать явные
ссылки `[FR-001]` на requirements из `spec.md`. Это небольшое расширение Markdown, которое
не мешает Spec Kit, но не позволяет ARIA сфабриковать coverage при импорте.

Поддерживаемый interchange соответствует основному потоку GitHub Spec Kit: spec, clarify,
plan, tasks и implement. ARIA добавляет к нему lock, requirement coverage, независимые роли,
verification evidence и converge.

Release acceptance:

```powershell
aria release-check --output 'D:\ARIA-Acceptance\candidate-1.5'
```

Целевая папка должна отсутствовать. Команда никогда не перезаписывает прежний отчёт.

## Главное

Вся работа ведётся в Codex-чате. Пользователь формулирует задачу обычным языком. Codex сам
запускает ARIA, читает контекст, работает с проектом, вызывает независимых агентов, проводит
тесты и закрывает run. Пользователю не нужны SHA, score, milestones и внутренние команды.

## Первый запуск

Канонические папки не включают обязательный `workspace`:

```text
C:\Tools\aria-codex             framework ARIA 1.5.5
D:\Projects\example             code_root, отдельный Git checkout продукта
D:\Projects\example-aria        docs_root проекта ARIA
%LOCALAPPDATA%\ARIA-Codex       локальный registry, keys, locks и evidence
```

Нельзя вкладывать framework, `code_root`, `docs_root` или runtime друг в друга. Каталог
`workspaces` допустим только как выбранная пользователем группировка; ARIA не придаёт ему
никакого значения.

Для совершенно нового локального продукта сначала создайте отдельный Git-репозиторий и
зафиксируйте исходный baseline:

```powershell
New-Item -ItemType Directory 'D:\Projects\example'
git -C 'D:\Projects\example' init -b main
# добавьте реальные исходники и manifest проекта
git -C 'D:\Projects\example' add .
git -C 'D:\Projects\example' commit -m 'Initial product baseline'
```

Затем из установленной ARIA создайте и зарегистрируйте документы. Если `--docs-root`
не задан, `init` использует соседнюю папку `<code_root-name>-aria`:

```powershell
aria --framework-root 'C:\Tools\aria-codex' init `
  --project example `
  --code-root 'D:\Projects\example' `
  --docs-root 'D:\Projects\example-aria'
```

`init` делает только детерминированный inventory bootstrap. После него Codex обязан прочитать
реальные исходники и manifests продукта, исправить неподтверждённые элементы `STACK.md` и
`SYSTEM_MAP.yaml`, затем создать device identity и активировать доступ:

```powershell
aria --framework-root 'C:\Tools\aria-codex' identity enroll `
  --actor local-owner --device my-pc `
  --output '.\owner-enrollment.json'
aria --framework-root 'C:\Tools\aria-codex' `
  --identity-actor local-owner --identity-device my-pc `
  access bootstrap --project example
aria --framework-root 'C:\Tools\aria-codex' doctor --project example
aria --framework-root 'C:\Tools\aria-codex' `
  --identity-actor local-owner --identity-device my-pc `
  feature --project example --task 'Первая проверяемая возможность'
```

Начинать в `shadow`. После прохождения doctor, реального run и isolated canary Codex может
выполнить скрытую активацию. Каждый проект активируется отдельно.

Если документы ARIA уже существуют, вместо `init` используется только локальная регистрация:

```powershell
aria --framework-root 'C:\Tools\aria-codex' register --project example `
  --docs-root 'D:\Projects\example-aria' `
  --code-root 'D:\Projects\example'
```

`--framework-root` является глобальной опцией и указывается до `init`, `register`, `doctor`,
`run` или другой команды. CLI сверяет установленный wheel с operational framework root и
блокирует module drift.

ARIA различает проекты через локальный `%LOCALAPPDATA%\ARIA-Codex\projects.toml`: один
`project_id` связывает абсолютный путь к папке документов и абсолютный путь к одному Git
репозиторию. Название текущей папки и сам чат не используются как догадка о проекте.

Если проект передан другому пользователю, ему нужны ARIA, папка документов проекта и Git
checkout. На своей машине он выполняет тот же `register` с локальными путями, затем просит
Codex проверить и активировать проект. Existing STATE/HISTORY/specs сохраняются, а runtime,
locks и machine paths создаются локально заново.

Если проекта в ARIA ещё нет, пользователь указывает Codex Git root и желаемую папку
документов. `aria init` детерминированно создаёт инвентаризационный bootstrap
`PROJECT.yaml`, `STATE.yaml`, `STACK.md`, `SYSTEM_MAP.yaml`, `HISTORY.jsonl` и каталогов.
Это ещё не семантическая карта архитектуры. Codex обязан прочитать реальный код, уточнить
STACK и SYSTEM_MAP, настроить identity/access, выполнить doctor и только затем запускать
первую feature. Универсальный bootstrap нельзя выдавать за завершённый анализ проекта.

ARIA не создаёт и не требует папку `workspaces`. Каждый `project_id` связывается напрямую
с верхним уровнем одного Git-репозитория через `code_root`. Каталог с именем `workspaces`
может быть только пользовательским способом группировки репозиториев.

## Обычная задача

```powershell
py -3.12 -B -m aria run --project example --task 'Исправить повторную доставку события'
```

Codex открывает context и действует по route:

- quick — локально и без spec;
- standard — короткий план, реальная проверка и независимое review;
- deep/design — proposal и явное утверждение пользователя;
- deep/build — реализация утверждённой spec, C1, adversarial tests и C2;
- review — immutable scope, coverage, runtime evidence и findings.

Для standard/deep design и build Codex сначала создаёт Feature Contract. Пользователь не
заполняет JSON вручную: контракт выводится из задачи, релевантной spec и кода. Он содержит
outcome, requirements, acceptance oracles, закрытые clarifications, implementation plan и
зависимый task graph. До изменения product code ARIA замораживает SHA контракта только при
неизменном Git baseline. Completed standard/deep build дополнительно требует convergence —
связь requirements с выполненными tasks/changed paths и отдельное доказательство каждого
acceptance oracle фактическим test proof excerpt. Quick-задачи этого контракта не требуют.

## Почему тестов может быть больше

ARIA смотрит не на размер diff, а на риск. Изменение одной строки в auth, общей схеме,
очереди либо координатной математике может иметь большой blast radius. Assurance plan
показывает обязательные классы и причину каждого усиления.

Для E2E/load/recovery Codex строит связный сценарий. Пример:

> параллельная отправка → повтор события → падение worker → рестарт → восстановление →
> исчерпание quota → отмена → проверка отсутствия потерь и дублей.

Если реальная инфраструктура недоступна, Codex не пишет PASS. Он отделяет выполненное,
непроверенное и точный внешний blocker.

## Review

Для компонента:

```powershell
py -3.12 -B -m aria run --project example --task 'Проверить очередь' `
  --intent review --target-type component --target backend/queue
```

Для всего проекта:

```powershell
py -3.12 -B -m aria run --project example --task 'Полный release audit' `
  --intent review --target-type repository
```

Repository review дороже: это assurance campaign. Он проверяет систему по компонентам,
общим примитивам, критическим потокам и runtime-рискам, а не перечисляет style замечания.

Для component и repository review порядок обязателен:

1. immutable scope;
2. LLM-карта `functional-coverage.md`;
3. независимый поиск пропущенной функциональности;
4. разные тестовые векторы, выведенные из карты и assurance plan;
5. минимум один Mission Supertest на runtime-компонент;
6. problem report пользователю;
7. findings, file/dimension coverage, read-back и closure.

Карта — свободный Markdown, а не таблица типов нод. Она разбирает функции и ожидаемое
поведение, каждое существенное поле/вход/выход/состояние, реальные UI/API/backend/runtime
bindings, ошибки, границы, вложенность и взаимодействия. При наличии spec LLM сверяется с
ней; без spec выводит ожидания из кода и явно отделяет observed, inferred и unknown. ARIA
машинно проверяет только наличие пяти смысловых разделов, SHA и привязку к scope. Полноту
семантики оспаривает независимый LLM-рецензент; его verdict привязан к SHA карты и точному
verification contract, поэтому после review нельзя незаметно заменить классы, сценарий или
evidence при тех же логах.

Problem report появляется сразу после review или тестирования, а не только после внутреннего
closure. Он отдельно перечисляет подтверждённые дефекты, риски и неизвестные, проблемы
окружения/конфигурации и не-баги. Каждая проблема получает evidence, влияние, доказанный
production root cause либо честный следующий шаг диагностики, промышленное продуктовое
решение причины и набор проверок приёмки. Review-only предлагает ремонт, но не меняет код
без запроса. В build Codex устраняет корневую причину; изменение теста, подавление ошибки и
локальный обход не считаются ремонтом. «Промышленное» не означает переусложнение: решение
должно быть минимально достаточным, но учитывать безопасность, данные, совместимость,
наблюдаемость, эксплуатацию и реальный масштаб проекта.

Mission Supertest — одна осмысленная продуктовая миссия с фокусом на проверяемом блоке. Она
проходит через реальные соседние ноды/UI/API/worker/storage, связывает максимально
совместимые возможности и пытается нарушить ключевые invariants применимыми adversarial,
load/stress и crash/recovery воздействиями. В карте заранее фиксируются purpose, SUT,
capabilities, neighbors, failure hypotheses, oracle, expected/forbidden outcomes,
side effects, correlation, load/fault profile, read-backs и ограничения результата. Один
супертест не обязан искусственно совмещать взаимоисключающие режимы и не заменяет focused
тесты. Если browser, hardware или live provider недоступны, эта часть остаётся `UNPROVEN`,
а не объявляется PASS по mock.

## SYSTEM_MAP

```powershell
py -3.12 -B -m aria map-status --project example
```

`valid: false` блокирует run: карта сломана или относится к другому проекту. `fresh: false`
не блокирует, потому что код мог измениться параллельно, но Codex обязан перепроверить
затронутые компоненты. Только deep repository review может сформировать и опубликовать новую карту; component review карту не заменяет, а candidate не может удалить уже известную архитектуру.

## Статус и история

```powershell
py -3.12 -B -m aria status --project example
py -3.12 -B -m aria history --project example --verify
```

STATE хранит только текущую рабочую карту. HISTORY хранит последовательность доказанных
событий. Git хранит код, diff и commits. Runtime хранит тяжёлые raw logs и artifacts.

## Диагностика

```powershell
py -3.12 -B -m aria doctor
py -3.12 -B -m aria projects
py -3.12 -B -m aria doctor --project example
```

Framework doctor не зависит от переменных окружения продукта. Project doctor проверяет
реестр, roots, Git identity, manifests, STATE/HISTORY, map, budgets и runtime read-back.

## Что делать нельзя

- создавать второй framework;
- копировать Git history в STATE;
- считать exit code доказательством без output/read-back;
- заменять E2E переименованным unit test;
- закрывать repository review без file/dimension coverage;
- выдавать imported foreign commit pointer за существующий commit текущего repo;
- редактировать legacy-import;
- расширять review scope после старта;
- запускать active без canary и engine binding.
