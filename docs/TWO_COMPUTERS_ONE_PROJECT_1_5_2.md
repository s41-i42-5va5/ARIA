# ARIA 1.5.2 — один общий проект на двух компьютерах

Эта инструкция предназначена для одного владельца проекта, который попеременно работает
на двух Windows-компьютерах. На обоих компьютерах используется один actor `local-owner`,
но разные device identity: `pc1` и `pc2`.

## Статус проверки

- команды сверены с установленной CLI ARIA 1.5.2;
- локальный smoke-сценарий с двумя независимыми runtime и Git checkout прошёл цепочку
  `init → enroll pc1 → bootstrap → register pc2 → enroll pc2 → grant → audits → doctor`;
- SHA-256 указанного production ZIP повторно совпал;
- фактическую передачу файлов через ваш Google Drive необходимо подтвердить контрольным
  тестом из раздела 18, потому что она выполняется внешним клиентом Google, а не ARIA.

## 1. Главный принцип

Общими между компьютерами являются только:

- Git-история исходного кода через Git remote;
- документы проекта ARIA через Google Drive;
- публичные enrollment requests и переносимые evidence packages.

На каждом компьютере отдельно создаются:

- framework root ARIA;
- `venv-1.5.2`;
- `aria-runtime`;
- локальный Git clone продукта;
- device identity и private key;
- локальные runs, locks и execution receipts.

```text
                              Git remote
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
            локальный Git clone        локальный Git clone
                    │                           │
                Компьютер 1                 Компьютер 2
                    │                           │
                    └──────── Google Drive ─────┘
                          общий docs_root
```

Google Drive не является распределённой базой данных и не даёт ARIA общий lock. Поэтому
допускается только один компьютер-писатель в каждый момент времени. Один managed run нельзя
начать на одном ПК и продолжить на другом: run находится в локальном `aria-runtime`.

## 2. Что устанавливается и что синхронизируется

| Объект | Компьютер 1 | Компьютер 2 | Способ передачи |
|---|---|---|---|
| ARIA framework | локальная копия | локальная копия | проверенный ZIP |
| `venv-1.5.2` | локально | локально | не переносить |
| `aria-runtime` | локально | локально | не переносить |
| Код продукта | локальный clone | локальный clone | Git remote |
| `.git` продукта | локально | локально | Git, не Google Drive |
| Project docs ARIA | общий каталог | тот же общий каталог | Google Drive |
| Private device key | только ПК1 | только ПК2 | не переносить |
| Enrollment request | публичный файл | публичный файл | `exchange` в Drive |

Не помещайте в Google Drive:

- `venv-1.5.2` и любые `.venv`;
- `aria-runtime`;
- private keys;
- `.aria-work`;
- работающую `.git` двух компьютеров;
- незавершённые локальные run artifacts.

## 3. Предварительные требования на обоих компьютерах

Нужны:

1. Windows x64.
2. Python 3.12 x64.
3. Git.
4. Google Drive for desktop.
5. Доступ к одному Git remote продукта.
6. Доступ к одной папке Google Drive с документами проекта.

Проверьте Python и Git:

```powershell
py -3.12 --version
git --version
```

Если команда `py` отсутствует, `setup.ps1` может использовать доступный `python.exe` или
явно заданный путь:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\setup.ps1 `
  -Python 'C:\Path\To\Python312\python.exe'
```

## 4. Подготовка Google Drive

Создайте общий родительский каталог:

```text
G:\My Drive\ARIA\
├── my-project-aria\   общий docs_root; создаст ARIA init
└── exchange\          публичные enrollment requests и evidence packages
```

До выполнения `aria init` каталог `my-project-aria` должен отсутствовать. Создать заранее
нужно только `G:\My Drive\ARIA` и `exchange`.

На обоих компьютерах:

1. Откройте одну и ту же папку Google Drive.
2. Предпочтительно включите Mirror files.
3. При Stream files сделайте `my-project-aria` и `exchange` доступными offline.
4. Убедитесь, что Drive не показывает ошибок синхронизации.
5. Запишите фактический локальный путь к Drive на каждом ПК. Буквы диска могут отличаться.

## 5. Установка ARIA на компьютере 1

Используйте только проверенный архив:

```text
aria-codex-1.5.2-prod-521d248.zip
SHA-256: f723f849a353fa80bbe41a36fe6eb617e5ac64babb02c5db658a514cfad87392
```

