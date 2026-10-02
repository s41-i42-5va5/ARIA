# ARIA 1.5.2 — полная инструкция установки и ручной проверки

## 1. Что именно поддерживается

ARIA 1.5.2 использует четыре физически раздельные границы:

```text
C:\Tools\aria-codex-1.5.2       framework root; отдельная локальная копия на каждом ПК
C:\Projects\example             code_root; Git top-level продукта на данном ПК
G:\My Drive\ARIA\example-aria   docs_root; общие документы проекта
%LOCALAPPDATA%\ARIA-Codex       registry, private device keys, locks, runs и evidence данного ПК
```

Папка с именем `workspaces` не нужна. Общая родительская папка может называться как угодно.
ARIA регистрирует только конкретные `code_root` и `docs_root`.

Для двух компьютеров поддерживается:

- отдельная device identity на каждом ПК;
- один проект и один общий набор project docs;
- отдельные абсолютные пути в локальном registry каждого ПК;
- отдельные Git-ветки и runs;
- подписанный доступ, backlog, audit и evidence;
- последовательная передача управления между ПК после полной синхронизации.

Не поддерживается как доказанная гарантия:

- одновременная запись в `ACCESS.yaml`, `BACKLOG.yaml`, `STATE.yaml` или `HISTORY.jsonl`
  с двух ПК через Google Drive;
- один run, начатый на одном ПК и продолженный из локального runtime другого ПК;
- общий межкомпьютерный `team claim`: team leases хранятся в локальном runtime;
- exactly-one backlog claim между двумя несинхронными копиями Google Drive.

Google Drive синхронизирует файлы, но не предоставляет ARIA общий process-lock. Если содержимое
одного файла разошлось, Drive может сохранить обе версии. Поэтому Google Drive-сценарий ниже
использует строго одного писателя в каждый момент времени.

## 2. Рекомендуемая архитектура для двух компьютеров

```text
Google Drive
└── ARIA
    ├── example-aria\       # общий docs_root
    └── exchange\           # только публичные enrollment requests и evidence packages

Компьютер 1
├── C:\Tools\aria-codex-1.5.2\
├── C:\ARIA\venv-1.5.2\
├── C:\Projects\example\    # локальный Git clone
└── %LOCALAPPDATA%\ARIA-Codex\

Компьютер 2
├── C:\Tools\aria-codex-1.5.2\
├── C:\ARIA\venv-1.5.2\
├── C:\Projects\example\    # второй локальный Git clone
└── %LOCALAPPDATA%\ARIA-Codex\
```

В Google Drive не помещать:

- `.venv`;
- `%LOCALAPPDATA%\ARIA-Codex`;
- private keys;
- временные CI results;
- живую `.git` двух одновременно работающих компьютеров.

Product code рекомендуется синхронизировать через Git remote. Google Drive используется для
`docs_root` и передачи публичных артефактов.

Если продукт всё же целиком расположен в Google Drive, используйте отдельные соседние папки:

```text
G:\My Drive\ARIA\example-code\
G:\My Drive\ARIA\example-aria\
```

В этом режиме запрещена одновременная работа. Перед каждым Git- или ARIA-изменением нужно
получить управление, дождаться полного sync и только затем запускать команду.

## 3. Настройка Google Drive

Для `My Drive` предпочтителен режим Mirror files: файлы постоянно существуют на локальном
диске. При Stream files папки `example-aria` и `exchange` нужно вручную сделать доступными
offline. Shared Drives поддерживают только streaming, поэтому обе папки обязательно сделать
доступными offline.

На обоих ПК:

1. Установить Google Drive for desktop.
2. Войти в один разрешённый аккаунт либо открыть одну общую папку.
3. Настроить одинаковую или понятную локальную точку доступа.
4. Сделать `example-aria` и `exchange` доступными offline.
5. Проверить в Drive for desktop отсутствие sync errors.
6. Не переключать Stream/Mirror, пока есть несинхронизированные изменения.

Официальные справки Google:

- <https://support.google.com/drive/answer/13401938>
- <https://support.google.com/drive/answer/10838124>
- <https://support.google.com/drive/answer/13470231>

