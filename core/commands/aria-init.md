Интерактивная инициализация ARIA в новом проекте. Разворачивает полный набор команд, шаблонов, hooks, MCP-конфигурации и генеральной спеки продукта.

**Синтаксис:** `/aria-init`

**PATHS:** после подстановки — `${{ENV_DOCS}}/PATHS.yaml`.
**МОДЕЛЬ:** субагенты — только `model: "opus"`.

**Предусловия:**
- Установлен Claude Code (Opus 4.6)
- Установлен Git
- Для contribute-back / release: установлен `gh CLI` с `gh auth login`
- Есть 2 каталога под docs-репо и code-репо (могут быть пусты или содержать существующий код)

---

## ШАГ 1: ИНТЕРВЬЮ (AskUserQuestion)

Задать пользователю 7 вопросов:

### 1.1 Имя проекта
- **slug** (kebab-case, для директорий): `my-project`, `data-hub`, ...
- **display name** (для документов): например: `MyProject`, `DataHub`, ...

### 1.2 Описание проекта
Одно-два предложения о продукте.

### 1.3 Адаптер
Выбор из `adapters/`:
- `python-fastapi` — Python backend + React frontend
- `kotlin-android` — Kotlin Android-приложение
- `csharp-avalonia` — C# .NET + Avalonia UI
- `custom` — без готового адаптера (минимальный режим)

### 1.4 Пути
- `docs_directory` — где будут лежать docs-репо (обычно в облачном хранилище: Yandex.Disk, iCloud, Google Drive, OneDrive, Obsidian vault)
- `code_directory` — где будет code-репо (обычно локальный путь, потом push на GitHub)

### 1.5 Переменные окружения
- `ENV_DOCS` — имя переменной для docs (например `MYPROJECT_DOCS`)
- `ENV_CODE` — имя переменной для code (например `MYPROJECT_CODE`)

### 1.6 **Команды проекта (Task 6, R6)**
Спросить команды для `project_config.yaml`, с дефолтами от адаптера:
- `test` — команда запуска тестов (`python -m pytest backend/tests/ -v` для python-fastapi, `./gradlew test` для kotlin-android и т.д.)
- `lint` — команда линтера
- `typecheck` — команда проверки типов
- `dev_server` — команда запуска dev-сервера (можно пропустить = пустая строка, если не применимо)

### 1.7 GitHub Repo (опционально)
- Планируется ли публикация на GitHub? Если да — `owner/repo` для записи в upstream `FORKS.md` при согласии пользователя.

---

## ШАГ 2: ПОДГОТОВКА ISSTOCHNIKA

Клонировать upstream ARIA (или использовать локальный путь):

```bash
# Свежий клон
gh repo clone Yura1980Yura/ARIA /tmp/aria-upstream
# или git clone https://github.com/Yura1980Yura/ARIA.git /tmp/aria-upstream

# Убедиться что есть нужные директории
ls /tmp/aria-upstream/core/commands/       # 11+ команд
ls /tmp/aria-upstream/core/templates/      # 6 шаблонов
ls /tmp/aria-upstream/core/protocols/      # fork_sync_playbook
ls /tmp/aria-upstream/adapters/            # 3 адаптера
ls /tmp/aria-upstream/scripts/             # validate_spec_e2e.sh, validate_e2e_results.sh, hooks/
```

---

## ШАГ 3: ПОДСТАНОВКА БАЗОВЫХ ШАБЛОНОВ

В **docs-репо** (`$DOCS_DIRECTORY`):

1. `CLAUDE.md.template` → `CLAUDE.md` — подставить `{{PROJECT_NAME}}`, `{{PROJECT_DESCRIPTION}}`, `{{ENV_DOCS}}`, `{{ENV_CODE}}`, `{{ADAPTER_NAME}}`, `{{TECH_STACK}}`, `{{COMMIT_FORMAT}}`, `{{CODE_DIRS}}`, `{{LANGUAGE_SPECIFIC_ANTIPATTERNS}}`, `{{INFRASTRUCTURE_SECTION}}`, `{{ARIA_VERSION}}`

2. `PATHS.yaml.template` → `PATHS.yaml`

