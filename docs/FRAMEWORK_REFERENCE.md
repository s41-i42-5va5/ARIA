# ARIA 1.5

ARIA 1.5.5 добавляет исполнимый governance-контур и сохраняет исправления полноты аудита
репозиториев из 1.5.3.
Базовая полная ручная проверка ARIA 1.5.2, включая безопасную работу
одного проекта с двух компьютеров через Google Drive:
[docs/MANUAL_TEST_1_5_2.md](docs/MANUAL_TEST_1_5_2.md).

Пошаговая эксплуатационная инструкция для одного владельца, двух компьютеров, общего
`docs_root` в Google Drive и исходного кода через Git:
[docs/TWO_COMPUTERS_ONE_PROJECT_1_5_2.md](docs/TWO_COMPUTERS_ONE_PROJECT_1_5_2.md).

ARIA 1.5.5 добавляет executable governance поверх совместимого контура 1.5. Для проекта с
контрактом `PROJECT.yaml.governance` новый build начинается только из clean Git baseline,
после claim существующего `BACKLOG.yaml` item и с `--backlog-item`. Перед первой записью:

```powershell
aria governance check --project example
aria --identity-actor alice --identity-device workstation `
  backlog claim --project example --item BLG-... --expected-revision CURRENT
aria --identity-actor alice --identity-device workstation `
  feature --project example --task 'Добавить проверяемую возможность' --backlog-item BLG-...
aria preflight --project example --operation write --run RUN-ID
```

`governance check` является read-only. Если он возвращает `RECOVERY_REQUIRED`, product
files не изменяют до reconciliation. Принятые решения сначала просматривают через
`aria governance plan --project example`; применение требует точных `--item`,
`--expected-revision`, `--confirm-acceptance` и локальной подписывающей identity. ARIA не обещает перехват
произвольной записи процессом того же OS user: такая запись находится вне local trust model.

## Каноническая структура папок

ARIA не создаёт и не требует папку `workspaces`. Для каждого проекта используются четыре
раздельные границы:

```text
C:\path\to\aria-codex          framework root ARIA 1.5.5
D:\Projects\example            code_root: верхний уровень Git-репозитория продукта
D:\Projects\example-aria       docs_root: документы ARIA; это значение по умолчанию для init
%LOCALAPPDATA%\ARIA-Codex      локальный registry, device keys и runtime evidence
```

Папка `workspaces` допустима только как пользовательский контейнер для нескольких Git-
репозиториев. ARIA не ищет её, не регистрирует и не связывает с особой логикой. Framework,
`code_root`, `docs_root` и runtime не должны быть вложены друг в друга.

## Что нового в 1.5

ARIA 1.5 добавляет проверяемую совместную работу без центрального сервера:

- device identity Ed25519; закрытый ключ хранится в локальном runtime и защищается
  Windows DPAPI для текущего пользователя;
- подписанный `ACCESS.yaml` с правами по проекту, версии и Git-ветке;
- один подписанный `BACKLOG.yaml` с creator/assignee, optimistic revision, блокировкой
  `claim` до завершения зависимостей, evidence-bound completion только по проверенному
  `run:<run-id>` или текущему `git:<full-sha>` при чистом worktree и точным источником
  каждого элемента;
- автоматическое добавление runs, review findings, verification failures и blockers;
- автоматическое закрытие или блокировка связанного backlog item по итоговому статусу run;
- безопасная идемпотентная миграция закрытого проекта 1.4 в 1.5.

Первичная настройка нового проекта:

```powershell
aria init --project example --code-root D:\Projects\example
aria identity enroll --actor local-owner --device workstation `
  --output .\owner-enrollment.json
aria --identity-actor local-owner --identity-device workstation `
  access bootstrap --project example
aria doctor --project example
aria --identity-actor local-owner --identity-device workstation `
  feature --project example --task 'Добавить проверяемую возможность'
```

Новый участник создаёт ключ на своей машине и передаёт только enrollment request:

```powershell
aria identity enroll --actor alice --device alice-laptop `
  --output .\alice-enrollment.json
aria --identity-actor local-owner access grant --project example `
  --request .\alice-enrollment.json --permission project.read `
  --permission run.create --permission run.advance --permission backlog.read `
  --permission backlog.write --permission backlog.claim --permission backlog.close `
  --version '1.5.*' --branch 'feature/*' --expected-revision 1
```