## 4. Локальная установка ARIA на каждом ПК

На каждом компьютере нужна отдельная неизменяемая копия framework root и отдельный venv.
Wheel и framework root должны относиться к одной версии 1.5.2.

Из framework root выполните один раз:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Установщик:

1. проверяет `.aria-root` и версию framework;
2. проверяет SHA-256 всех файлов `releases\1.5.2`;
3. требует локальный Python 3.12;
4. создаёт рядом с framework root каталог `venv-1.5.2`;
5. устанавливает ARIA и зависимости только из локального wheelhouse, без сети;
6. выполняет `pip check`;
7. подтверждает, что `aria` импортируется из нового venv, а не из другой копии framework;
8. создаёт соседний `aria-runtime`;
9. выполняет framework `doctor`.

Ожидаемый итог:

- `ok: true`;
- version/package/runtime равны `1.5.2`;
- framework marker найден;
- engine identity рассчитана;
- `pip check` не сообщает broken requirements.

Повторный обычный запуск `setup.ps1` безопасно переустанавливает принятый wheel в ту же среду.
Если venv повреждён или создан другой версией Python, выполните:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Recreate
```

Для ежедневных команд:

```powershell
$Framework = (Resolve-Path .).Path
$InstallRoot = Split-Path -Parent $Framework
$Venv = "$InstallRoot\venv-1.5.2"
$env:ARIA_RUNTIME_ROOT = "$InstallRoot\aria-runtime"
$AriaExe = "$Venv\Scripts\aria.exe"

function aria152 {
    & $AriaExe --framework-root $Framework @args
}

aria152 doctor
```

На втором компьютере `setup.ps1` выполняется отдельно. Каталоги `venv-1.5.2` и
`aria-runtime` между компьютерами не копируются и через Google Drive не синхронизируются.

Если `doctor` показывает несовпадение module/package/framework, не продолжать. Нельзя
смешивать wheel 1.5.2 и framework root другой версии.

## 5. Подготовка product Git на компьютере 1

Рекомендуемый вариант с Git remote:

```powershell
$ProjectId = 'example'
$Code = 'C:\Projects\example'
$Docs = 'G:\My Drive\ARIA\example-aria'
$Exchange = 'G:\My Drive\ARIA\exchange'

git clone https://example.invalid/your/example.git $Code
git -C $Code switch -c feature/manual-152
git -C $Code status
```

Замените URL на настоящий. `git status` должен показывать Git top-level проекта.

Если новый репозиторий создаётся с нуля:

```powershell
New-Item -ItemType Directory -Force $Code
git -C $Code init -b main
# Создайте реальные исходники и manifest проекта.
git -C $Code add .
git -C $Code commit -m 'Initial product baseline'
git -C $Code switch -c feature/manual-152
```

ARIA требует существующий commit и принимает в `--code-root` только Git top-level.

## 6. Создание проекта ARIA на компьютере 1

`docs_root` должен отсутствовать. Запускать `init` только один раз:

```powershell
aria152 init `
  --project $ProjectId `
  --code-root $Code `
  --docs-root $Docs
```

Ожидается:

- `bootstrap_kind: deterministic_git_inventory`;
- `semantic_bootstrap: false`;
- `semantic_review_required: true`;
- создана регистрация в shadow mode;
- созданы `PROJECT.yaml`, `STATE.yaml`, `STACK.md`, `SYSTEM_MAP.yaml`,
  `HISTORY.jsonl`, `ARIA_TEAM.yaml`, `TRUST.yaml`, `ACCESS.yaml`, `BACKLOG.yaml`.

`init` не завершает анализ архитектуры. До первой feature нужно открыть реальные исходники,
проверить `STACK.md` и `SYSTEM_MAP.yaml`, заменить generic/unknown элементы подтверждёнными
данными и зафиксировать изменения docs.

## 7. Добавление второго участника в ARIA_TEAM.yaml

Для теста двух ПК используем двух actors:

- `local-owner` — компьютер 1;
- `operator` — компьютер 2.

В `ARIA_TEAM.yaml` внутри списка `actors` добавить:

```yaml
- id: operator
  display_name: Manual test operator
  type: human
  roles:
  - contributor
  - reviewer
```

Не менять `schema_version`, `project_id` и существующих actors.

После сохранения:

```powershell
aria152 doctor --project $ProjectId
```

На этой стадии `ACCESS.yaml` ещё может иметь status `bootstrap`; это допустимо только до
первичной активации доступа.

## 8. Device identity и bootstrap на компьютере 1

```powershell
$OwnerRequest = Join-Path $Exchange 'owner-pc1-enrollment.json'

aria152 identity enroll `
  --actor local-owner `
  --device owner-pc1 `
  --display-name 'Owner PC 1' `
  --output $OwnerRequest

aria152 identity whoami --actor local-owner

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  access bootstrap --project $ProjectId

aria152 access status --project $ProjectId
aria152 access audit --project $ProjectId
aria152 doctor --project $ProjectId
```

Ожидается:

- private key остаётся в локальном runtime ПК1;
- enrollment request содержит только публичную информацию;
- access status становится `active`;
- access revision после bootstrap равна `1`;
- audit содержит один подписанный event;
- project doctor возвращает `ok: true`.

Enrollment request можно хранить в `exchange`; private key туда копировать нельзя.

## 9. Передача проекта компьютеру 2

Сначала дождаться полного Google Drive sync на ПК1. На ПК2 дождаться появления всех файлов
`example-aria`, затем получить отдельный product Git clone и ту же ветку:

```powershell
$ProjectId = 'example'
$Code = 'C:\Projects\example'
$Docs = 'G:\My Drive\ARIA\example-aria'
$Exchange = 'G:\My Drive\ARIA\exchange'

git clone https://example.invalid/your/example.git $Code
git -C $Code switch feature/manual-152
```

Если ветка ещё не опубликована, сначала выполнить push с ПК1.

На ПК2 выполнить только `register`, а не `init`:

```powershell
aria152 register `
  --project $ProjectId `
  --docs-root $Docs `
  --code-root $Code

aria152 doctor --project $ProjectId
aria152 projects
```

Ожидается:

- тот же `project_id`;
- локальные пути ПК2 записаны только в локальный `projects.toml`;
- общие `PROJECT.yaml`, `STATE.yaml` и history не перезаписаны;
- doctor возвращает `ok: true`.

## 10. Enrollment и grant компьютера 2

На ПК2:

```powershell
$OperatorRequest = Join-Path $Exchange 'operator-pc2-enrollment.json'

aria152 identity enroll `
  --actor operator `
  --device operator-pc2 `
  --display-name 'Operator PC 2' `
  --output $OperatorRequest

aria152 identity whoami --actor operator
```

Дождаться upload request на ПК2 и download на ПК1.

На ПК1 выполнить grant при access revision `1`:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  access grant `
  --project $ProjectId `
  --request $OperatorRequest `
  --permission project.read `
  --permission run.create `
  --permission run.advance `
  --permission verify.execute `
  --permission evidence.sign `
  --permission team.claim `
  --permission backlog.read `
  --permission backlog.write `
  --permission backlog.claim `
  --permission backlog.close `
  --version '1.5.*' `
  --branch 'feature/*' `
  --expected-revision 1
```

Ожидается access revision `2`.

После полного sync на ПК2:

```powershell
aria152 access status --project $ProjectId
aria152 access audit --project $ProjectId

aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog list --project $ProjectId --version 1.5.2
```

Все команды должны завершиться успешно.

## 11. Обязательный протокол передачи управления через Google Drive

Перед каждой мутирующей командой:

1. Другой ПК прекращает ARIA/Git-записи.
2. Писатель выполняет одну операцию.
3. Писатель закрывает процесс и ждёт статуса Google Drive «синхронизация завершена».
4. Второй ПК ждёт завершения download.
5. На обоих ПК сравниваются SHA критических файлов.
6. Только после совпадения SHA управление передаётся второму ПК.

Команда сравнения:

```powershell
Get-FileHash `
  (Join-Path $Docs 'ACCESS.yaml'), `
  (Join-Path $Docs 'ACCESS_HISTORY.jsonl'), `
  (Join-Path $Docs 'BACKLOG.yaml'), `
  (Join-Path $Docs 'STATE.yaml'), `
  (Join-Path $Docs 'HISTORY.jsonl') `
  -Algorithm SHA256
