# ARIA 1.4 — Team & Trusted CI

## Граница доверия

ARIA различает три уровня evidence:

- `local` — Evidence Bundle v1 из локального runtime ARIA 1.3;
- `signed` — переносимый пакет, подписанный разрешённым Ed25519-ключом;
- `ci-signed` — пакет результата изолированного ARIA CI job.

Подпись доказывает целостность и владение ключом. `TRUST.yaml` дополнительно связывает ключ
с actor id, задаёт allowlist для каждой policy, срок действия и revocation. Приватные ключи
не входят в project docs, package или Git.

```yaml
schema_version: 1
keys:
  - id: ed25519:0123456789abcdef0123456789abcdef
    public_key: BASE64_RAW_ED25519_PUBLIC_KEY
    actor_id: github-actions
    status: trusted
    not_before: 2026-07-28T00:00:00Z
    expires_at: 2027-07-28T00:00:00Z
policies:
  contributor:
    minimum_trust_level: signed
    trusted_keys:
      - ed25519:0123456789abcdef0123456789abcdef
    allowed_actor_roles: [contributor, maintainer]
  job:
    minimum_trust_level: signed
    trusted_keys:
      - ed25519:0123456789abcdef0123456789abcdef
  ci:
    minimum_trust_level: ci-signed
    trusted_keys:
      - ed25519:0123456789abcdef0123456789abcdef
  release:
    minimum_trust_level: ci-signed
    trusted_keys:
      - ed25519:0123456789abcdef0123456789abcdef
    required_assurance_classes: [focused, integration]
    allowed_actor_roles: [ci, release-manager]
    required_approvals: 1
```

`public_key` — base64 от raw 32-byte Ed25519 public key. `key id` вычисляется как
`ed25519:` плюс первые 32 hex-символа SHA-256 public key. Неизвестный, запрещённый,
revoked, ещё не действующий, просроченный или принадлежащий другому actor ключ блокирует
policy gate.

`job` policy исполняется вне project runtime и использует точный key allowlist как
полномочие authorizer. Для неё нельзя задавать `allowed_actor_roles` или
`required_approvals`: контекст команды/ревью недоступен executor и такая policy корректно
закроется fail-closed. Роли и approvals применяются к contributor, integration, release и
review policies, где они проверяются по `ARIA_TEAM.yaml`.

## Evidence Package v2

ZIP-пакет содержит:

- `package.json` — тип, subject, trust level и полный inventory;
- `signature.json` — Ed25519 signature точных bytes `package.json`;
- `evidence/**` или `ci/**` — manifest, contracts, receipts и raw logs.

Verifier не распаковывает архив на диск, запрещает absolute/traversal/duplicate paths,
лишние неописанные файлы и превышение лимитов. Для каждого файла сверяются размер и
SHA-256. `inspect` только читает метаданные и явно возвращает `signature_checked: false`.

## CI protocol

1. `prepare` требует clean checkout и совпадение HEAD с baseline run.
2. Job фиксирует случайные job id и nonce, expiry, commit, Feature/Execution Contract SHA,
   полный набор required assurance classes и подписывается разрешённым authorizer.
3. `execute` валидирует подпись job, создаёт detached Git worktree точного commit и использует
   защищённый execution primitive ARIA 1.3.
4. Любая запись команды в tracked/untracked Git surface делает execution failed. Временный
   worktree удаляется, исходный checkout сверяется до/после.
5. `execute` выпускает unsigned receipt archive и не получает постоянный приватный ключ.
6. Отдельный `attest`, запущенный в защищённой CI environment вне тестируемого checkout,
   сверяет job, receipts, outputs и coverage, после чего подписывает Result package как
   `ci-signed`.
7. `import` повторяет cryptographic/policy/binding checks и запрещает повторный job id.

Job expiry ограничивает окно исполнения и импорта. Job JSON и result package сохраняются
как неизменяемые артефакты; их редактирование нарушает SHA/nonce binding.

Reference workflow запускается только через `workflow_dispatch` для доверенного commit.
`ci-signed` означает, что разрешённый CI-attester проверил структурированные receipts и
подписал result package; это не platform-rooted доказательство честности hostile worker.
Worktree и process-tree isolation защищают исходный checkout и обычные дочерние процессы,
но не являются песочницей для намеренно вредоносного кода с правами текущего OS user. Для
untrusted fork/hostile workload требуется отдельный одноразовый runner/VM и внешняя
платформенная аттестация.

## Параллельная работа и integration

`ARIA_TEAM.yaml` хранит actors типов `human` и `service` с ролями `contributor`, `reviewer`,
`maintainer`, `release-manager`, `ci`. Runtime team state имеет монотонную revision.
`team claim` выполняется под OS lock и принимает `expected-revision`: при гонке выигрывает
ровно один участник. Lease ограничен TTL и освобождается только actor с точным token.

Каждый участник работает в отдельной ветке/run. После merge создаётся новый CI job с
purpose `integration`, итоговым commit и SHA всех source packages. Integration gate требует:

- PASS и доверенную policy для каждого уникального source package;
- PASS `ci-signed` integration package на точном target commit;
- точное равенство набора source package SHA;
- actors с разрешёнными ролями;
- отдельный подписанный review package, точно связанный с target commit, source hashes и
  integration package SHA; reviewer отличается от всех авторов source и integration evidence;
- каждый source commit является предком target commit.

PASS отдельных веток не переносится через merge и не заменяет интеграционную проверку.

## Миграция 1.3

```powershell
aria upgrade-1-4 --project example
```

Миграция блокируется при открытых runs 1.3, добавляет `ARIA_TEAM.yaml` и fail-closed
`TRUST.yaml`, затем последним atomic write обновляет `PROJECT.yaml` до `framework_version:
1.4.0`. Повторный запуск идемпотентен. После миграции замените placeholder actors, добавьте
публичные ключи и выполните doctor/canary.
