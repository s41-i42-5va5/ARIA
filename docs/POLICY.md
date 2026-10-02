# Политика границ и качества

## Identity, access и backlog 1.5

- Email, hostname и OS user являются метаданными, а не authentication.
- Приватный device key не записывается в project docs, Git, CLI output или enrollment request.
- На Windows ключ защищается DPAPI current-user; процесс того же OS user с полным правом
  записи остаётся вне локальной trust boundary.
- Любое защищённое действие требует активного device key, разрешённого permission и
  совпадения version/branch scope.
- Branch-контекст backlog-команд читается из зарегистрированного Git checkout; явное
  значение принимается только при точном совпадении с фактической текущей веткой.
- `ACCESS.yaml`, `ACCESS_HISTORY.jsonl` и `BACKLOG.yaml` проверяются fail-closed:
  tamper, stale revision, revoked device и разрыв hash/signature chain блокируют действие.
- При наличии `PROJECT.yaml.governance` build разрешён только из clean baseline и только
  для claimed backlog item, связанного с immutable run. Dirty checkout без active build-run,
  несколько одновременно активных build-run или terminal run с несогласованным backlog
  переводят проект в `RECOVERY_REQUIRED`.
- Git branch protection или внешний coordinator нужны против hostile участников,
  способных напрямую переписывать общие файлы.

## Team & Trusted CI 1.4

- Приватные signing keys находятся вне project docs, Git и Evidence Package.
- Доверенный key обязан быть связан с одним actor id; actor substitution блокируется.
- `local < signed < ci-signed`; повышение уровня без нового подписанного исполнения запрещено.
- CI job ограничен expiry и связан с nonce, commit и immutable contract SHA.
- Параллельные участники работают в отдельных runs; task lease использует OS lock и
  optimistic revision.
- После merge нужен новый integration CI run на итоговом commit и точном наборе source
  package SHA. PASS отдельных веток не переносится автоматически.
- Reviewer не может быть contributor либо автором проверяемого source evidence.

## Trusted execution 1.3

- При настроенном `VERIFY.yaml` только receipt от `aria verify` считается executed evidence.
- Команды задаются массивом argv и исполняются без shell через adapter allowlist.
- PASS действует только для зафиксированных contract SHA и точного Git HEAD/worktree.
- FAIL, timeout, неполное покрытие classes, contract/output tamper и Git drift блокируют closure.
- Секретные значения маскируются в persisted output и receipt; передавать секреты в argv
  всё равно запрещено операционной практикой, поскольку список процессов находится вне
  trust boundary ARIA.
- Evidence Bundle доказывает факт запуска и целостность результата, но не заменяет качество
  тестового oracle, независимый review и внешнюю аттестацию hostile worker/CI runner.
- `contract-anchor.json` и каталог `execution/` являются control artifacts движка, а не
  writable surface агента. Процесс с правом изменять весь runtime того же OS user находится
  вне локальной trust boundary; для него необходимы отдельные ACL или внешняя подпись CI.

## Источники истины

- Framework: только текущий `aria-codex`.
- Код продукта: только зарегистрированный Git root.
- Актуальная рабочая точка: project `STATE.yaml`.
- Полная процессная история: project `HISTORY.jsonl` + архив specs/research/ADR.
- Machine paths и mode: `%LOCALAPPDATA%\ARIA-Codex\projects.toml`.
- Семантическая архитектура: `SYSTEM_MAP.yaml` как LLM-кэш, всегда перепроверяемый по коду.
- Raw execution data: только локальный project runtime.

ARIA не хранит Git diff или полный Git log в STATE/HISTORY. Она сохраняет минимальную трассировку: base/head, commit SHA/subject/`ARIA-*` trailers и path/SHA изменённых файлов. Содержимое коммитов остаётся только в Git.

`STATE.yaml` является единственной живой project map. `ROADMAP.md` в активной папке проекта запрещён. Профиль `frontier` допускает неполный постепенно растущий план; профиль `roadmap` требует валидные этапы, уникальные task id, зависимости без циклов и согласованный current/focus.

## Documents

- Quick не создаёт spec.
- Standard использует spec только если она уже релевантна или задача объективно требует долговременного контракта.
- Deep/design создаёт компактную spec для длинной реализации; deep/build требует утверждённую spec.
- Deep/design сначала отдаёт `proposed` с независимым attacker-review и останавливается. Только явный пользовательский approval точных proposal/spec SHA разрешает публикацию; изменение кандидата аннулирует старое утверждение.
- Внешние sources сохраняются только если их claims влияют на долговременную spec/ADR/реализацию; обычные ссылки не материализуются.
- RESEARCH синтезирует material references, ADR фиксирует долговременное решение, SPEC задаёт контракт. Research никогда не становится ADR автоматически.
- Imported legacy-копии immutable.
- Новый документ создаётся только если он будет рабочим источником истины, а не доказательством выполнения ритуала.

## Shadow и active

Shadow разрешает чтение project docs и запись только в local runtime. Active разрешает скрытому closure изменять STATE/HISTORY и публиковать проверенные deep/design spec/research/ADR через lock, snapshot, atomic replace каждого файла, read-back и crash-recovery. Общего CLI writer для docs нет.