В PowerShell:

```powershell
$Package = 'D:\Transfer\aria-codex-1.5.2-prod-521d248.zip'
$Expected = 'f723f849a353fa80bbe41a36fe6eb617e5ac64babb02c5db658a514cfad87392'
$Actual = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()

if ($Actual -ne $Expected) {
    throw "Неверный SHA-256 архива: $Actual"
}

$LocalRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ARIA'
New-Item -ItemType Directory -Path $LocalRoot -Force | Out-Null
Expand-Archive -LiteralPath $Package -DestinationPath $LocalRoot

$Framework = Join-Path $LocalRoot 'aria-codex-1.5.2'
Set-Location $Framework

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

После установки:

```text
Documents\ARIA\
├── aria-codex-1.5.2\
├── venv-1.5.2\
└── aria-runtime\
```

Подготовьте текущую PowerShell-сессию на ПК1:

```powershell
$LocalRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ARIA'
$Framework = Join-Path $LocalRoot 'aria-codex-1.5.2'
$AriaExe = Join-Path $LocalRoot 'venv-1.5.2\Scripts\aria.exe'
$env:ARIA_RUNTIME_ROOT = Join-Path $LocalRoot 'aria-runtime'
$Actor = 'local-owner'
$Device = 'pc1'

function aria152 {
    & $AriaExe --framework-root $Framework @args
}

function ariaOwner {
    & $AriaExe `
      --framework-root $Framework `
      --identity-actor $Actor `
      --identity-device $Device `
      @args
}

aria152 doctor
```

Результат framework doctor должен содержать `"ok": true` и `"version": "1.5.2"`.

## 6. Подготовка Git-репозитория продукта на компьютере 1

### Существующий проект

```powershell
$ProjectId = 'my-project'
$Remote = 'https://YOUR-GIT-SERVER/YOUR-ACCOUNT/my-project.git'
$Code = 'C:\Projects\my-project'

git clone $Remote $Code
git -C $Code status
```

### Новый проект

```powershell
$ProjectId = 'my-project'
$Remote = 'https://YOUR-GIT-SERVER/YOUR-ACCOUNT/my-project.git'
$Code = 'C:\Projects\my-project'

New-Item -ItemType Directory -Path $Code -Force | Out-Null
git -C $Code init -b main

# Создайте реальные исходные файлы и manifests проекта.

git -C $Code add .
git -C $Code commit -m 'Initial project baseline'
git -C $Code remote add origin $Remote
git -C $Code push -u origin main
```

ARIA принимает в `--code-root` только Git top-level с существующим commit. Перед `init`
команда `git -C $Code status` должна завершаться успешно.

## 7. Создание общих документов проекта на компьютере 1

Укажите реальный путь Google Drive:

```powershell
$ProjectId = 'my-project'
$Code = 'C:\Projects\my-project'
$Docs = 'G:\My Drive\ARIA\my-project-aria'
$Exchange = 'G:\My Drive\ARIA\exchange'

if (Test-Path -LiteralPath $Docs) {
    throw 'Для первого aria init docs_root должен отсутствовать'
}

aria152 init `
  --project $ProjectId `
  --display-name 'My Project' `
  --code-root $Code `
  --docs-root $Docs
```

`init` выполняется только один раз — на ПК1. Он создаёт:

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
specs\
adr\
knowledge\
```

Если в Git-репозитории продукта обнаружены поддерживаемые test manifests, `init` также
создаст `VERIFY.yaml`. Если таких manifests нет, ARIA не выдумывает команды проверки и
не создаёт этот файл: их нужно определить по реальному проекту до первой build-задачи.

Проверьте:

```powershell
aria152 projects
aria152 doctor --project $ProjectId
git -C $Code status
```

`init` создаёт детерминированный inventory, но не заменяет анализ архитектуры. Semantic
bootstrap выполняется после активации identity/access в следующем разделе. Не выдумывайте
компоненты, команды тестов и зависимости.

## 8. Identity и первичная активация доступа на компьютере 1

```powershell
$OwnerRequestPc1 = Join-Path $Exchange 'local-owner-pc1-enrollment.json'

aria152 identity enroll `
  --actor local-owner `
  --device pc1 `
  --display-name 'Owner PC 1' `
  --output $OwnerRequestPc1

aria152 identity whoami --actor local-owner

ariaOwner access bootstrap --project $ProjectId
aria152 access status --project $ProjectId
aria152 access audit --project $ProjectId
aria152 doctor --project $ProjectId
```

