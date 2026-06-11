# ПРОМПТ: Полный аудит ARIA после реализации Phase 1

> **Запускать в НОВОМ чате** после того, как все 15 задач Phase 1 закрыты.
> Это не часть конвейера `/auto` — это **разовый приёмочный аудит** новой системы.

---

## Твоя роль

Ты — независимый аудитор ARIA. Твоя задача — **объективно** проверить, что система после Phase 1 **не потеряла ни одной функциональной возможности** относительно базы — форка ARIA форков — и **только приобрела** новые возможности.

**Принципы работы:**
- **Факты, не мнения.** Анализируй содержимое файлов, а не декларации.
- **Без домыслов.** Если не видишь функцию в коде — значит её нет, независимо от того, что написано в документации.
- **Критическое мышление.** Не соглашайся с утверждениями спеки, пока сам не проверил в файлах.
- **Честный отчёт.** Если найдёшь регрессию — фиксируй как регрессию, не оправдывай «это стало лучше».
- **Только Opus 4.6** для всех субагентов.

---

## Входные данные

### Источник истины форков (эталон, обкатано на 37+ задачах)

**Важно о структуре форков.** форков имеет два репозитория по ARIA-модели:
- **docs-репо:** `$FORK_DOCS` = `W2_Logic Converter/` (в Yandex.Disk/Obsidian) — источник истины для команд, спек, STATE, CHANGELOG, ROADMAP, project_config. `.claude/commands/` находится **в корне этого репо** — так было с самого начала (подтверждено архивом `W2/arhive/backup/commands_v0_original/` от 2026-04-06).
- **code-репо:** `{fork-code-repo}/` (GitHub) — только код приложения + runtime `.claude/commands/auto.md` + зеркало docs в `.dev/`. Зеркало синхронизируется rsync-ом из `auto.md` шаг 7 — при этом `.claude/` ИСКЛЮЧАЕТСЯ из rsync (команды не копируются в code-репо, кроме runtime auto.md).

**НЕ путать:** команды в code-репо `{fork-code-repo}/.claude/commands/` — только 1 файл `auto.md`. Полный набор — в docs-репо `W2/.claude/commands/` (9 файлов).