3. `project_config.yaml.template` → `project_config.yaml` — **включая секцию `commands:` (R6, Task 1)**:
   ```yaml
   project: {slug}
   description: "{description}"
   paths:
     docs: "${{ENV_DOCS}}"
     code: "${{ENV_CODE}}"
   git:
     code_repo: "${{ENV_CODE}}"
     remote: "origin"
     branch: "main"
     auto_push: {true_или_false}
   commands:
     test: "{из ШАГ 1.6}"
     lint: "{из ШАГ 1.6}"
     typecheck: "{из ШАГ 1.6}"
     dev_server: "{из ШАГ 1.6 или пустая строка}"
   infrastructure:
     check_command: "{из adapter.yaml → infrastructure.check_command, или пустая строка если нет}"
     start_command: "{из adapter.yaml → infrastructure.start_command, или пустая строка если нет}"
   aria:
     version: "{ARIA_VERSION}"
     adapter: "{ADAPTER_NAME}"
   ```

4. `STATE.yaml.template` → `STATE.yaml` (заготовка с расширенным контрактом задачи, Task 8 R1: `files`, `tests`, `acceptance`, `commit`, `sessions`). Финальное наполнение — в ШАГ 10 после генерации SPEC.md.

5. `STACK.md.template` → `docs/STACK.md` — подставить стек из `adapter.yaml`

---

## ШАГ 4: КОПИРОВАНИЕ КОМАНД (СЕЛЕКТИВНО — только fork-level)

**Правило разделения команд** (Phase 1 audit v2.4, закрытие regression R17):

| Класс | Команды | Куда |
|-------|---------|------|
| **Fork-level** (12) | `auto`, `done`, `spec`, `review`, `next-task`, `research`, `status`, `aria-sync`, `adr-new`, `aria-docs-audit`, `e2e-gate`, `roadmap-sync` | Копируются в форк |
| **Upstream-only** (3) | `aria-init`, `aria-release`, `aria-triage` | **НЕ копируются в форк** — живут только в upstream ARIA |

Обоснование upstream-only:
- `aria-init` — запускается один раз для создания форка; в самом форке не нужна
- `aria-release` — релиз новой версии ARIA upstream; maintainer-only
- `aria-triage` — обработка входящих PR от других форков; maintainer-only

```bash
mkdir -p "$DOCS_DIRECTORY/.claude/commands"

# Fork-level команды — копируем
FORK_COMMANDS=(
  auto done spec review next-task research status
  aria-sync adr-new aria-docs-audit e2e-gate roadmap-sync
)
for cmd in "${FORK_COMMANDS[@]}"; do
  cp "/tmp/aria-upstream/core/commands/${cmd}.md" "$DOCS_DIRECTORY/.claude/commands/"
done

# Upstream-only команды (aria-init, aria-release, aria-triage) — НЕ копируем
```

Команды живут в `$DOCS_DIRECTORY/.claude/commands/` (правило 4 разделения репо — ARIA-инфраструктура в docs-репо).

**Валидация:**
```bash
for forbidden in aria-init.md aria-release.md aria-triage.md; do
  if [ -f "$DOCS_DIRECTORY/.claude/commands/$forbidden" ]; then
    echo "VIOLATION: upstream-only команда $forbidden попала в форк"
    exit 1
  fi
done
```

---

## ШАГ 5: АДАПТЕР-СПЕЦИФИЧНЫЕ ДОПОЛНЕНИЯ

Если адаптер != `custom`:

```bash
ADAPTER=$ADAPTER_NAME
cp /tmp/aria-upstream/adapters/$ADAPTER/adapter.yaml "$DOCS_DIRECTORY/adapter.yaml"

# Hooks в code-репо (только SOFT для форка)
mkdir -p "$CODE_DIRECTORY/scripts/hooks"
cp /tmp/aria-upstream/adapters/$ADAPTER/hooks/pre-commit "$CODE_DIRECTORY/scripts/hooks/"
bash /tmp/aria-upstream/scripts/hooks/install.sh "$CODE_DIRECTORY"
```

---

## ШАГ 6: УСТАНОВКА HOOKS

```bash
cd "$CODE_DIRECTORY"
git config core.hooksPath scripts/hooks
chmod +x scripts/hooks/*
# Проверка
git commit --allow-empty -m "test hooks" --dry-run || echo "hooks OK"
```

