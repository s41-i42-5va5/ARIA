# ARIA 1.5 Feature Contract

## Outcome

ARIA 1.5 превращает project-local командный контур 1.4 в проверяемую local-first
систему совместной работы: каждый человек имеет стабильный actor id и отдельные
криптографические устройства, доступ ограничивается подписанной политикой проекта и
версии, а единый backlog автоматически собирает пользовательские задачи и доказанные
инженерные хвосты.

## Requirements

### R-1501 — Bounded engine identity

Расчёт identity ядра обходит только operational framework roots и заранее отсекает
`.venv`, VCS, caches, build trees и dependency trees. Большое локальное окружение не
может линейно замедлять `doctor`, activation или тесты.

### R-1502 — Device identity

`aria identity enroll` создаёт Ed25519 identity на устройстве, не перезаписывает
существующий ключ и возвращает переносимый enrollment request только с публичными
данными. Закрытый ключ хранится только в machine runtime: DPAPI current-user на Windows,
permission-restricted local storage на остальных поддерживаемых ОС.

### R-1503 — Actor/device binding

Один human actor может иметь несколько устройств. Каждое устройство имеет уникальный
device id, hostname metadata, key id, статус и даты действия. Actor id, device id и key id
проверяются fail-closed. Имя компьютера, OS user и email не являются proof of identity.

### R-1504 — Signed access policy

Проект получает `ACCESS.yaml`. Политика связывает actors/roles с точными permissions и
version/branch scopes, имеет monotonic revision и подпись доверенного maintainer key.
Неизвестное permission, actor, key, scope, подпись, downgrade или policy drift блокируют
защищённое действие. Bootstrap разрешён ровно один раз для пустой политики проекта.

### R-1505 — Automatic backlog

Проект получает канонический backlog с типами `feature`, `bug`, `debt`, `risk`,
`clarification`, `coverage-gap`, `review-finding`, `verification-failure` и `blocker`.
Элементы создаются из явного пользовательского запроса и из доказанных lifecycle/evidence
событий. Source identity обеспечивает идемпотентность и не позволяет размножать один факт.

### R-1506 — Ownership and version targeting

Backlog item содержит creator, optional assignee, status, priority, target versions,
requirements, run/commit/evidence refs, dependencies и acceptance summary. Назначение,
claim, block и completion проверяют identity, permissions, допустимые переходы и optimistic
revision. Completion без closure/evidence reference для инженерного элемента запрещён.

### R-1507 — Signed, recoverable audit

Каждое изменение access/backlog создаёт подписанное событие с actor/device/key identity,
timestamp, previous state revision и content SHA. Запись выполняется через lock, atomic
replace и read-back. Повтор команды идемпотентен; stale revision, competing claim,
tamper и частичная запись блокируются или восстанавливаются без silent loss.

### R-1508 — Init and migration

Новый проект 1.5 получает team v2, trust, access и backlog documents. `upgrade-1-5`
безопасно и идемпотентно переносит закрытый 1.4 project, сохраняет actor ids/roles,
не выдумывает emails/keys и оставляет явный bootstrap action. Открытые legacy runs и
невалидные team/trust documents блокируют migration.

### R-1509 — Command surface

Публичный CLI включает:

- `aria identity enroll|whoami`;
- `aria access bootstrap|status|grant|revoke`;
- `aria backlog add|list|show|assign|claim|block|done|audit`;
- identity-aware `aria team claim|release`.

Результаты остаются machine-readable JSON, ошибки возвращают ненулевой exit code.

### R-1510 — Compatibility boundary

ARIA 1.5 остаётся без центрального сервера. Синхронизация между машинами выполняется через
Git/shared repository, а concurrent Git conflicts выявляются, но real-time distributed
lease без coordinator не заявляется. Claude/GPT email, generic cloud folder и hostname не
считаются authentication provider.

### R-1511 — Security and privacy

Private keys, OS identifiers и secrets не попадают в project docs, Git, backlog или
persisted command output. Public keys допустимы в `TRUST.yaml`. Процесс с полным правом
переписывать runtime/docs того же OS user остаётся вне local trust boundary; документация
требует OS ACL, protected Git branch либо external coordinator для hostile users.

### R-1512 — Release evidence

Release требует focused, integration, adversarial, concurrency, recovery, migration,
tamper, performance-regression и full-regression checks, установленный wheel, provenance
doctor и Mission Supertest: два actors на разных devices одновременно работают с backlog,
сталкиваются на одном claim, проходят version access, evidence-bound completion и audit
read-back без duplicate ownership или потери события.

## Non-goals

- Центральный ARIA Coordinator и real-time SaaS.
- OAuth/SSO вход через ChatGPT, Claude.ai или другой consumer account.
- Предотвращение прямого изменения файлов пользователем с полным OS write access.
- Совместное live-редактирование одного run.

## Acceptance

1. Framework doctor и полный regression завершаются без failures/skips.
2. Engine identity не посещает исключённую `.venv` даже при большом дереве.
3. Private key отсутствует во всех project artifacts и CLI JSON.
4. Неверная подпись, revoked device, stale revision и запрещённая версия fail closed.
5. Automatic sources дедуплицируются и сохраняют точную provenance.
6. Competing claim даёт ровно одного owner.
7. Crash windows восстанавливают согласованный backlog/access/audit state.
8. Upgrade 1.4→1.5 идемпотентен и не создаёт ложную identity.
9. Clean wheel install, console entry point, framework binding и release acceptance проходят.