Email и имя компьютера — только метаданные, не доказательство личности. Для нескольких
машин один actor получает отдельный device key на каждой машине. Рекомендуемый транспорт
общих документов — Git. Google Drive допустим только по последовательному single-writer
протоколу из инструкции для двух компьютеров; ARIA не синхронизирует Drive и не создаёт
межкомпьютерный lock. Real-time редактирование одного run без coordinator не заявляется.
Backlog-команды получают branch-контекст из зарегистрированного Git checkout. Явный
`--branch` допустим как fail-closed assertion: значение обязано совпасть с фактической
текущей веткой.

## Что нового в 1.4

ARIA 1.4 добавляет Team & Trusted CI поверх локального trusted execution 1.3:

- переносимый Evidence Package v2 с SHA-256 inventory и подписью Ed25519;
- offline `evidence inspect/verify` и fail-closed trust policies;
- изолированный протокол `ci prepare → execute → attest → import`, связанный с commit, nonce,
  Feature Contract и Execution Contract;
- actors, roles, task leases и optimistic project revision для параллельной работы;
- integration gate, который требует новый CI run на итоговом commit и точный набор
  исходных evidence packages;
- reference adapter для GitHub Actions.

```powershell
aria key generate --private-key .\aria-private.pem --public-key .\aria-public.pem
aria team status --project example
aria team claim --project example --task T-001 --actor alice --expected-revision 0

aria ci prepare --project example --run RUN_ID --output .\aria-ci-job.json `
  --private-key .\job-authorizer.pem --actor alice
aria ci execute --job .\aria-ci-job.json --checkout D:\Projects\example `
  --output .\ci-unsigned.zip --job-trust-policy .\TRUST.yaml --job-policy job
aria ci attest --job .\aria-ci-job.json --result .\ci-unsigned.zip `
  --output .\ci-result.aria-evidence --private-key .\aria-private.pem `
  --actor github-actions --job-trust-policy .\TRUST.yaml --job-policy job
aria ci import --project example --run RUN_ID --job .\aria-ci-job.json `
  --package .\ci-result.aria-evidence --trust-policy .\TRUST.yaml --policy ci `
  --job-trust-policy .\TRUST.yaml --job-policy job
```

Приватный ключ не включается в evidence. Верная подпись сама по себе не означает доверие:
`TRUST.yaml` отдельно задаёт разрешённые ключи, срок действия, revocation и минимальный
уровень `local`, `signed` или `ci-signed`. Evidence Bundle v1 читается как `local`, но не
может удовлетворить `ci-signed` policy.

`ci-signed` подтверждает проверку receipts и подпись разрешённым CI-attester для доверенного
commit. Worktree/process isolation не является hostile-code sandbox: недоверенные forks
требуют отдельный одноразовый runner/VM и внешнюю платформенную аттестацию.

## Что нового в 1.3

ARIA 1.3 добавляет Trusted Execution & Evidence: тест считается доказательством только если
его запустила сама ARIA из структурированного `VERIFY.yaml` и сохранила проверяемый receipt.

```powershell
aria verify --project example --run RUN_ID --links .\verification-links.json
aria converge --project example --run RUN_ID --result .\result.json
```

`aria init` выводит `VERIFY.yaml` только из реально найденных Git manifests. Поддерживаются
адаптеры `python`, `node`, `rust` и `go`; команды исполняются без shell, с allowlist
исполняемых файлов, timeout, ограничением output и маскированием секретов. На каждый запуск
фиксируются команда, cwd, Git HEAD/worktree, immutable execution contract, exit code,
timestamps и SHA raw output. Повторный вызов безопасно использует PASS-receipt только при
полном совпадении команды, контракта и Git state.

Evidence Bundle закрывается fail-closed: отсутствующий класс проверки, FAIL/timeout,
изменённый output, contract drift или product Git delta после теста блокируют closure.
`verification-links.json` связывает исполнение с requirements и acceptance criteria:

```json
{
  "schema_version": 1,
  "commands": {
    "test": {
      "requirement_ids": ["R-001"],
      "acceptance_ids": ["AC-001"]
    }
  }
}
```

Команда пригодна для локального запуска и CI: JSON-результат выводится в stdout, а любой
неуспешный обязательный запуск возвращает ненулевой exit code.

## Что нового в 1.2

ARIA 1.2 добавляет публичный spec-driven lifecycle поверх гарантий 1.1:

`specify → clarify → plan → tasks → implement → converge`.

Один обычный вход:

```powershell
aria feature --project example --task 'Добавить проверяемый health endpoint'
```

Codex проходит возвращённые фазы сам. Пользователь не редактирует Feature Contract JSON,
не вычисляет SHA и не вызывает внутренние команды. `aria implement` проверяет и фиксирует
контракт до product delta; `aria converge` проверяет requirement coverage, evidence и closure.

Управляемая редакция уже зафиксированного контракта сохраняет предыдущую версию, причину,
SHA до/после и цепочку run contract:

```powershell
aria contract amend --project example --run RUN_ID `
  --contract .\updated-feature-contract.json `
  --reason 'Обнаружено обязательное поведение восстановления'
```

Совместимость с GitHub Spec Kit использует его реальные Markdown-артефакты `spec.md`,
`plan.md`, `tasks.md`:

```powershell
aria contract import-spec-kit --project example --run RUN_ID --spec-dir .\specs\001-feature
aria contract export-spec-kit --project example --run RUN_ID --output-dir .\exported-spec-kit
```

Импорт намеренно fail-closed: неразрешённый `NEEDS CLARIFICATION`, отсутствие формальных
requirements, acceptance scenarios, checklist tasks или точных `[FR-001]`-ссылок в
acceptance/plan/tasks блокируют создание готового Feature Contract. ARIA не приписывает всем
tasks все requirements автоматически.

Новый проект создаётся из фактического Git inventory:

```powershell
aria init --project example --code-root 'D:\Projects\example'
```

ARIA детерминированно обнаруживает tracked manifests/source roots, создаёт инвентаризационный
bootstrap STACK/SYSTEM_MAP/STATE, регистрирует проект в shadow и не перезаписывает существующую
папку документов. Этот bootstrap не считается семантическим анализом архитектуры: до первой
feature Codex обязан прочитать реальный код, проверить и уточнить STACK и SYSTEM_MAP.

Полная приёмка релизного кандидата запускается одной командой:

```powershell
aria release-check
```

Команда создаёт чистый venv, дважды воспроизводимо собирает wheel с привязанным к candidate
commit `SOURCE_DATE_EPOCH`, требует совпадения SHA-256, устанавливает wheel offline из
локального bundle, проверяет console entry point, package provenance/version и installed
doctor, затем выполняет disposable init/doctor/status/history/map/routes/canary/Feature Contract
lock, реальную focused/integration/E2E/adversarial миссию, коллизию двух backlog claim,
version/branch rejection, evidence-bound completion, active cutover и полную регрессию с
closure/convergence cases. Кандидат обязан быть чистым и закоммиченным. Итог сохраняется в
`release-acceptance.json`, raw output — в отдельных logs.

ARIA — LLM-first система инженерной работы и assurance для Codex. Она не заменяет LLM
детерминированным workflow-engine и не заменяет CI. Codex принимает инженерные решения,
читает код, меняет его, запускает реальные инструменты и привлекает независимых агентов.
ARIA обеспечивает правильный контекст, память проекта, карту системы, проверяемый scope,
risk-based тестовый контракт и защиту от ложного завершения.

## Что стало боевым ядром

- один project-aware путь: `doctor → run → работа Codex → verify → evidence → closure`;
- структурированный execution contract и проверяемый Evidence Bundle;
- локальный реестр нескольких проектов;
- `quick / standard / deep` независимо от `design / build / review`;
- обязательный Feature Contract для standard/deep design и build: outcome,
  requirements, acceptance oracles, resolved clarifications, plan и task graph,
  замороженные до изменения product Git baseline;