```

Для отсутствующего `ACCESS_HISTORY.jsonl` до bootstrap исключите этот путь.

Если SHA не совпадает или Drive показывает две версии файла, остановить работу. Не выбирать
версию наугад: сохранить обе копии, определить последнюю подтверждённую revision/audit head и
восстановить проект из проверенного состояния.

## 12. Полный ручной тест backlog на двух ПК

Все действия ниже выполнять последовательно с handoff после каждой мутации.

### 12.1. Добавление — ПК1

```powershell
$Add = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog add `
  --project $ProjectId `
  --title 'Ручная проверка ARIA 1.5.2' `
  --type feature `
  --priority high `
  --target-version 1.5.2 `
  --acceptance 'Элемент проходит assign, claim, block, reclaim и evidence completion' `
  --source-kind manual-test `
  --source-ref two-pc-152 `
  --expected-revision 0 | ConvertFrom-Json

$ItemId = $Add.item.id
$Add
```

Ожидается revision `1`, status `open`, непустой `BLG-...` id.

### 12.2. Read-back — ПК2

После sync:

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog show --project $ProjectId --item $ItemId
```

ID, title, revision и source должны совпасть.

### 12.3. Assign — ПК1

`operator` не получил `backlog.assign`; assign выполняет owner:

```powershell
$Assign = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog assign `
  --project $ProjectId `
  --item $ItemId `
  --assignee operator `
  --expected-revision 1 | ConvertFrom-Json
```

Ожидается revision `2`, status `assigned`, assignee `operator`.

### 12.4. Claim — ПК2

```powershell
$Claim = aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog claim `
  --project $ProjectId `
  --item $ItemId `
  --expected-revision 2 | ConvertFrom-Json
```

Ожидается revision `3`, status `in_progress`.

### 12.5. Block — ПК2

```powershell
$Block = aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog block `
  --project $ProjectId `
  --item $ItemId `
  --reason 'Проверка блокировки' `
  --expected-revision 3 | ConvertFrom-Json
```

Ожидается revision `4`, status `blocked`, сохранён reason.

### 12.6. Reassign и повторный claim

ПК1:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog assign `
  --project $ProjectId `
  --item $ItemId `
  --assignee operator `
  --expected-revision 4
```

Ожидается revision `5`, status `assigned`.

После sync ПК2:

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog claim `
  --project $ProjectId `
  --item $ItemId `
  --expected-revision 5
```

Ожидается revision `6`, status `in_progress`.

### 12.7. Evidence-bound completion — ПК2

```powershell
$Head = git -C $Code rev-parse HEAD

aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog done `
  --project $ProjectId `
  --item $ItemId `
  --evidence "git:$Head" `
  --expected-revision 6
```

Ожидается revision `7`, status `done`, один проверенный Git evidence ref. ARIA принимает
только полный 40-символьный SHA commit, существующего в зарегистрированном `code_root` и
совпадающего с текущим HEAD при чистом working tree. Альтернатива — `run:RUN_ID`, если
завершённый run находится в локальном runtime этого же проекта и повторно проходит полную
closure-проверку contract/anchor, context, verification, convergence, result и SHA.

### 12.8. Audit и sync

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog audit --project $ProjectId --version 1.5.2

aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog sync `
  --project $ProjectId `
  --version 1.5.2 `
  --expected-revision 7
```

Если новых runs/findings нет, sync должен быть idempotent и сохранить revision `7`.

## 13. Обязательные отрицательные backlog-тесты

### Stale revision

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog assign `
  --project $ProjectId `
  --item $ItemId `
  --assignee operator `
  --expected-revision 1

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: stale revision была принята'
}
```

Команда обязана завершиться ненулевым exit code.

### Подмена Git-ветки

На фактической ветке `feature/manual-152`:

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog list `
  --project $ProjectId `
  --version 1.5.2 `
  --branch main

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: подмена branch context была принята'
}
```