| Путь | Содержимое | Роль |
|------|------------|------|
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/.claude/commands/*.md` | **9 команд source-of-truth:** aria-sync, auto, done, e2e-gate, next-task, research, review, spec, status | docs-репо, корневая .claude/ |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/docs/` | Проектная документация продукта: ADR/, research/, spec/, SPEC.md, REFERENCES.md, DESIGN_GUIDE.md, EVOLUTION_SINGLE_TO_SAAS.md | docs-репо, проектные документы |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/STATE.yaml` | STATE с расширенным контрактом задачи (files/tests/acceptance/commit) | docs-репо, корень |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/CHANGELOG.md` | Табличный CHANGELOG | docs-репо, корень |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/ROADMAP.md` | ROADMAP с галочками и SHA | docs-репо, корень |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/CLAUDE.md` | Протокол решения проблем | docs-репо, корень |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/project_config.yaml` | Секция `commands: {test, lint, typecheck, dev_server}` | docs-репо, корень |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/PATHS.yaml` | Пути | docs-репо, корень |
| `{fork-code-repo}/.claude/commands/auto.md` | Runtime /auto (копия из docs-репо, единственная команда в code-репо) | code-репо, runtime |
| `{fork-code-repo}/.dev/` | Зеркало docs-репо (автосинхрон через rsync в auto.md:7, исключая .claude/) | code-репо, read-only зеркало |
| `{fork-code-repo}/.mcp.json` | MCP-серверы для runtime | code-репо |
| `{fork-code-repo}/CLAUDE.md` | CLAUDE-правила для runtime (копия из docs-репо) | code-репо |

### Актуальное состояние новой ARIA

| Путь | Содержимое |
|------|------------|
| `C:/Users/user/Desktop/Projects/ARIA/` | Код-репо upstream ARIA |
| `C:/Users/user/Desktop/Projects/ARIA/core/` | CLAUDE.md.template, commands/, protocols/, templates/ |
| `C:/Users/user/Desktop/Projects/ARIA/adapters/` | python-fastapi/, kotlin-android/, csharp-avalonia/ |
| `C:/Users/user/Desktop/Projects/ARIA/scripts/` | hooks/ + validate_spec_e2e.sh + validate_e2e_results.sh (после Task 15) |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W4_ARIA/` | Docs-репо upstream ARIA |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W4_ARIA/CLAUDE.md` | Актуальная CLAUDE |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W4_ARIA/STATE.yaml` | Текущее состояние |
| `C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W4_ARIA/docs/spec/phase-1-stabilization.md` | Реализованная спека v2.3 |

---

## Структура аудита

Выполни **7 этапов последовательно**, не пропуская ни одного. Каждый этап заканчивается вердиктом `PASS` / `FAIL` с конкретными находками.

---

## ЭТАП 1: Сравнение команд ARIA vs форков

Для **каждой** команды из `$FORK_DOCS/.claude/commands/*.md` (9 команд) прочитай **обе версии полностью** — форков-оригинал и ARIA-upstream (`$ARIA_CODE/core/commands/*.md`).

Заполни таблицу для каждой команды:

```
### Команда: {name}

**форков:** {path}, {N строк}
**ARIA upstream:** {path}, {M строк}

**Структура форков (шаги):**
1. ...
2. ...
...

**Структура ARIA (шаги):**
1. ...
2. ...
...

**Функции форков, присутствующие в ARIA:** [список]
**Функции форков, ОТСУТСТВУЮЩИЕ в ARIA:** [список — каждая находка = регрессия]
**Функции ARIA, отсутствующие в форков (добавленные):** [список — только если они реально дают ценность, а не заменяют потерянное]

**Вердикт:** PASS / FAIL
**Регрессии:** {перечислить с номерами R?}
```

**Критерий PASS:** ВСЕ функции форков присутствуют в ARIA (возможно через плейсхолдеры адаптера). Допустимо: универсализация через плейсхолдеры. Недопустимо: потеря шагов, субагентов, валидационных скриптов.

**Обязательные проверки по командам:**

| Команда | Что ДОЛЖНО быть в ARIA |
|---------|------------------------|
| `spec.md` | ШАГ 2 — 3 параллельных субагента (Research детальный с gh/context7/fetch/WebSearch, Code Reader Архитектурный, Code Reader Деструктивный). ШАГ 3 — обязательная E2E Testing Plan (TCM с 7 подсекциями, 2 контура тестов, GDQ 10 вопросов, метрики, антипаттерны задачи). ШАГ 4 — Атакер с 23 векторами + PRE-MORTEM. ШАГ 5 — `validate_spec_e2e.sh` + удаление промежуточных файлов (_draft/_attack/_research). |
| `review.md` | Общий Code Reader + 4 детальных ревьюера: VERIFY (build+spec compliance) / CODE REVIEW (качество+архитектура) / **ADVERSARIAL TESTER** (пишет НОВЫЕ тесты по 5 категориям и запускает) / **E2E INTEGRATION** (10+ API тестов + 3+ browser через `{{E2E_BROWSER_TOOLS}}`). |
| `next-task.md` | 9 шагов: инфра-проверка → show spec → confirm → code → test этап 1 → test этап 2 Quality Gate → /review → **ШАГ 8 E2E VERIFY** с субагентом-верификатором + `validate_e2e_results.sh` + артефактами `e2e-results/*.yaml` и `e2e-audit/*`. |
| `auto.md` | STATE → spec → код оркестратор → тесты → Quality Gate → /review → /done. |
| `done.md` | Two-Phase Commit + STATE update + CHANGELOG update (гибридный формат scope+таблица). |
| `research.md` | gh CLI + context7 + WebSearch + WebFetch + fetch MCP. Пишет в `docs/research/{topic}.md` И в `docs/REFERENCES.md` при нахождении конкретного алгоритма. |
| `status.md` | Краткий отчёт текущего состояния (phase, task, blockers). |
| `aria-sync.md` | Pull / Dry-run / Contribute-back с auto-labeling, читает/пишет SYS_CHANGELOG.md. |
| `e2e-gate.md` | Либо существует, либо функционал поглощён `/next-task` ШАГ 8. |

---

## ЭТАП 2: Сравнение форматов документов

Для **каждого** документа в таблице ниже — сравни формат в форков (фактический, в файле) и в ARIA-upstream (фактический + согласно шаблонам в `core/templates/`).

| Документ | форков | ARIA |
|----------|-----------|------|
| STATE.yaml | files/tests/acceptance/commit по задачам | `core/templates/STATE.yaml.template` |
| CHANGELOG.md | табличный: `\| дата \| task \| КОМПОНЕНТ \| SHA \| файлы \| тесты \|` | гибридный (scope-секции + табличные строки внутри) |
| REFERENCES.md | `## Компонент — Подтема / Источник / Взяли / Изменили / Антиреференс` | `docs/REFERENCES.md` после Task 13 |
| SPEC.md | 10 частей + Implementation Guide с чек-листами | `core/templates/SPEC.md.template` + `docs/SPEC.md` upstream |
| STACK.md | нет отдельного файла (стек в CLAUDE.md) | 12-секционный + контур |
| project_config.yaml | секция `commands: {test, lint, typecheck, dev_server}` | `core/templates/project_config.yaml.template` |
| ROADMAP.md | чекбоксы + дата + SHA | `ROADMAP.md` upstream (автоген из SPEC) |

**Для каждого:**

```
### Документ: {name}

**Формат форков:** {краткое описание, пример строки}
**Формат ARIA:** {краткое описание, пример строки}

**Поля форков, присутствующие в ARIA:** [список]
**Поля форков, ОТСУТСТВУЮЩИЕ в ARIA:** [список]
**Поля ARIA, отсутствующие в форков:** [список]

**Вердикт:** PASS / FAIL
**Регрессии:** {R1-R6}
```

**Критерий PASS:** Все поля форков присутствуют в ARIA-шаблонах или ARIA-документах.

---

## ЭТАП 3: Проверка замкнутости контуров

Для **каждого** документа в системе (сгенерируй список через `ls $ARIA_CODE/core/ $ARIA_DOCS/docs/ $ARIA_DOCS/*.yaml $ARIA_DOCS/*.md` и аналогичные) определи:

| Поле | Что проверить |
|------|---------------|
| Писатель | Какая команда/hook/субагент создаёт/обновляет этот файл? Проверить по `grep` в `core/commands/` |
| Читатель 1 | Какая команда/hook/субагент читает этот файл в рамках workflow? |
| Читатель 2 | Дополнительные читатели |
| Класс | operational / external-audience / policy / reference |
| Вердикт | LIVE / PARTIAL / DEAD |

**Формат отчёта:**

```
| Документ | Писатель | Читатель | Класс | Вердикт |
|----------|----------|----------|-------|---------|
| STATE.yaml | /done, /spec | каждая сессия + /auto шаг 1 | operational | LIVE |
| ROADMAP.md | /roadmap-sync, /done | pre-commit validator | operational (исключение) | LIVE |
| SYS_CHANGELOG.md | /aria-sync | /aria-sync (следующий запуск) | operational | LIVE |
| README.md | /done при триггере | GitHub-посетители | external-audience | LIVE |
| ... |
```

**Критерий PASS:** 
- Ни одного DEAD документа.
- Все PARTIAL имеют объяснение (какой читатель/писатель неявный).
- Все LIVE подтверждены фактическим кодом команд (grep в core/commands/).

**Обязательные проверки протечек (из v2.2):**
- [ ] `research/*.md` — читается `/spec` ШАГ 0 (агрегирует список) + ШАГ 4 Аналитик + ШАГ 5 Атакер по релевантности
- [ ] `SYS_CHANGELOG.md` — явный контракт чтения в `/aria-sync` и `/aria-sync --contribute`
- [ ] Policies (CHANGELOG_POLICY, DOCUMENTATION_LIFECYCLE, CLAUDE.md) — правило «только через чат с AI»
- [ ] ROADMAP.md — pre-commit валидатор признан читателем
- [ ] README.md — класс внешней аудитории с триггерами обновления

---

## ЭТАП 4: Проверка закрытия регрессий R1-R12

Для **каждой** регрессии проверь в коде ARIA:

### R1: STATE.yaml расширенный контракт
```bash
# Проверить файл
cat $ARIA_CODE/core/templates/STATE.yaml.template
# Должны быть поля по задаче: files, tests, acceptance, commit, sessions
```
**PASS** если все 5 полей присутствуют.

### R2: CHANGELOG гибридный формат
```bash
cat $ARIA_DOCS/docs/policies/CHANGELOG_POLICY.md | grep -A 20 "Формат"
```
**PASS** если описан scope-секции + табличные строки задач внутри.

### R3: REFERENCES формат форков
```bash
cat $ARIA_CODE/core/commands/spec.md | grep -A 20 "REFERENCES"
# Ожидается: Источник / Взяли / Изменили / Антиреференс
```
**PASS** если формат форков документирован в spec.md.

### R4: REFERENCES группировка по компоненту
**PASS** если в spec.md написано «группировка по компоненту, не по дате».

### R5: Трассируемость коммит→задача
```bash
# Проверить что в CHANGELOG есть поля SHA + files + tests в табличной форме
```
**PASS** если есть.

### R6: project_config.commands
```bash
cat $ARIA_CODE/core/templates/project_config.yaml.template | grep -A 10 "commands:"
```
**PASS** если секция commands с 4+ полями.

### R7-R12: команды upstream
Выполни сравнение строк:
```bash
wc -l "C:/Users/user/Yandex.Disk/Obsidian/Obsidian Yandex Disk/W2_Logic Converter/.claude/commands/spec.md"
wc -l $ARIA_CODE/core/commands/spec.md
# ARIA-версия должна быть ≥ форков-версии
```

**R7:** в ARIA spec.md должны быть явные команды gh CLI, context7, WebSearch, WebFetch, fetch MCP с примерами.
**R8:** в ARIA spec.md должны быть 2 субагента Code Reader (Архитектурный + Деструктивный) с разными промптами.
**R9:** в ARIA spec.md Атакер должен иметь 23 вектора (проверить числом).
**R10:** в ARIA spec.md секция `## E2E Testing Plan` с TCM, 7 подсекциями, GDQ.
**R11:** в ARIA review.md 4 ревьюера, включая Adversarial Tester (пишет тесты) и E2E Integration.
**R12:** в ARIA next-task.md ШАГ 8 E2E VERIFY с validate_e2e_results.

---

## ЭТАП 5: Проверка двунаправленной актуализации (upstream ↔ форк)

### 5.1 Upstream → Форк (обновления)

Проверь в коде:
- `/aria-release` ШАГ 7 читает `FORKS.md` (НЕ CONTRIBUTIONS.md) — `grep -n "FORKS.md" core/commands/aria-release.md`
- `/aria-release` использует `gh issue create --repo {fork_repo}` для уведомления
- `FORKS.md` содержит поле Repo с валидными `owner/repo` значениями
- `/aria-sync` в форке читает `fork_sync_playbook.md` upstream
- `/aria-sync` определяет статусы (NEW / UPSTREAM_AHEAD / FORK_AHEAD / BOTH_DIVERGED / SYNCED / INTENTIONALLY_CUSTOM)
- `/aria-sync` обновляет `SYS_CHANGELOG.md` и `project_config.yaml aria.version`

### 5.2 Форк → Upstream (contribute-back)

Проверь:
- `/aria-sync --contribute` создаёт PR с auto-labels `contribute-back` + `scope:*`
- `.github/PULL_REQUEST_TEMPLATE.md` существует и содержит поля: Fork / Adapter / ARIA version / Что / Почему ценно / Файлы / Обкатано
- `/aria-triage` собирает PR через `gh pr list --label contribute-back`
- `/aria-triage` регистрирует в `TRIAGE.md`, пишет отчёт с анализом scope и конфликтов
- `/aria-triage --accept` делает merge + пишет строку в CHANGELOG с атрибуцией `from {fork}, PR #{N}`
- `/aria-triage --decline` + `/aria-triage --discuss` реализованы

### 5.3 Отсутствие разрывов

Проверь граничные случаи из спеки v2.2 (secция «Реальные слабости механизма»):
- [ ] Протокол rebase при версионной несовместимости (форк с ARIA 3.1 отправляет PR в ARIA 3.4) — прописан ли?
- [ ] Агрегация атрибуции в CHANGELOG при PR с N коммитами — одна строка или N строк?
- [ ] Обработка `BOTH_DIVERGED` — нет автоматического мерджа, показ пользователю?

Эти случаи могут быть в спеке помечены как «добавим когда проявится». Если их нет — проверить что это не создаёт немедленной регрессии.

**Критерий PASS:** Все 3 подэтапа имеют конкретные команды/файлы, подтверждающие работоспособность.

---

## ЭТАП 6: Функциональное тестирование — smoke-сценарии

### Сценарий 1: новый форк через /aria-init

```
1. Создать временный каталог /tmp/aria-smoke-fork
2. Запустить /aria-init с тестовыми данными (проект-заглушка)
3. Проверить что создано:
   - CLAUDE.md (без {{плейсхолдеров}})
   - PATHS.yaml
   - project_config.yaml с секцией commands:
   - STATE.yaml с расширенным контрактом
   - docs/SPEC.md (заполнен интерактивно)
   - docs/STACK.md (12 секций)
   - .claude/commands/ (9 команд после Task 15)
   - scripts/hooks/ (если адаптер предполагает)
   - .mcp.json (или пропущен при пустом mcp_servers)
4. Проверить: нет {{плейсхолдеров}} в выходных файлах (grep -r "{{" /tmp/aria-smoke-fork)
```

### Сценарий 2: /spec создаёт полноценную спеку

```
1. В тестовом форке запустить /spec {test_task}
2. Проверить что создано spec/{test_task}.md с секциями:
   - YAML-заголовок (task, read_docs, read_code)
   - Цель / Архитектура / AC / Файлы / Риски / Тесты
   - E2E Testing Plan (TCM / 7 подсекций / 2 контура / GDQ / метрики)
   - Лог решений + Отвергнутые альтернативы
3. Проверить что промежуточные файлы _draft/_attack/_research УДАЛЕНЫ
4. Запустить validate_spec_e2e.sh — должен дать SPEC_E2E_PASS
5. Проверить что REFERENCES.md пополнился (если Research нашёл конкретный алгоритм)
```

### Сценарий 3: /review находит проблему

```
1. В тестовом форке написать код с НАМЕРЕННОЙ ошибкой (пустой catch, magic number)
2. Запустить /review
3. Проверить что все 4 ревьюера отработали
4. Проверить что Adversarial Tester написал новый test-файл и запустил pytest
5. Проверить что findings содержат CRITICAL/WARN с fix snippets
```

### Сценарий 4: /aria-triage — пакетная обработка

```
1. Создать тестовый PR в upstream с label contribute-back
2. Запустить /aria-triage
3. Проверить что PR появился в TRIAGE.md с status: pending
4. Запустить /aria-triage --accept T-001
5. Проверить: PR смерджен, TRIAGE.md → архив, CHANGELOG пополнился строкой с from {fork}, PR #N
```

**Критерий PASS:** Все 4 сценария отрабатывают без ошибок, артефакты создаются корректно.

---

## ЭТАП 7: Гарантия не-регрессии для форков

**Критический тест:** взять **реальную задачу** из истории форков STATE.yaml (например, `step_replay` — 4df1b54) и прогнать её через новые ARIA-команды в dry-run режиме.

```
1. Скопировать spec этой задачи (или раздел из SPEC.md) как вход для /spec
2. Запустить /spec {step_replay}
3. Сравнить: покрывает ли новая спека все элементы, которые были в оригинале?
   - Research с 3 реф-проектами (Temporal, n8n, Airflow)
   - Антиреференс (n8n while-loop со стеком — делаем DAG+topo)
   - Адверсариал-ревью
   - E2E Testing Plan с TCM
4. Если чего-то не хватает — это регрессия для форков.
```

---

## Выходной артефакт аудита

Сохрани полный отчёт в `$ARIA_DOCS/docs/audit/post-phase-1-audit-{YYYY-MM-DD}.md` со структурой:

```markdown
# Пост-Phase-1 аудит ARIA — {дата}

## Резюме
- Этапов пройдено: N/7
- Регрессий найдено: N (список R1-R12 + новых)
- Dead-документов: N
- Протечек контуров: N
- Функциональных сценариев PASS: N/4
- **Общий вердикт: PASS / FAIL**

## Этап 1: Команды
{таблица по всем 9 командам}

## Этап 2: Форматы документов
{таблица по 7 документам}

## Этап 3: Контуры
{полная таблица документов}

## Этап 4: Регрессии R1-R12
{проверка каждой}

## Этап 5: Двунаправленная актуализация
{upstream→форк, форк→upstream, граничные случаи}

## Этап 6: Smoke-сценарии
{4 сценария с логами}

## Этап 7: форков не-регрессия
{сравнение на реальной задаче}

## Критические находки
{если есть — список с PRIORITY: CRITICAL / WARN}

## Рекомендации
{что доработать в Phase 1 или перенести в Phase 2, если появится}
```

---

## Критерий финального PASS аудита

Аудит считается PASS **только если ВСЕ 7 этапов имеют вердикт PASS И:**
1. Ни одна регрессия R1-R12 не открыта заново
2. Нет мёртвых документов
3. Нет протечек контуров без явного обоснования
4. Все 4 smoke-сценария отработали
5. На реальной задаче форков новая ARIA показывает ≥ функциональности оригинальной

**FAIL:** любая регрессия относительно форков считается блокером выхода из Phase 1.

---

## Правила проведения аудита

1. **Читай файлы целиком,** не полагайся на `grep`-сниппеты для ключевых утверждений.
2. **Сравнивай строки,** а не пересказы. «spec.md в ARIA должен иметь ≥400 строк» — это факт, проверяемый `wc -l`.
3. **Не доверяй спеке Phase 1 v2.3 слепо.** Если в спеке написано «сделано», но в коде нет — это FAIL, а не «оформим потом».
4. **Критическое мышление.** Если видишь «универсализация через плейсхолдеры» — проверь что плейсхолдеры действительно резолвятся в adapter.yaml всех 3 адаптеров.
5. **Честность.** Не оправдывай регрессии «так стало лучше». Регрессия — это регрессия.
6. **Параллельные субагенты** для ЭТАПА 1 (9 команд) и ЭТАПА 3 (документы) — ускорить работу. Все с `model: "opus"`.
7. **Отчёт — на русском** (по CLAUDE.md).

---

## Запуск

```
Претендуй на роль аудитора. Прочитай этот файл целиком. Начни с ЭТАПА 1. 
Не переходи к следующему этапу пока текущий не PASS или FAIL зафиксирован.
В конце — сохрани отчёт по формату выше и выведи резюме.
```