После bootstrap:

- private key ПК1 остаётся только в локальном `aria-runtime`;
- `ACCESS.yaml` получает status `active`;
- access revision обычно становится `1`;
- enrollment request в `exchange` содержит только публичную информацию.

До первой feature попросите Codex провести semantic bootstrap: прочитать реальные исходники,
проверить `STACK.md`, провести repository review и заменить generic/unknown элементы
`SYSTEM_MAP.yaml` подтверждёнными данными.

Пример команды для создания review-run уже с активной identity:

```powershell
$Review = ariaOwner run `
  --project $ProjectId `
  --task 'Провести semantic bootstrap и проверить архитектуру проекта' `
  --intent review `
  --mode deep `
  --target-type repository | ConvertFrom-Json

$Review
```

Дальнейший lifecycle этого review выполняет Codex на ПК1 согласно возвращённому manifest.
Завершите review на ПК1; не пытайтесь продолжить его на ПК2.

Дождитесь полного завершения Google Drive sync.

## 9. Установка ARIA и проекта на компьютере 2

На ПК2 повторите раздел 5 с тем же ZIP. Не копируйте с ПК1 `venv-1.5.2` и
`aria-runtime`.

Подготовьте PowerShell-сессию ПК2:

```powershell
$LocalRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ARIA'
$Framework = Join-Path $LocalRoot 'aria-codex-1.5.2'
$AriaExe = Join-Path $LocalRoot 'venv-1.5.2\Scripts\aria.exe'
$env:ARIA_RUNTIME_ROOT = Join-Path $LocalRoot 'aria-runtime'
$Actor = 'local-owner'
$Device = 'pc2'

function aria152 {
    & $AriaExe --framework-root $Framework @args
}

function ariaOwner {
    & $AriaExe `
      --framework-root $Framework `
      --identity-actor $Actor `
      --identity-device $Device `
      @args
}

aria152 doctor
```

Получите отдельный Git clone:

```powershell
$ProjectId = 'my-project'
$Remote = 'https://YOUR-GIT-SERVER/YOUR-ACCOUNT/my-project.git'
$Code = 'C:\Projects\my-project'
$Docs = 'G:\My Drive\ARIA\my-project-aria'
$Exchange = 'G:\My Drive\ARIA\exchange'

git clone $Remote $Code
git -C $Code status
```

Дождитесь полного download `my-project-aria` из Google Drive. На ПК2 выполняется `register`,
а не повторный `init`:

```powershell
aria152 register `
  --project $ProjectId `
  --code-root $Code `
  --docs-root $Docs

aria152 projects
aria152 doctor --project $ProjectId
```

Регистрация сохраняет абсолютные пути только в локальном runtime ПК2 и не перезаписывает
общие project docs.

## 10. Регистрация второго устройства того же владельца

На ПК2 создайте новую device identity:

```powershell
$OwnerRequestPc2 = Join-Path $Exchange 'local-owner-pc2-enrollment.json'

aria152 identity enroll `
  --actor local-owner `
  --device pc2 `
  --display-name 'Owner PC 2' `
  --output $OwnerRequestPc2

aria152 identity whoami --actor local-owner
```

Дождитесь upload request на ПК2 и download этого файла на ПК1.

На ПК1 прочитайте фактическую access revision:

```powershell
$Access = aria152 access status --project $ProjectId | ConvertFrom-Json
$Access.revision
```

Затем на ПК1 предоставьте ПК2 полный набор прав владельца проекта:

```powershell
$OwnerRequestPc2 = Join-Path $Exchange 'local-owner-pc2-enrollment.json'

$Grant = ariaOwner access grant `
  --project $ProjectId `
  --request $OwnerRequestPc2 `
  --permission project.read `
  --permission run.create `
  --permission run.advance `
  --permission verify.execute `
  --permission evidence.sign `
  --permission team.claim `
  --permission team.manage `
  --permission backlog.read `
  --permission backlog.write `
  --permission backlog.assign `
  --permission backlog.claim `
  --permission backlog.close `
  --permission release.manage `
  --permission access.manage `
  --version '1.5.*' `
  --branch 'main' `
  --branch 'feature/*' `
  --expected-revision $Access.revision | ConvertFrom-Json

$Grant
```