Ожидается сообщение, что branch context не совпадает с зарегистрированным Git checkout.

### Запрещённая версия

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog list --project $ProjectId --version 2.0.0

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: запрещённая version scope была принята'
}
```

Ожидается отказ access policy.

### Completion без evidence

CLI требует хотя бы один `--evidence` и должна отклонить команду ещё на уровне аргументов.

### Незавершённая dependency и неподтверждённый evidence

На ПК1 после revision `7` создать prerequisite и зависимую задачу:

```powershell
$Prerequisite = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog add `
  --project $ProjectId `
  --title 'Prerequisite для dependency gate' `
  --type feature `
  --priority normal `
  --target-version 1.5.2 `
  --acceptance 'Prerequisite завершён с проверенным Git evidence' `
  --source-kind manual-test `
  --source-ref dependency-prerequisite `
  --expected-revision 7 | ConvertFrom-Json

$Dependent = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog add `
  --project $ProjectId `
  --title 'Dependent задача' `
  --type feature `
  --priority normal `
  --target-version 1.5.2 `
  --acceptance 'Claim разрешён только после prerequisite' `
  --dependency $Prerequisite.item.id `
  --source-kind manual-test `
  --source-ref dependency-dependent `
  --expected-revision 8 | ConvertFrom-Json

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog claim `
  --project $ProjectId `
  --item $Dependent.item.id `
  --expected-revision 9

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: claim с незавершённой dependency была принята'
}
```

Revision должна остаться `9`. Затем принять prerequisite и проверить отказ произвольной
evidence-строки:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog claim `
  --project $ProjectId `
  --item $Prerequisite.item.id `
  --expected-revision 9

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog done `
  --project $ProjectId `
  --item $Prerequisite.item.id `
  --evidence 'manual:trust-me' `
  --expected-revision 10

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: неподтверждённая evidence-строка была принята'
}

$Head = git -C $Code rev-parse HEAD
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog done `
  --project $ProjectId `
  --item $Prerequisite.item.id `
  --evidence "git:$Head" `
  --expected-revision 10

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog claim `
  --project $ProjectId `
  --item $Dependent.item.id `
  --expected-revision 11
```

После завершения prerequisite повторный claim зависимой задачи должен пройти и дать
revision `12`. Для продолжения manual test её можно закрыть тем же `git:$Head` при
`--expected-revision 12`; итоговая revision будет `13`.

## 14. Проверка team lease

Team leases локальны для конкретного ПК. Этот тест проверяет механизм, но не является
межкомпьютерной блокировкой через Google Drive.

На одном ПК:

```powershell
$Team = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  team status --project $ProjectId | ConvertFrom-Json

$TeamRevision = $Team.revision

$Lease = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  team claim `
  --project $ProjectId `
  --task MANUAL-TASK-001 `
  --actor operator `
  --expected-revision $TeamRevision `
  --ttl-seconds 600 | ConvertFrom-Json

$Lease
```

Повторный claim со старой revision должен завершиться отказом. Затем освободить lease:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  team release `
  --project $ProjectId `
  --task MANUAL-TASK-001 `
  --actor operator `
  --token $Lease.lease.token `
  --expected-revision $Lease.revision
```

Неверный token должен завершиться отказом.

## 15. Проверка feature/run и автоматического backlog

Managed feature выполняется полностью на одном ПК, потому что run artifacts находятся в
локальном runtime.

На ПК1:

```powershell
$Feature = aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  feature `
  --project $ProjectId `
  --task 'Добавить тестовый health signal' | ConvertFrom-Json

