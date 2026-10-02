# ARIA 1.4 Feature Contract — Team & Trusted CI

Status: approved for implementation  
Target: `1.4.0`

## Outcome

Несколько участников могут параллельно выполнять отдельные ARIA runs, передавать
проверяемые результаты через CI и получать один fail-closed integration/release verdict.

## Requirements

- **R-1401 Portable evidence.** ARIA экспортирует self-contained Evidence Package v2 и
  проверяет его offline без исходного runtime.
- **R-1402 Cryptographic identity.** Receipt и package manifest подписываются Ed25519;
  verification проверяет key id, trust policy, signature и revocation.
- **R-1403 Isolated execution.** CI execution работает с точным commit в отдельном checkout
  и не использует незакоммиченные данные неявно.
- **R-1404 CI protocol.** `prepare`, `execute` и `import` связываются одноразовым job id,
  nonce, source commit, Feature/Execution Contract SHA и сроком действия.
- **R-1405 Actors and roles.** Human/service actors имеют стабильные ids и роли;
  автор изменения не закрывает обязательное independent review.
- **R-1406 Parallel coordination.** Task leases и optimistic project revision предотвращают
  потерю обновлений и двойное владение задачей.
- **R-1407 Integration gate.** Несколько source runs объединяются только через новый
  integration run на итоговом commit; stale evidence не переносится через merge.
- **R-1408 Trust policies.** Policies задают minimum trust level, trusted/revoked keys,
  required assurance classes, approvals и допустимые actor roles.
- **R-1409 GitHub reference adapter.** ARIA генерирует проверяемый GitHub Actions workflow,
  но protocol и evidence format не зависят от GitHub.
- **R-1410 Compatibility.** Локальный Evidence Bundle v1 ARIA 1.3 остаётся читаемым как
  trust level `local`; повышение до signed/CI без нового исполнения запрещено.

## Acceptance

- **AC-1401:** экспортированный package проходит offline verification после удаления runtime.
- **AC-1402:** изменение любого байта manifest/receipt/log/signature блокирует verification.
- **AC-1403:** unknown, expired или revoked key блокирует policy gate.
- **AC-1404:** job result для другого commit/contract/nonce не импортируется.
- **AC-1405:** isolated execution не изменяет исходный checkout.
- **AC-1406:** два конкурирующих lease одной task дают ровно одного владельца.
- **AC-1407:** stale project revision отклоняется без потери принятого события.
- **AC-1408:** PASS отдельных runs не заменяет PASS integration run итогового commit.
- **AC-1409:** contributor не может подписать собственный independent review.
- **AC-1410:** generated GitHub workflow выполняет generic CI protocol.
- **AC-1411:** ARIA 1.3 bundle читается, но не удовлетворяет `ci-signed` policy.
- **AC-1412:** wheel 1.4 проходит clean installed-CLI release acceptance без skipped checks.

## Non-goals

- SaaS backend, web UI и собственный fleet/queue runners.
- Live совместное редактирование одного run.
- Hostile-workload sandbox и platform-rooted attestation для недоверенного fork-кода.
- Одновременная поддержка GitLab, Jenkins, Azure и Kubernetes adapters.
- Автоматический merge без явной policy и integration evidence.