---

## ШАГ 7: ГЕНЕРАЦИЯ .mcp.json

Прочитать `adapter.yaml` → секция `mcp_servers`.

```python
# Псевдокод
if mcp_servers == {}:
    print("Адаптер не требует MCP-серверов. Пропускаю генерацию .mcp.json")
else:
    # Показать пользователю рекомендуемые MCP с reason + required
    # AskUserQuestion: "Какие MCP-серверы установить?"
    # По выбору пользователя сгенерировать .mcp.json

    # Windows: обернуть в cmd /c
    is_windows = (uname -s).lower().contains("mingw") or contains("cygwin") or contains("msys")

    mcp_json = {"mcpServers": {}}
    for name, config in selected_servers.items():
        entry = {"env": config.get("env", {})}
        if is_windows:
            entry["command"] = "cmd"
            entry["args"] = ["/c", config["command"], *config["args"]]
        else:
            entry["command"] = config["command"]
            entry["args"] = config["args"]
        mcp_json["mcpServers"][name] = entry

    # Записать в docs-repo и code-repo (дублирование по стандарту Claude Code)
    write("$DOCS_DIRECTORY/.mcp.json", json.dumps(mcp_json, indent=2))
    cp "$DOCS_DIRECTORY/.mcp.json" "$CODE_DIRECTORY/.mcp.json"
```

**.mcp.json пишется в ОБА репо** (стандарт Claude Code), хэши должны совпадать. `/aria-docs-audit` проверит идентичность.

---

## ШАГ 8: JUNCTION project-docs/ → docs-repo

В code-repo создать junction на docs-repo:

```bash
# Linux / macOS
ln -s "$DOCS_DIRECTORY" "$CODE_DIRECTORY/project-docs"

# Windows (Git Bash)
cmd //c "mklink /J \"$(cygpath -w $CODE_DIRECTORY)\\project-docs\" \"$(cygpath -w $DOCS_DIRECTORY)\""
```

**Риск:** на части Windows-версий `mklink /J` требует admin-прав. Если FAIL — запросить у пользователя admin-права или предложить альтернативу (symlink через `mklink /D` или ручной путь).

---

## ШАГ 9: СОЗДАНИЕ ДИРЕКТОРИЙ

В docs-repo:

```bash
mkdir -p "$DOCS_DIRECTORY/docs/spec"
mkdir -p "$DOCS_DIRECTORY/docs/ADR"
mkdir -p "$DOCS_DIRECTORY/docs/research"
mkdir -p "$DOCS_DIRECTORY/docs/policies"
mkdir -p "$DOCS_DIRECTORY/tests/e2e"
mkdir -p "$DOCS_DIRECTORY/backlog"
mkdir -p "$DOCS_DIRECTORY/reports"  # опционально, для артефактов анализов

# Копия политик из upstream
cp /tmp/aria-upstream/docs/policies/*.md "$DOCS_DIRECTORY/docs/policies/"
```

---

## ШАГ 10: ГЕНЕРАЦИЯ SPEC.md (интерактивно, Task 8)

Ключевой шаг — создание генеральной спеки продукта.

### 10.1 AskUserQuestion (5 вопросов)

1. **Назначение продукта:** что делает, для кого, какую проблему решает (1-2 предложения)?
2. **Целевая аудитория:** роли, рынок, сегмент?
3. **3-5 ключевых принципов:** что отличает от конкурентов?
4. **Ключевые сущности домена:** 5-7 основных сущностей (можно перечислить)?
5. **Первая фаза (Phase 1):** цель, результат, 3-10 задач с названиями?

### 10.2 AI заполняет SPEC.md.template

Взять `core/templates/SPEC.md.template`, подставить ответы из 10.1. Результат — `$DOCS_DIRECTORY/docs/SPEC.md`.

Обязательные секции заполнены (1-9). Опциональные (7. Implementation Guide, 8. Evolution & Vision) — оставить пустыми или с placeholder "будет заполнено при необходимости".

### 10.3 Финализация STATE.yaml

Взять задачи из ответа 10.1.5 (первая фаза) → записать в `STATE.yaml` под `phase_1.tasks[]` с расширенным контрактом:

```yaml
phase_1:
  title: "{Phase 1 title}"
  spec: "docs/SPEC.md#roadmap-phase-1"
  tasks:
    - name: "{first_task}"
      priority: 1
      status: not_started
      depends_on: []
      description: "{...}"
      scope: "[{SCOPE_GUESS}]"
      spec: null  # пока нет детальной спеки — создаст /spec позже
      files: []
      tests: []
      acceptance: []
      commit: null
      completed: null
```

Задачи без зависимостей — priority=1, остальные приоритеты определяет AI из описания.

### 10.4 Показ пользователю

```
Сгенерировано:
  - SPEC.md (N секций заполнено)
  - STATE.yaml (M задач Phase 1)

Просмотреть/подтвердить?
```

После подтверждения — продолжить к ШАГ 11.

---

## ШАГ 11: ПЕРВЫЙ TWO-PHASE COMMIT

```bash
cd "$CODE_DIRECTORY"

# Инициализация git если новый репо
[ ! -d .git ] && git init

# .gitignore с запретом ARIA-артефактов (Task 11 правило 4, Task 16)
cat >> .gitignore <<'EOF'

# ARIA infrastructure MUST live only in $DOCS_DIRECTORY.
# Any reappearance here = violation of repo-separation rule (CLAUDE.md правило 4).
.claude/
.dev/
CLAUDE.md
STATE.yaml
CHANGELOG.md
ROADMAP.md
SYS_CHANGELOG.md
PATHS.yaml
project_config.yaml
SPEC.md
STACK.md
REFERENCES.md
docs/
reports/
backlog/
EOF

# Phase 1 — код + инфраструктура code-репо
git add {{CODE_DIRS}} scripts/hooks/ .mcp.json .gitignore
git commit -m "[INIT] первичная инициализация проекта"

# Phase 2 — docs через project-docs/ junction
git add project-docs/
git commit -m "[DOCS] ARIA инициализация: CLAUDE, PATHS, STATE, SPEC, STACK, commands"
```

Если docs-repo отдельный (не junction) — Phase 2 выполняется в docs-repo:

```bash
cd "$DOCS_DIRECTORY"
[ ! -d .git ] && git init
git add .
git commit -m "[INIT] ARIA инициализация docs-репо"
```

---

## ШАГ 12: ВАЛИДАЦИЯ ПОСЛЕ ИНИЦИАЛИЗАЦИИ

Проверка что всё на месте:

### 12.1 Нет {{плейсхолдеров}} в итоговых файлах
```bash
grep -rE '\{\{[A-Z_]+\}\}' "$DOCS_DIRECTORY" --include="*.md" --include="*.yaml" && echo "FAIL: остались плейсхолдеры"
```

### 12.2 Правило разделения репо
```bash
FORBIDDEN=(.claude .dev CLAUDE.md STATE.yaml CHANGELOG.md ROADMAP.md SYS_CHANGELOG.md SPEC.md STACK.md REFERENCES.md PATHS.yaml project_config.yaml docs reports backlog)
# `.mcp.json` — разрешён (стандарт Claude Code)
# `project-docs/` — junction, не обычная директория
for p in "${FORBIDDEN[@]}"; do
  # Проверяем что это не внутри project-docs/ (junction — считается в docs-repo)
  if [ -e "$CODE_DIRECTORY/$p" ] && ! [ -L "$CODE_DIRECTORY/$p" ]; then
    if [ "$(readlink -f $CODE_DIRECTORY/$p 2>/dev/null)" != "$(readlink -f $DOCS_DIRECTORY/$p 2>/dev/null)" ]; then
      echo "VIOLATION: $CODE_DIRECTORY/$p"
      exit 1
    fi
  fi
done
```

### 12.3 `/aria-docs-audit`
Запустить аудит — не должно быть DEAD документов и нарушений.

### 12.4 YAML/JSON валидация
```bash
for f in STATE.yaml PATHS.yaml project_config.yaml adapter.yaml; do
  python -c "import yaml; yaml.safe_load(open('$DOCS_DIRECTORY/$f'))" 2>/dev/null \
    || ruby -e "require 'yaml'; YAML.load(File.read('$DOCS_DIRECTORY/$f'))" 2>/dev/null \
    && echo "OK: $f" || echo "FAIL: $f"
done

[ -f "$CODE_DIRECTORY/.mcp.json" ] && \
  python -c "import json; json.load(open('$CODE_DIRECTORY/.mcp.json'))" && echo "OK: .mcp.json"
```