- convergence gate для standard/deep build: каждое требование связано с завершённой
  задачей и changed path, а каждый acceptance criterion — с отдельным проверенным proof;
- LLM `SYSTEM_MAP.yaml` с компонентами, shared primitives и critical flows;
- автоматический blast-radius при изменении общих примитивов;
- assurance plan с focused, integration, E2E, adversarial, concurrency, load, stress,
  soak, recovery, chaos, migration/rollback, security isolation и full regression;
- LLM Functional Coverage Map между immutable scope и тестами: функции, поля, bindings,
  ошибки, композиция и test obligations без domain-specific схем;
- обязательный Mission Supertest на runtime-компонент: одна осмысленная продуктовая миссия
  через реальные соседние ноды/слои с попыткой сломать SUT нагрузкой, adversarial и
  recovery воздействиями;
- неизменяемый review scope и полное file/dimension coverage;
- независимые Codex-роли для standard/deep/review;
- raw test output с SHA, проверяемым excerpt и описанием фактического результата;
- обязательный post-assurance problem report с production root cause, влиянием,
  промышленным продуктовым решением и проверками приёмки;
- recoverable active closure через lock, preimage, atomic replace и read-back;
- компактный `STATE.yaml`, hash-linked `HISTORY.jsonl` и Git trace без копии Git.

Старый standalone workflow engine, scoreboard, migration-gates, ручные acknowledgments,
profile-команды и stage-церемонии удалены из рабочего framework. Они сохранены во внешнем
бэкапе миграции, но не участвуют в runtime 1.2.

## Быстрый старт

Требуется Git for Windows. Bundle schema 3 содержит собственный side-by-side
Python 3.12 runtime; системный Python и Windows registry он не изменяет. На каждом компьютере
из framework root один раз выполните:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` проверяет SHA-256 каждого artifact, подпись Python Software Foundation внутри
side-by-side runtime, устанавливает ARIA без сети, выполняет `pip check` и `doctor`, ставит
Codex skill и создаёт `%LOCALAPPDATA%\ARIA-Codex` для локального runtime. Команда
`aria.cmd` добавляется в user `PATH`; новый shell увидит её без ручной настройки. Если на
машине уже есть рабочий Python 3.12, ARIA использует его только для создания собственного
venv и не заявляет на него право владения.

Для повторного создания повреждённой среды:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Recreate
```

После установки:

```powershell
$Framework = (Resolve-Path .).Path
$InstallRoot = Split-Path -Parent $Framework
$AriaExe = "$InstallRoot\bin\aria.cmd"

& $AriaExe --framework-root $Framework register `
  --project my-project `
  --docs-root 'D:\Projects\my-project-aria' `
  --code-root 'D:\Projects\my-project'

& $AriaExe --framework-root $Framework doctor --project my-project
& $AriaExe --framework-root $Framework run `
  --project my-project `
  --task 'Исправить обработку повторной доставки'
```

Для нестандартного каталога установки:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 `
  -InstallRoot 'C:\ARIA' `
  -RuntimeRoot 'C:\ARIA-State'
```

ARIA разрешает такое разделение только когда все Python-модули wheel побайтово совпадают с
`aria/` указанного framework root. Module drift и произвольный альтернативный root блокируются.
Полная матрица релизной приёмки находится в `docs/RELEASE_ACCEPTANCE.md`.

Git-команды имеют конечный бюджет 60 секунд. Для очень большого или медленного репозитория
его можно явно увеличить через `ARIA_GIT_TIMEOUT_SECONDS` (допустимо 1–1800), не включая
бесконечные retry.

`run` возвращает:

- `manifest_path` — неизменяемый контракт;
- `context_path` — компактный контекст для Codex;
- `scope_path` — точный scope review;
- route, assurance level и следующее действие.

Пользователь работает только в Codex-чате. В lifecycle 1.2 Codex вызывает публичный
`aria converge`; внутренний `_close-run` остаётся только compatibility primitive движка.

## Публичные команды

