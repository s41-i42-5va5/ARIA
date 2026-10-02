# ARIA 1.5 — release acceptance

Основной запуск текущего кандидата:

```powershell
aria release-check --output <new-empty-path>
```

Итог считается чистым только если `release-acceptance.json.ok == true` и каждый элемент
`checks` имеет `ok: true`. Проверяются source framework doctor, full regression, clean venv,
wheel bundle, offline install, `pip check`, installed framework doctor, disposable Git project,
детерминированный inventory bootstrap, project doctor и публичный `feature` start. Исходный
кандидат обязан быть чистым Git checkout с существующим commit; его branch, HEAD и отсутствие
изменений фиксируются в `source_git`. Wheel собирается дважды с одинаковым
`SOURCE_DATE_EPOCH`, равным времени candidate commit; имена и SHA-256 обеих сборок обязаны
полностью совпасть. Принятый SHA-256 сохраняется в `wheel_sha256`. Raw output каждого шага сохраняется
отдельным log-файлом вместе с SHA-256, размером и фактическим результатом.

В 1.5 installed-wheel smoke дополнительно создаёт две device identity, bootstrap подписанной
access policy, scoped grant второго пользователя, signed backlog read-back/audit, проверяет
одновременный claim одного backlog item двумя actors на разных devices, ровно одного победителя,
evidence-bound completion, version/branch fail-closed, team status/claim/release, active cutover
с read-back, генерирует GitHub adapter и проходит полный generic CI protocol:
clean commit → signed `ci prepare` → isolated `ci execute` → separate `ci attest` →
`evidence verify` с actor-bound
`TRUST.yaml` → `ci import`. Любой skipped 1.5 check делает отчёт отрицательным.

В 1.2 автоматический report дополнительно проверяет installed distribution
version/provenance, настоящий console entry point, status/history/map, verified
quick/standard/deep-design/deep-build route policies, isolated canary, Spec Kit import,
Feature Contract lock и lifecycle read-back. Отчёт также фиксирует изолированные
`PIP_CACHE_DIR`, `TEMP`, `TMP` и отдельный smoke root.
Verification fixture реально проверяет health contract, идемпотентную durable запись,
read-back отдельным процессом, наличие side effect, отказ для traversal identifier и повреждённого
state. Простая печать названий assurance-классов доказательством не считается. Closure/convergence,
tamper, crash и concurrency входят в обязательный полный regression suite; их падение делает
общий report отрицательным. Отдельный performance check проверяет, что engine identity не посещает
большое исключённое дерево `.venv` и не превышает установленный regression limit.

Ручная независимая review-роль остаётся обязательной для изменения самого framework: она
проверяет код, тесты, документацию, compatibility boundary 1.3–1.5 и честность acceptance report.
Synthetic role fixture внутри автоматического smoke проверяет только schema/gate plumbing и не
считается этой независимой семантической review.

Релиз считается принятым только одним непрерывным циклом на текущем candidate.

## Обязательная матрица

1. Создать новый venv в новом каталоге; старый venv повторно не использовать.
2. Перенаправить `PIP_CACHE_DIR`, `TEMP` и `TMP` в writable scratch-каталог.
3. Выполнить обычный `pip install .` с build isolation и зависимостями.
4. Подтвердить `pip check`, версию, импорт из `site-packages` и console entry point.
5. Повторно собрать wheel с `SOURCE_DATE_EPOCH` candidate commit и подтвердить побайтовое
   совпадение имени и SHA-256 обеих сборок.
6. Проверить публичный `--framework-root` и framework doctor.
7. Создать disposable Git-проект с раздельными framework/docs/code/runtime roots.
8. Через установленный `aria.exe` выполнить register, project doctor, status, history и map-status.
9. Запустить quick build, standard build, deep design и deep build; проверить manifest policies.
10. Для standard build пройти Feature Contract lock, реальную Git-дельту, `aria verify`,
   Evidence Bundle, все required assurance classes, independent role, convergence и shadow closure.
11. Выполнить isolated project canary и подтвердить `source_unchanged: true`.
12. Запустить полный `unittest discover`; migration, tamper, crash и concurrency tests не исключать.
13. Через установленный wheel проверить Ed25519 key generation, task lease, GitHub adapter,
    offline package verification и полный CI prepare/execute/attest/import.
14. Установить immutable wheel 1.3.0 из `releases/1.3.0`, создать им настоящий проект,
    выполнить upgrade 1.3 → 1.4 → 1.5 установленным wheel, bootstrap access и затем
    project doctor + isolated canary. Для 1.4 → 1.5 проверить journal recovery после
    каждого частичного окна записи и повторную идемпотентную миграцию.
15. Проверить device enrollment, tamper/revocation/version scopes, competing backlog claim,
    dependency-blocked claim, automatic run/finding/failure reconciliation и completion
    только по verified completed run либо точному текущему Git HEAD.
16. Выполнить атомарный shadow → active cutover, проверить изменение STATE/HISTORY и повторный
    project doctor в active mode.
17. Повторить framework doctor после всех правок и получить реальный независимый release-review.

## Чистая wheel-проверка

```powershell
py -3.12 -m venv C:\scratch\aria-acceptance-venv
$env:PIP_CACHE_DIR = 'C:\scratch\pip-cache'
$env:TEMP = 'C:\scratch\tmp'
$env:TMP = $env:TEMP
C:\scratch\aria-acceptance-venv\Scripts\python.exe -m pip install C:\path\to\aria-codex
C:\scratch\aria-acceptance-venv\Scripts\python.exe -m pip check
C:\scratch\aria-acceptance-venv\Scripts\aria.exe --help
C:\scratch\aria-acceptance-venv\Scripts\aria.exe `
  --framework-root C:\path\to\aria-codex doctor
```

`--framework-root` является глобальной опцией и располагается до `COMMAND`.

## Fail-closed условия

Acceptance останавливается при любом из условий:

- wheel использует source tree вместо установленного `site-packages`;
- две сборки wheel имеют разные имена или SHA-256;
- installed modules и operational framework root расходятся;
- doctor, project doctor, history или system map не проходят;
- Feature Contract создан после product/runtime output;
- acceptance/convergence evidence неполны;
- trusted receipt отсутствует, подменён или относится к другому Git/contract state;
- package signature, actor/key binding, trust policy, job nonce, commit или contract SHA расходятся;
- CI execution меняет исходный checkout либо integration evidence относится не к итоговому commit;
- canary меняет исходный проект;
- полный regression содержит failure/error;
- независимый reviewer оставил незакрытый P1/P2.