---

## ШАГ 13: ОПЦИОНАЛЬНО — запись в FORKS.md upstream

Если пользователь указал `owner/repo` в ШАГ 1.7 и согласен на регистрацию:

### 13.1 Клонирование upstream
```bash
gh repo clone Yura1980Yura/ARIA /tmp/aria-upstream-for-fork-registration
cd /tmp/aria-upstream-for-fork-registration
```

### 13.2 Добавление строки в FORKS.md
Добавить строку в таблицу «Активные форки» FORKS.md:

```
| {PROJECT_NAME} | {STACK_SUMMARY} | {ADAPTER_NAME} | {OWNER/REPO} | {ARIA_VERSION} | {YYYY-MM-DD} |
```

### 13.3 PR
```bash
git checkout -b register-fork-{PROJECT_SLUG}
git add FORKS.md
git commit -m "[META] register new fork: {PROJECT_NAME}"
git push origin HEAD
gh pr create \
  --repo Yura1980Yura/ARIA \
  --title "[register fork] {PROJECT_NAME}" \
  --body "Новый форк использует ARIA. Регистрация в FORKS.md для автоуведомлений при /aria-release." \
  --label "contribute-back"
```

Upstream-maintainer примет через `/aria-triage --accept`.

---

## ШАГ 14: ФИНАЛЬНЫЙ ВЫВОД

```
✓ ARIA инициализирована для проекта {PROJECT_NAME}.

Создано:
  - $DOCS_DIRECTORY/CLAUDE.md, PATHS.yaml, project_config.yaml, STATE.yaml, STACK.md
  - $DOCS_DIRECTORY/.claude/commands/ (N команд)
  - $DOCS_DIRECTORY/docs/SPEC.md (генеральная спека продукта)
  - $DOCS_DIRECTORY/docs/{spec,ADR,research,policies}/
  - $CODE_DIRECTORY/.mcp.json ({N} MCP-серверов)
  - $CODE_DIRECTORY/scripts/hooks/
  - $CODE_DIRECTORY/.gitignore (с запретами ARIA-артефактов)
  - $CODE_DIRECTORY/project-docs/ (junction → $DOCS_DIRECTORY)

Validation:
  - YAML/JSON валидные: OK
  - Плейсхолдеры не остались: OK
  - Разделение репо: OK
  - /aria-docs-audit: PASS

Следующий шаг:
  cd $DOCS_DIRECTORY
  # Запусти первую задачу:
  /spec {first_task_name}
  # После спеки:
  /next-task
```

---

## Riski и их митигации

| Риск | Митигация |
|------|-----------|
| Junction `mklink /J` требует admin на Windows | Запросить у пользователя admin-права при FAIL; альтернатива — `mklink /D` или обработка путей вручную |
| MCP-серверы требуют `npm`, `docker`, etc. | В ШАГ 7 показать пользователю `required`-поля из adapter.yaml перед генерацией, пропустить необязательные |
| Пользователь попытается отредактировать SPEC.md вручную | Правило 1 живого документа (CLAUDE.md) + при `/done` проверяется, что SPEC.md не правился без триггера из DOCUMENTATION_LIFECYCLE.md |
| Шаблоны изменятся в upstream после /aria-init | Форк получит обновления через `/aria-sync` — при большом расхождении предложит rebase |
| SPEC.md получился слишком пустой (пользователь не знает ответов) | Разрешить placeholder-ы в опциональных секциях (7, 8). Обязательные (1-6) — требовать полных ответов или прекратить /aria-init |

---

## Замкнутый контур

- **Писатель:** `/aria-init` (этот файл — единственный писатель при первичной инициализации)
- **Читатели каждого создаваемого артефакта** — см. таблицу в CLAUDE.md.template «Структура документов»
- **Валидация после завершения:** ШАГ 12 (плейсхолдеры, разделение репо, YAML/JSON валидность, `/aria-docs-audit`)