```text
aria doctor [--project ID]
aria projects
aria upgrade-1-4 --project ID
aria upgrade-1-5 --project ID
aria register --project ID --docs-root PATH --code-root PATH
aria init --project ID --code-root PATH [--docs-root PATH]
aria identity enroll|whoami ...
aria access bootstrap|status|grant|revoke|audit ...
aria backlog add|list|show|assign|claim|block|done|sync|audit ...
aria feature --project ID --task TEXT
aria lifecycle --project ID --run RUN_ID
aria specify|clarify|plan --project ID --run RUN_ID --input PATH
aria tasks --project ID --run RUN_ID --input PATH --contract PATH
aria implement --project ID --run RUN_ID
aria verify --project ID --run RUN_ID [--links PATH]
aria converge --project ID --run RUN_ID --result PATH
aria contract import-spec-kit|export-spec-kit|amend ...
aria release-check [--output PATH]
aria key generate --private-key PATH --public-key PATH
aria evidence export|verify|inspect ...
aria team status|claim|release ...
aria ci prepare|execute|attest|import|github ...
aria gate integration ...
aria run --project ID --task TEXT [--intent ...] [--mode ...]
aria spec --project ID --task TEXT
aria next-task-new --project ID --task TEXT --spec PATH
aria status --project ID [--task TASK_ID]
aria history --project ID --verify
aria map-status --project ID
```

Незавершённый run привязан к точной версии engine. После upgrade framework его нужно
перезапустить; ARIA не переносит открытый run на новый engine неявно.

## Режимы

| Режим | Когда | Процесс |
|---|---|---|
| `quick` | локальная low-risk правка | прямое выполнение + focused evidence |
| `standard` | несколько связанных частей | короткий план + независимое review + нужные тесты |
| `deep` | security/schema/concurrency/safety/cross-layer/release | spec либо deep build, независимые роли и усиленный assurance |

`design`, `build`, `review` — отдельная ось. Repository review всегда превращается в
assurance campaign, а не в чтение нескольких случайных файлов.

## Проектная папка

```text
PROJECT.yaml
STATE.yaml
STACK.md
SYSTEM_MAP.yaml
HISTORY.jsonl
ARIA_TEAM.yaml
TRUST.yaml
ACCESS.yaml
BACKLOG.yaml
specs/
adr/
knowledge/
legacy-import/   # immutable archive, если был импорт
```

`PROJECT.yaml` связывает документы с одним внешним Git root. Пример:

```yaml
schema_version: 1
project_id: example
display_name: Example
framework_version: 1.5.5
state_profile: frontier
documents:
  state: STATE.yaml
  stack: STACK.md
  history: HISTORY.jsonl
  system_map: SYSTEM_MAP.yaml
  specs: specs
  adr: adr
  knowledge: knowledge
  team: ARIA_TEAM.yaml
  trust: TRUST.yaml
  access: ACCESS.yaml
  backlog: BACKLOG.yaml
governance:
  status_authority: BACKLOG.yaml
  require_active_run_for_writes: true
  decision_registry: docs/decisions/DECISIONS.md
context:
  default_budget_bytes: 131072
  state_budget_bytes: 32768
  state_projection_budget_bytes: 32768
  stack_manifests: [pyproject.toml]
  git_ignore_prefixes: [project-docs]
```

Machine paths находятся только в `%LOCALAPPDATA%\ARIA-Codex\projects.toml`, runtime — в
`%LOCALAPPDATA%\ARIA-Codex\projects\<project>`.

ARIA не создаёт и не требует общего каталога `workspaces`. `code_root` указывает прямо на
верхний уровень конкретного Git-репозитория независимо от его расположения. Папка с именем
`workspaces` допустима только как выбранный пользователем контейнер и не имеет специальной
семантики для ARIA.

Проект определяется не именем папки, а записью локального реестра: `project_id` указывает
на один docs root и один Git code root. Поэтому перенос на другой компьютер не требует
переписывать документы: пользователь получает папку проектных документов и Git checkout,
затем повторно выполняет `aria register` с новыми абсолютными путями. Регистрация всегда
начинается в `shadow`; Codex проверяет doctor, выполняет canary и активирует проект.