Если проект использует другие ветки, добавьте их отдельными `--branch`. Не используйте
устаревшую revision: при несовпадении повторно прочитайте `access status` и выясните, кто
изменил policy.

После полного Google Drive sync на ПК2:

```powershell
aria152 access status --project $ProjectId
aria152 access audit --project $ProjectId
ariaOwner backlog list --project $ProjectId --version 1.5.2
aria152 doctor --project $ProjectId
```

Теперь actor один — `local-owner`, но его устройства криптографически различаются.

## 11. Обязательный протокол передачи управления

Этот протокол выполняется перед каждой сменой активного компьютера.

### Действия на текущем активном компьютере

1. Не оставляйте незавершённый managed run для продолжения на другом ПК.
2. Завершите либо корректно заблокируйте текущую задачу на этом же компьютере.
3. Проверьте Git и отправьте изменения:

```powershell
git -C $Code status
git -C $Code push
```

4. Проверьте ARIA:

```powershell
aria152 doctor --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
ariaOwner history --project $ProjectId --verify
```

5. Рассчитайте SHA общих критических файлов:

```powershell
$Critical = @(
    'PROJECT.yaml',
    'ACCESS.yaml',
    'ACCESS_HISTORY.jsonl',
    'BACKLOG.yaml',
    'STATE.yaml',
    'HISTORY.jsonl'
) | ForEach-Object { Join-Path $Docs $_ } | Where-Object { Test-Path -LiteralPath $_ }

Get-FileHash -LiteralPath $Critical -Algorithm SHA256
```

6. Дождитесь статуса Google Drive «Синхронизация завершена».
7. Зафиксируйте активную Git-ветку и полный commit SHA:

```powershell
git -C $Code branch --show-current
git -C $Code rev-parse HEAD
```

### Действия на компьютере, который принимает управление

1. Не запускайте ARIA, пока Google Drive не завершил download.
2. Получите код только через Git:

```powershell
git -C $Code fetch --prune
git -C $Code switch NAME-OF-ACTIVE-BRANCH
git -C $Code pull --ff-only
git -C $Code status
git -C $Code rev-parse HEAD
```

3. Повторите `Get-FileHash` для общих документов и сравните значения.
4. Выполните:

```powershell
aria152 doctor --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
ariaOwner history --project $ProjectId --verify
ariaOwner status --project $ProjectId
```

Только после совпадения Git commit, SHA документов и успешных audit/doctor новый компьютер
становится писателем.

## 12. Ежедневное начало работы

В новой PowerShell-сессии сначала выполните блок переменных и функций из раздела 5 или 9.
Затем:

```powershell
git -C $Code fetch --prune
git -C $Code pull --ff-only
git -C $Code status

aria152 doctor --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
ariaOwner backlog list --project $ProjectId --version 1.5.2
ariaOwner status --project $ProjectId
```

Условия начала разработки:

- Google Drive полностью синхронизирован;
- этот компьютер назначен единственным писателем;
- Git working tree имеет ожидаемое состояние;
- активная ветка разрешена в `ACCESS.yaml`;
- `doctor`, access audit, backlog audit и history verify проходят.

## 13. Работа над новой задачей

Для каждой новой feature-задачи сначала создаётся ARIA feature-run. Весь run выполняется на
одном компьютере.

```powershell
$Feature = ariaOwner feature `
  --project $ProjectId `
  --task 'Реальное описание задачи' `
  --mode standard | ConvertFrom-Json

$RunId = $Feature.run_id
$Feature