$RunId = $Feature.run_id
$Feature

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  lifecycle --project $ProjectId --run $RunId
```

Ожидается:

- новый run id;
- manifest/context/scope paths;
- route `standard` либо выбранный `deep`;
- новый автоматически связанный backlog item;
- lifecycle сообщает следующую фазу.

Дальнейшие `specify → clarify → plan → tasks → implement → verify → converge` выполняет Codex
на том же ПК. Пользователь не должен вручную сочинять Feature Contract, role evidence,
convergence SHA или receipts.

Для ручной проверки дайте Codex команду:

```text
Работай только с проектом example через ARIA 1.5.2.
Заверши тестовую standard feature "Добавить тестовый health signal":
specify, clarify, plan, tasks, implement, реальные tests, aria verify,
independent review и converge. Показывай каждый ARIA verdict.
```

После завершения:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  status --project $ProjectId

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  history --project $ProjectId --verify

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  map-status --project $ProjectId

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  backlog sync `
  --project $ProjectId `
  --version 1.5.2 `
  --expected-revision CURRENT_BACKLOG_REVISION
```

Связанный backlog item должен закрыться либо заблокироваться согласно реальному итогу run.

Не продолжать этот run на ПК2. ПК2 может начать свой отдельный run в своей Git-ветке после
синхронизации project docs.

## 16. Проверка signing и Evidence Package

Private signing key хранить только локально:

```powershell
$PrivateKey = 'C:\ARIA\keys\owner-private.pem'
$PublicKey = Join-Path $Exchange 'owner-public.pem'
$Package = Join-Path $Exchange 'manual-run.aria-evidence'

aria152 key generate `
  --private-key $PrivateKey `
  --public-key $PublicKey

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  evidence export `
  --project $ProjectId `
  --run $RunId `
  --output $Package `
  --private-key $PrivateKey `
  --actor local-owner

aria152 evidence inspect --package $Package
aria152 evidence verify --package $Package
```

Ожидается:

- package schema v2;
- SHA inventory;
- signature valid;
- private key отсутствует внутри package;
- inspect не выдаётся за signature verification;
- изменение архива после подписи приводит к отказу verify.

Trust-policy test требует заранее внести public key в `TRUST.yaml` и разрешить его в выбранной
policy. Не добавлять private key в `TRUST.yaml`.

## 17. Проверка GitHub/CI adapter

Генерация reference workflow:

```powershell
$Workflow = Join-Path $Code '.github\workflows\aria-ci.yml'

aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  ci github --project $ProjectId --output $Workflow

Select-String -Path $Workflow -Pattern 'aria-codex==1.5.2'
```

Workflow должен:

- использовать hash-pinned GitHub actions;
- устанавливать `aria-codex==1.5.2`;
- разделять execute и attest;
- не передавать signing secret в execute.

Полный `ci prepare → execute → attest → import`, signed review и integration gate требует
настроенного `TRUST.yaml`, завершённого run, clean commit и отдельного signing boundary.
Эта цепочка полностью проверяется автоматическим `release-check` из раздела 20.

## 18. Проверка revoke

Выполнять в конце, потому что ПК2 потеряет доступ.

Сначала узнать текущую access revision:

```powershell
aria152 access status --project $ProjectId
```

На ПК1:

```powershell
aria152 `
  --identity-actor local-owner `
  --identity-device owner-pc1 `
  access revoke `
  --project $ProjectId `
  --actor operator `
  --device operator-pc2 `
  --expected-revision CURRENT_ACCESS_REVISION
```

После sync на ПК2:

```powershell
aria152 `
  --identity-actor operator `
  --identity-device operator-pc2 `
  backlog list --project $ProjectId --version 1.5.2

if ($LASTEXITCODE -eq 0) {
    throw 'ОШИБКА: revoked device сохранило доступ'
}
```

Для восстановления создать на ПК2 новый device id, например `operator-pc2-restored`, и
выполнить новый enrollment/grant с актуальной access revision.

## 19. Проверка миграции

Не проверять migration на единственной рабочей копии проекта.

Безопасные варианты:

1. Полный автоматический `release-check` — он создаёт disposable legacy project и проверяет
   1.3 → 1.4 → 1.5.2.
2. Сделать отдельную копию project docs 1.4, зарегистрировать её под отдельным project id и
   выполнить:

```powershell
aria152 upgrade-1-5 --project disposable-legacy
aria152 doctor --project disposable-legacy
```

Ожидается:

- `PROJECT.yaml.framework_version: 1.5.2`;
- созданы `ACCESS.yaml` и `BACKLOG.yaml`;
- team/trust сохранены и обновлены;
- повторный upgrade idempotent;
- открытый legacy run блокирует migration;
- recovery journal восстанавливает preimage после частичной записи.

## 20. Полная автоматическая приёмка всей ARIA 1.5.2

Это обязательный способ проверить всё ядро, включая то, что неудобно и опасно воспроизводить
вручную на рабочем проекте:

- полный unit/integration/adversarial regression без фиксированного числа тестов;
- performance regression;
- clean wheel build и offline install;
- console entry point и provenance;
- init/register/doctor/routes;
- trusted verify и convergence;
- два actors/devices и claim collision;
- version/branch rejection;
- evidence completion;
- migration 1.3 → 1.4 → 1.5.2;
- CI prepare/execute/attest/import;
- integration gate;
- shadow → active cutover и active doctor;
- engine/candidate stability.

Условия:

- source candidate должен иметь commit;
- `git status --short` должен быть пуст;
- output path должен отсутствовать;
- нельзя использовать старый acceptance report.

```powershell
git -C $Framework status --short

$Acceptance = 'C:\ARIA-Acceptance\aria-1.5.2-manual'
aria152 release-check --output $Acceptance

$Report = Get-Content `
  (Join-Path $Acceptance 'release-acceptance.json') `
  -Raw | ConvertFrom-Json

$Report.ok
($Report.checks | Where-Object { -not $_.ok }).Count
$Report.wheel
```

PASS:

```text
report.ok = true
failed checks = 0
каждый checks[].ok = true
wheel = aria_codex-1.5.2-py3-none-any.whl
source_git.clean = true
initial/final engine SHA совпадают
initial/final candidate SHA совпадают
```

Любой skipped или failed check означает общий FAIL.

## 21. Что нельзя считать успешной проверкой

Не является достаточным:

- только `aria doctor`;
- только exit code без raw log;
- старый report версии 1.5.1;
- тесты из source tree без clean-wheel install;
- ручная печать слов `FOCUSED`, `E2E` или `ADVERSARIAL`;
- проверка Google Drive sync вместо ARIA audit;
- успешная локальная claim на двух несинхронных ПК;
- наличие подписи без trust-policy проверки;
- продолжение одного локального run на другом ПК;
- отсутствие видимых ошибок при наличии conflicted copies.

## 22. Итоговый чек-лист пользователя

- [ ] На обоих ПК framework/package/runtime показывают 1.5.2.
- [ ] Framework, code, docs и runtime физически разделены.
- [ ] `workspaces` нигде не требуется.
- [ ] На ПК2 выполнен `register`, а не повторный `init`.
- [ ] Private device/signing keys не находятся в Google Drive.
- [ ] Access bootstrap, grant, audit и revoke проверены.
- [ ] Backlog add/show/assign/claim/block/done/sync/audit проверены.
- [ ] Claim с незавершённой dependency и произвольный completion evidence отклонены.
- [ ] Stale revision, wrong branch, wrong version и revoked device отклонены.
- [ ] Handoff между ПК выполняется только после полного sync и сравнения SHA.
- [ ] Team lease проверен как локальный механизм, не как Google Drive lock.
- [ ] Каждый managed run полностью выполняется на одном ПК.
- [ ] Evidence package создан, inspected и cryptographically verified.
- [ ] GitHub adapter закрепляет `aria-codex==1.5.2`.
- [ ] Migration проверена только на disposable project.
- [ ] `release-check` завершён с `ok=true` и без failed/skipped checks.

## 23. Если нужна настоящая одновременная работа

Google Drive-синхронизация не превращает два локальных runtime в один coordinator. Для
одновременных мутаций одного backlog/team state нужен отдельный coordination layer:

- общий файловый сервер с подтверждённой поддержкой межмашинных locks и atomic replace;
- либо центральный сервис/БД с optimistic concurrency;
- либо организационное разделение: разные Git-ветки и разные runs без одновременной записи
  общих YAML.

ARIA 1.5.2 гарантирует локальную транзакционность и проверяемую последовательную совместную
работу. Она не заявляет real-time multi-writer coordination поверх Google Drive.