Для совершенно нового проекта пользователь просто просит Codex «подключить этот Git-проект
к ARIA». Codex анализирует код, создаёт осмысленные `PROJECT/STATE/STACK/SYSTEM_MAP/HISTORY`
и каталоги документов, регистрирует roots и проверяет shadow-run. Пустой универсальный
scaffold намеренно не создаётся: stack и system map должны отражать реальный проект, а не
формальный шаблон.

## SYSTEM_MAP

Карту строит и обновляет LLM по реальному коду. Это кэш, а не источник истины. Она содержит:

- представления по слоям, доменам, runtime surfaces и cross-cutting рискам;
- компоненты, responsibilities, dependencies, risks и test seams;
- shared primitives и правила инвалидации;
- critical flows с failure modes и assurance-сценариями;
- известные неизвестные и Git/working-tree fingerprint.

`aria map-status` показывает validity и freshness. Устаревшая карта не останавливает чтение
проекта, но Codex обязан перепроверить затронутую область. Только deep repository review может передать свежий map candidate; active closure опубликует его recoverably после проверки, что карта не потеряла уже известные компоненты, shared primitives, critical flows и dimensions.

## Assurance и реальные тесты

### Feature Contract и convergence

Quick остаётся прямым low-risk путём. Standard/deep design и build до работы создают
`outputs/feature-contract.json`. Контракт фиксирует измеримый outcome, material requirements,
acceptance oracles, закрытые неоднозначности, implementation plan и ациклический task graph.
Каждое требование обязано присутствовать в acceptance, plan и tasks; пустой формальный
scaffold не проходит. До изменения product code контракт обязательно замораживается скрытой
командой `_lock-feature-contract`; ARIA сверяет неизменный Git baseline и связывает SHA
контракта с отдельными start-anchor и lock-receipt, а затем с immutable phase run manifest.

Completed standard/deep build дополнительно создаёт `outputs/convergence.json`. Он связывает
каждое требование с завершёнными contract tasks и реальными путями из Git delta. Каждый
acceptance criterion отдельно повторяет точный oracle контракта и ссылается на собственный
proof excerpt из проверенного `class_evidence` raw output. Пропущенный или `unproven`
acceptance/requirement блокирует завершение. Утверждённая deep spec связывается с точным SHA
Feature Contract, а portable trace сохраняет сам контракт и итог convergence.

Граница доверия: `manifest.json`, `contract-anchor.json` и
`feature-contract-lock.json` — control artifacts движка. Агент пишет только объявленные
файлы в `outputs/` и не редактирует control artifacts вручную. Cross-checks обнаруживают
повреждение и одностороннюю подмену внутри этой модели, но не являются криптографической
защитой от процесса с правом одновременно переписать весь runtime. Для hostile multi-tenant
execution runtime нужен вне writable boundary агента: OS ACL, внешний append-only ledger
или подпись ключом, недоступным worker-процессу.

ARIA планирует проверки по рискам:

- локальная логика — focused;
- несколько компонентов — integration;
- UI/API/worker/device/runtime — E2E;
- deep/cross-layer — E2E + adversarial;
- shared state/queues — concurrency + load + stress;
- отказ/долговечность — recovery + chaos;
- schema/data — migration + rollback;
- auth/tenancy — security isolation;
- release/repository — full regression и применимый soak.

Сложный тест обязан описывать initial state, связанную последовательность действий,
expected и forbidden outcome, side effects, correlation id, нагрузку/параллелизм и
фактический результат. Evidence принимается только с существующим raw-output файлом,
совпадающим SHA и excerpt, реально найденным в output. Один linked scenario может закрывать несколько test classes, но для каждого класса нужны собственные proof excerpts из этого output; нагрузочные классы также содержат JSON-метрики из raw output.

После тестов или review Codex сразу выводит пользователю не только статус, но и problem
report. Он разделяет подтверждённые дефекты, риски/unknown, проблемы окружения/конфигурации
и не-баги. Для каждой проблемы указываются evidence, влияние, доказанный production root
cause либо следующий шаг его установления, лучшее подходящее проекту промышленное решение
причины и проверки приёмки. ARIA не считает исправлением подавление симптома, изменение
fixture или локальный workaround; при этом не требует избыточной платформы там, где
достаточно небольшого, но полного продуктового изменения.