ariaOwner lifecycle --project $ProjectId --run $RunId
```

Откройте возвращённые `context_path`, `manifest_path` и, если есть, `scope_path`. Следуйте
фактическим `route`, `assurance_plan`, `role_contract` и `STACK.md`.

Codex должен выполнить lifecycle целиком:

1. уточнить требования;
2. сформировать Feature Contract для standard/deep;
3. зафиксировать план и task graph;
4. вызвать `aria implement` до изменения product code;
5. изменить код в зарегистрированном Git root;
6. если manifest требует committed delta, создать требуемые Git commits и trailers до
   convergence;
7. выполнить реальные проверки через `aria verify`;
8. провести обязательные независимые роли/review;
9. сформировать convergence evidence;
10. завершить через `aria converge`;
11. прочитать итоговые status, history и backlog.

Пользователь не создаёт вручную `manifest.json`, contract lock, receipts, role evidence или
convergence SHA.

Готовый prompt для Codex:

```text
Работай только с зарегистрированным проектом my-project через ARIA 1.5.2.
Сначала выполни project doctor, затем создай feature для задачи:
"РЕАЛЬНОЕ ОПИСАНИЕ ЗАДАЧИ".
Пройди весь возвращённый lifecycle на этом компьютере: контекст, Feature Contract,
implement, изменения кода, реальные тесты через aria verify, независимый review,
convergence и итоговый read-back. Не подменяй evidence ручными утверждениями.
```

После завершения:

```powershell
ariaOwner status --project $ProjectId
ariaOwner history --project $ProjectId --verify
ariaOwner map-status --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
```

Порядок commit определяется manifest конкретного run. Для deep/build с изменениями ARIA
требует чистый исходный baseline, закоммиченный task delta, чистое финальное дерево и trailers
`ARIA-Task`, а на последнем commit также `ARIA-Run` и, когда применимо, `ARIA-Spec`/`ARIA-ADR`.
Такие commits должны существовать **до** `aria converge`. Для маршрутов без этого требования
не добавляйте фиктивный commit. После успешного convergence отправьте уже проверенные commits:

```powershell
git -C $Code status
git -C $Code push
```

Не используйте `git add .`, если не проверили полный список изменений. Точный формат commits
и trailers берите из `manifest_path` текущего run, а не копируйте универсально.

## 14. Учёт задач в BACKLOG

`BACKLOG.yaml` — подписанный реестр задач, их приоритета, владельца, зависимостей,
acceptance criteria и evidence. Managed feature автоматически создаёт или связывает backlog
item; после завершения run выполните sync, если это указано lifecycle.

Прочитать текущую revision:

```powershell
$Backlog = ariaOwner backlog list `
  --project $ProjectId `
  --version 1.5.2 | ConvertFrom-Json

$Backlog.revision
$Backlog.items
```

Добавить плановую задачу вручную:

```powershell
$Revision = $Backlog.revision

$Added = ariaOwner backlog add `
  --project $ProjectId `
  --title 'Описание задачи' `
  --type feature `
  --priority high `
  --target-version 1.5.2 `
  --acceptance 'Проверяемый критерий приёмки' `
  --assignee local-owner `
  --expected-revision $Revision | ConvertFrom-Json

$ItemId = $Added.item.id
$Added
```

После каждого изменения backlog используйте только revision, полученную из последнего
успешного ответа. Stale revision должна завершаться отказом — это защита от потерянного
обновления.

Не закрывайте managed feature только ручным текстом. Допустимое evidence:

- `run:RUN_ID` — завершённый и повторно проверяемый локальный ARIA run;
- `git:FULL_40_CHARACTER_SHA` — текущий HEAD чистого зарегистрированного code root.

Проверка цепочки и автоматическое обнаружение runs/findings:

```powershell
$Backlog = ariaOwner backlog list `
  --project $ProjectId `
  --version 1.5.2 | ConvertFrom-Json

ariaOwner backlog audit `
  --project $ProjectId `
  --version 1.5.2

ariaOwner backlog sync `
  --project $ProjectId `
  --version 1.5.2 `
  --expected-revision $Backlog.revision
```

## 15. Работа на двух ветках

Если на двух компьютерах нужны разные задачи, используйте разные Git-ветки, например:

```text
feature/task-a
feature/task-b
```

Но Google Drive docs всё равно имеют только одного писателя. Допустимо, чтобы ПК2 писал код
в своей ветке, пока ПК1 не меняет project docs. Перед любым `feature`, backlog, access,
converge или active closure снова требуется передача управления.

Не выполняйте одновременно на двух ПК:

- `access grant/revoke/bootstrap`;
- `backlog add/assign/claim/block/done/sync`;
- `feature`, `implement`, `converge`;
- active closure, map publication или изменение `STATE/HISTORY`;
- ручное редактирование общих ARIA-документов.

## 16. Что делать при конфликте Google Drive

Признаки конфликта:

- Drive создал файл с пометкой conflict/copy;
- SHA критических файлов на ПК различаются;
- ARIA сообщает stale revision, invalid signature, broken audit/history chain;
- один ПК не видит последнюю access/backlog revision.