Пустой STATE frontier в shadow mode не означает отсутствие runtime run. И наоборот,
непустой или изменённый product checkout без started bound run не считается управляемой
работой. Процесс того же OS user всё ещё может обойти CLI на уровне файловой системы;
policy требует остановки, а doctor/preflight обнаруживают такой drift при следующем входе.

Каждый проект активируется отдельно только после live-canary на изолированной копии. Active registry entry обязан содержать точный engine SHA; drift блокирует workflow. Старые W2/W3 и imported legacy copies не читаются как live state и никогда не изменяются новым closure.

## Quality

Минимальное обязательное доказательство: результат/изменение, фактический output, read-back, review итогового diff/scope и closure. Score, milestone, ручные acknowledgments и закрытие микростадий не относятся к quality signal нового пути.

Завершённый deep/build обязан начинаться и заканчиваться с clean worktree, иметь закоммиченную полную дельту и trace trailers. Quick/standard не получают искусственного commit-gate, но существующий commit range сохраняется автоматически.

Поведенческая память принимает только подтверждённый lesson с trigger/finding/countermeasure/evidence. Она не оценивает LLM баллами и не записывает milestone каждого run.

Review scope неизменяем после старта и не может быть пустым. Coverage обязан учесть каждый файл и каждое измерение assurance plan. Каждый finding имеет severity, scoped file, положительный номер строки, конкретное evidence и confidence от 0 до 1. Runtime-sensitive finding проверяется подходящим тестом; repository review требует assurance campaign.

Component/repository review до тестов обязан создать свободную LLM Functional Coverage Map, привязанную к scope SHA. Она покрывает material functions, inputs/outputs/states, реальные cross-layer bindings, failure/boundary/composition behavior и test obligations. При отсутствии spec ожидания выводятся из кода с явным разделением observed/inferred/unknown. Python не моделирует предметную область и не требует полной комбинаторной матрицы; независимый `functional_coverage_reviewer` отвечает за поиск смысловых пропусков и соответствие тестов карте. Его verdict связывается с SHA карты, scope, точным verification digest и raw outputs.

Complex evidence содержит связный scenario contract и фактический raw output. `not_applicable` допустим только для assessment class с конкретной причиной и не заменяет обязательный executed class.

Standard/deep design и build требуют Feature Contract со статусом `ready`. Он содержит
измеримый outcome, material requirements, acceptance oracles, явное подтверждение разбора
неоднозначностей, implementation plan и ациклический task graph. Каждое требование обязано
покрываться согласованными acceptance, plan и tasks. Контракт блокируется до product delta:
lock отклоняется, если Git baseline уже изменён, а последующая смена SHA блокирует closure.
Для completed standard/deep build convergence связывает requirement с completed task/path и
доказывает каждый acceptance criterion отдельно: exact oracle плюс уникальный проверенный
proof excerpt из class_evidence raw output. Пропуск или `unproven` блокирует closure.

Control artifacts run (`manifest.json`, start anchor, lock receipt) принадлежат engine и не
редактируются агентом. Hash/SHA cross-check рассчитан на эту trust boundary; процесс с правом
совместно переписать весь runtime считается вне модели. Для недоверенного worker runtime
выносится за его writable boundary и защищается ACL, внешним ledger или подписью.

Каждый runtime-компонент в component/repository review и standard/deep build получает минимум один Mission Supertest: осмысленную продуктовую миссию с фокусом на SUT, реальными соседними компонентами/нодами и слоями, максимально совместимыми возможностями, failure hypotheses, oracle/invariants, применимыми adversarial/concurrency/load/stress/recovery воздействиями и фактическим side-effect read-back. Он не заменяет разные focused/integration векторы и не обязан искусственно совмещать взаимоисключающие режимы. Нагрузка выводится из SLO/ёмкости/риска. Mock или deterministic provider может доказать внутреннюю механику, но не live external readiness; недоступная часть остаётся `BLOCKED/UNPROVEN`. Семантическую осмысленность проверяет независимый LLM reviewer, без domain-specific Python-матрицы.

После любого review, аудита или тестирования Codex немедленно выдаёт пользователю post-assurance problem report. PASS/FAIL, closure и ссылки на evidence его не заменяют. Отчёт разделяет подтверждённые дефекты, риски/unknown, environment/configuration blockers и не-баги; для каждой проблемы содержит evidence, impact, доказанный production root cause либо явную незакрытую диагностику, промышленный продуктовый root fix и проверки приёмки. Workaround, подавление симптома, подгонка fixture или локальное исключение не считаются root fix. Решение учитывает архитектуру, безопасность, целостность данных, compatibility/migration, observability, operations и масштаб, но остаётся минимально достаточным без избыточного формализма. Review-only не пишет код без разрешения; build устраняет причину и повторяет затронутые и соседние проверки.

Обязательные независимые роли задаются immutable manifest. Role agents read-only и не выполняют code/docs/Git writes; closure требует их artifact SHA, context identity, verdict и разрешение всех findings. Самоотчёт дирижёра не считается независимым review.