Для каждого runtime-компонента component/repository review и standard/deep build выполняют
минимум один Mission Supertest. Он фокусируется на конкретном блоке, но проходит через
реальные соседние ноды и слои, объединяет совместимые возможности и проверяет продуктовую
миссию под применимыми adversarial, concurrency/load/stress и recovery/chaos воздействиями.
Это вершина кампании, а не замена focused/integration тестов: взаимоисключающие режимы и
локальные negative cases проверяются отдельными векторами. Сценарий обязан дать oracle,
forbidden outcomes, correlation, load/fault profile, side-effect read-backs и честно назвать,
что осталось `UNPROVEN`. Детерминированный provider доказывает внутреннюю механику и
воспроизводимость, но не заменяет отдельный live-provider canary.

## Review и аудит

Review фиксирует root, список файлов и SHA до анализа. Closure требует:

- до тестов — свободную Markdown-карту функционального покрытия, привязанную к scope SHA;
- функции и ожидаемое поведение, inputs/outputs/states, реальные UI/API/backend/runtime
  bindings, ошибки/границы/композицию и выведенные из них test obligations;
- независимый `functional_coverage_reviewer`, который ищет пропуски и машинно связывает
  свой verdict с SHA карты, scope, точными test-evidence metadata и raw outputs;
- `reviewed` для каждого captured source-файла и точный excluded inventory для binary/runtime/sensitive boundaries;
- coverage каждого измерения assurance plan;
- структурированные findings;
- обязательные runtime checks;
- независимые role artifacts;
- неизменность scope до завершения.

Поэтому `findings: []` означает не «ничего не посмотрели», а подтверждённое отсутствие
находок в полностью учтённой области.

Верхним методологическим слоем выступает аудит: multi-axis decomposition,
static/runtime/security lanes, blast radius, positive/negative controls, сложные сценарии,
neighbor retest и regression.

## Shadow и active

- `shadow` читает project docs и пишет только local runtime;
- `_project-canary` проверяет active closure на изолированной копии;
- `_project-activate` атомарно связывает проект с текущим operational engine SHA;
- после изменения operational engine та же canary-транзакция безопасно перепривязывает
  stale active-проект и сохраняет событие `aria_engine_rebind`;
- `active` recoverably обновляет STATE/HISTORY, approved design artifacts и SYSTEM_MAP.

Engine SHA включает исполняемый пакет и Codex skills/AGENTS, но не README, пользовательские
документы, тестовые логи и кэши. Редактирование справки не блокирует активные проекты.

## Естественные команды проекта в Codex и Claude Code

Wheel содержит skill `aria-project`. После `aria codex install` Codex может переводить фразы
вроде «покажи мои задачи», «добавь идею» или «беру BLG-...» в authenticated collaborative
команды без ручного ввода actor id и без прямого редактирования control-документов.

Проверка установки:

```powershell
aria codex status
```

Повторный запуск идемпотентен. Штатное удаление сохраняет Git-проекты, общий control plane,
локальный runtime и публичный provider profile:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "$InstallRoot\uninstall.ps1"
```

Подробный контракт и release boundary: [ARIA in Codex](docs/CODEX_INTEGRATION.md).

Для Claude Code wheel содержит отдельный skill и защитный `PreToolUse` hook. Установка:
`aria claude install`; проверка: `aria claude status`; удаление: `aria claude remove`.
Интеграция не использует MCP-сервер. Подробности и границы защиты:
[ARIA in Claude Code](docs/CLAUDE_CODE_INTEGRATION.md).

## Проверка framework

```powershell
$env:PYTHONUTF8 = '1'
py -3.12 -B -m aria doctor
py -3.12 -B -m unittest discover -s tests -v
py -3.12 -B -m aria doctor --project my-project
py -3.12 -B -m aria doctor --project solar-autopilot
```

Подробности: [архитектура](docs/ARCHITECTURE.md), [руководство](docs/GUIDE.md),
[границы качества](docs/POLICY.md), [модель памяти](docs/MEMORY.md).