Порядок действий:

1. Немедленно остановите записи на обоих ПК.
2. Не выбирайте версию файла наугад.
3. Сохраните обе конфликтующие копии вне `docs_root`.
4. На каждой полной копии отдельно проверьте:

```powershell
aria152 access audit --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
ariaOwner history --project $ProjectId --verify
```

5. Определите последнюю полную версию с валидной подписью, revision и hash-chain.
6. Восстановите весь согласованный набор связанных файлов, а не один случайный YAML.
7. Дождитесь полной синхронизации и повторите `doctor` на обоих ПК.

Не объединяйте вручную подписанные `ACCESS.yaml`, `BACKLOG.yaml`, audit/history JSONL и их
подписи. Такое слияние нарушает цепочку доверия.

## 17. Резервное копирование

Минимально необходимы:

- Git remote с product code;
- Google Drive с `docs_root`;
- отдельный периодический snapshot `docs_root`;
- экспортируемые публичные keys и evidence packages;
- проверенный ZIP ARIA 1.5.2 и его SHA-256.

Private device keys не храните в общей папке. При потере компьютера используйте другое
авторизованное устройство для revoke потерянного device и enroll нового.

## 18. Контрольный тест после настройки двух компьютеров

Выполняйте строго последовательно.

### ПК1

```powershell
aria152 doctor --project $ProjectId
ariaOwner access audit --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
```

Создайте одну небольшую реальную feature и полностью завершите её на ПК1. Выполните Git
commit/push и протокол передачи управления.

### ПК2

```powershell
git -C $Code pull --ff-only
aria152 doctor --project $ProjectId
ariaOwner access audit --project $ProjectId
ariaOwner backlog audit --project $ProjectId --version 1.5.2
ariaOwner status --project $ProjectId
ariaOwner history --project $ProjectId --verify
```

Затем создайте и полностью завершите другую небольшую feature на ПК2, выполните push и
верните управление ПК1.

Сценарий считается успешным, если:

- framework/package/runtime на обоих ПК показывают `1.5.2`;
- оба устройства проходят access audit;
- project doctor проходит на обоих ПК;
- код передаётся через Git без ручного копирования `.git`;
- docs передаются через Drive без конфликтующих копий;
- backlog revision и history head совпадают после sync;
- каждый run начинается и завершается на одном компьютере;
- обе feature имеют реальный Git/evidence trace.

## 19. Что не является поддерживаемым режимом

ARIA 1.5.2 не гарантирует:

- безопасную одновременную запись двух ПК в общий docs_root через Google Drive;
- продолжение одного локального run на другом компьютере;
- общий межкомпьютерный process-lock или team lease;
- exactly-one claim между двумя несинхронными копиями Drive;
- автоматическое разрешение конфликтующих подписанных файлов;
- синхронизацию Google Drive самой ARIA.

Если нужна настоящая параллельная работа двух писателей, требуется отдельный coordination
service с централизованной revision/lock boundary. Google Drive alone для этого недостаточно.

## 20. Краткий ежедневный чек-лист

### Начало

- [ ] Google Drive полностью синхронизирован.
- [ ] Этот ПК назначен единственным писателем.
- [ ] Git fetch/pull выполнены.
- [ ] Git branch и HEAD ожидаемые.
- [ ] `aria doctor --project` прошёл.
- [ ] access/backlog/history audit прошли.

### Работа

- [ ] Новая feature начата через `aria feature`.
- [ ] Run полностью остаётся на одном ПК.
- [ ] Использованы возвращённые context/manifest/scope.
- [ ] Проверки выполнены через `aria verify`.
- [ ] Review и convergence завершены доказательно.

### Завершение и handoff

- [ ] Status/history/backlog перечитаны.
- [ ] Product code закоммичен и отправлен в Git remote.
- [ ] Google Drive завершил upload.
- [ ] SHA критических docs записаны.
- [ ] На втором ПК Git commit и SHA docs совпали.
- [ ] Только после этого передано управление.

## 21. Связанные документы

- [README.md](../README.md) — возможности и быстрый старт ARIA.
- [GUIDE.md](GUIDE.md) — общий пользовательский workflow.
- [MANUAL_TEST_1_5_2.md](MANUAL_TEST_1_5_2.md) — расширенная ручная проверка всех
  механизмов 1.5.2.
- [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md) — критерии релизной приёмки.
