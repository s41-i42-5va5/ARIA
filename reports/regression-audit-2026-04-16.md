# Регрессионный аудит ARIA v3.4 + тест синхронизации fork-центр-fork

**Дата:** 2026-04-16
**Методика:** прямое чтение ВСЕХ файлов команд, hooks, validate-скриптов, CLAUDE.md, PATHS.yaml, STATE.yaml из трёх источников
**Инструменты:** 3 параллельных субагента (Opus 4.6) + прямое чтение ключевых файлов

## Источники

| Компонент | Путь | Команд | Строк (ключевые) |
|-----------|------|--------|-------------------|
| **форков docs** | `W2_Logic Converter` | 9 | spec:408, review:242, auto:156, e2e-gate:117, next-task:98, research:95 |
| **форков code** | `Desktop/Projects/форков` | 0 | validate_spec_e2e.py:373, validate_e2e_results.py:432, post-commit:2 |
| **ARIA upstream** | `W4_ARIA` | 15 | spec:433, review:239, aria-sync:197, aria-init:481, next-task:152, aria-release:151 |
| **форков docs** | `{fork-docs}` | 12 | aria-sync:197(synced), aria-docs-audit:165(synced), e2e-gate:105(broken), spec:87 |
| **форков code** | `{fork-code-repo}` | 0 | validate_spec_e2e.sh:158, validate_e2e_results.sh:113 |

---

# БЛОК 1: РЕГРЕССИОННЫЙ АУДИТ форков → ARIA → форков

## 1. Идея → Спека (`/spec`)

| Аспект | форков (408 строк) | ARIA upstream (433 строки) | форков (87 строк) | Вердикт |
|--------|----------------------|--------------------------|-------------------|---------|
| **Субагенты** | 4: Research, Code Reader Арх., Code Reader Деструкт., Атакер | 4: Research, Code Reader Арх., Code Reader Деструкт., Атакер | 3: Research, Code Reader x2, Атакер (краткое) | ARIA=форков. **форков деградация** |
| **ШАГ 0 (Ядро)** | Читает SPEC.md, STATE.yaml, available_docs, available_research | Идентично форков | Читает SPEC.md, STATE.yaml — **нет available_research** | форков: потеря research/ discovery |
| **ШАГ 1 (Уточнение)** | 5 категорий вопросов, включая доп.документы | Идентично форков | 4 категории, **нет секции "Дополнительные документы"** | форков: потеря read_docs discovery |
| **Research субагент** | Полный: GitHub, context7, WebSearch, WebFetch, fetch MCP, browser | Идентично форков (адаптирован стек) | Краткий: GitHub, WebSearch, WebFetch, datasheet'ы | **форков: потеря context7, MCP fetch, browser tools** |
| **Code Readers** | 2 параллельных: Архитектурный + Деструктивный (7+6 пунктов) | Идентично форков | 2 кратких: "существующий код" + "референсный код" — **без детальных инструкций** | **форков CRITICAL: потеря depth** |
| **Spec формат** | YAML header (read_docs, read_code), 12 секций, E2E Testing Plan (TCM+GDQ) | Идентично форков | YAML header, 7 секций — **нет E2E Testing Plan, TCM, GDQ** | **форков CRITICAL: нет E2E plan** |
| **Атакер** | 23 вектора, E2E Testing Plan review (8 векторов), Pre-mortem | Идентично форков (23 вектора) | 5 вопросов (краткие) — **нет формализованных векторов** | **форков CRITICAL: 5 vs 23** |
| **Финализация** | Машинная проверка validate_spec_e2e + финальный файл + удаление промежуточных | Идентично (bash вместо python) | Нет машинной проверки, нет удаления промежуточных | **форков CRITICAL: нет validate** |
| **Фиксация** | STATE.yaml, SPEC.md, REFERENCES.md, ADR | Идентично + `/adr-new` | STATE.yaml update (кратко) — **нет REFERENCES.md, нет ADR** | форков: потеря REFERENCES |

### Конкретные потери форков `/spec`

1. **E2E Testing Plan** полностью отсутствует (TCM 7 подсекций, GDQ 10 вопросов, привязка AC->тесты, антипаттерны, метрики)
2. **Машинная валидация** `validate_spec_e2e.sh` не вызывается
3. **23 атакующих вектора** редуцированы до 5 вопросов
4. **REFERENCES.md** не обновляется
5. **Research depth** — потеряны context7, MCP fetch, browser tools
6. **Code Reader depth** — потеряны детальные инструкции (imports, обратные зависимости, race conditions, сериализация)

---

## 2. Спека → Код (`/auto` и `/next-task`)

| Аспект | форков auto(156) + next-task(98) | ARIA auto(38) + next-task(152) | форков auto(35) + next-task(33) | Вердикт |
|--------|---------------------------------------|----------------------------------|----------------------------------|---------|
| **auto.md** | 156 строк: полный цикл с INFRA, code, 3-этапные тесты, QG, review, e2e | 38 строк: компактный цикл (7 шагов) | 35 строк: компактный цикл (7 шагов) | **ARIA перенесла тяжесть в next-task (152)** |
| **next-task.md** | 98 строк: выбор задачи, code, verify тесты | 152 строки: 7 шагов (INFRA, выбор, code, 2 этапа тестов, review, E2E verify) | 33 строки: выбор + code + тесты (кратко) | **форков CRITICAL: 33 vs 152** |
| **ИНФРА-проверка** | Docker/Redis/PostgreSQL startup | echo "не требует runtime-инфры" + правило разделения репо + .mcp.json sync | Нет | ARIA: правило разделения, **форков: потеря** |
| **Quality Gate** | Субагент: lint+types+tests | Субагент: bash -n + tests | Нет QG субагента | **форков: потеря Quality Gate** |
| **E2E Verify** | Субагент + validate_e2e_results.py | Субагент + validate_e2e_results.sh + 3 цикла retry | Нет E2E verify | **форков CRITICAL: потеря E2E verify** |
| **Code depth** | read_docs из YAML header, read_code, imports, chain вызовов | Идентично форков | read_docs, read_code (краткое) — **нет chain вызовов** | форков: уменьшенная глубина |

### Конкретные потери форков `/next-task`

1. **Quality Gate субагент** отсутствует
2. **E2E Verify** (субагент + скрипт валидатор + 3 цикла retry) отсутствует
3. **ИНФРА-проверка** (правило разделения репо) отсутствует
4. **Code reading depth** (8 пунктов -> 3)

---

## 3. Код → Ревью (`/review`)

| Аспект | форков (242 строки) | ARIA upstream (239 строк) | форков (50 строк) | Вердикт |
|--------|------------------------|--------------------------|-------------------|---------|
| **Архитектура** | Code Reader + 4 ревьюера (Verify, CodeReview, Adversarial, E2E Integration) | Идентично форков | Code Reader + 4 ревьюера (краткие) | Структура сохранена |
| **Code Reader** | 55 строк: 6 пунктов + результат 6-10K | Идентично форков | Одна строка: "Прочитай все файлы" | **форков CRITICAL: потеря depth** |
| **Verify** | Build (lint+types+tests) + Spec Compliance таблица + Quality/Tests/Security | Build + Spec Compliance + Quality/Tests/Security | "Соответствие spec, AC, edge cases" — **3 строки** | **форков CRITICAL** |
| **CodeReview** | Контекст решений (отвергнутые альтернативы), 5 пунктов качества, 5 пунктов архитектуры | Идентично форков | Не существует как отдельный ревьюер | **форков: CodeReview merged into others** |
| **Adversarial Tester** | Пишет тесты, 5 категорий, запускает | Пишет bash тесты, 5 категорий, запускает | Нет adversarial тестов | **форков CRITICAL: нет adversarial** |
| **E2E Integration** | 10+ API тестов + Browser E2E, топологии | 10+ bash тестов | Нет E2E integration | **форков CRITICAL: нет E2E** |
| **4 ревьюера** | Verify, CodeReview, Adversarial, E2E | Verify, CodeReview, Adversarial, E2E | Корректность, Safety, Performance, Совместимость | форков: **заменены 4 ревьюера** — предметно-ориентированные |
| **Adversarial тесты** | Сохраняются в кодобазе навсегда | Сохраняются в кодобазе навсегда | Нет adversarial | **форков: потеря живых артефактов** |

### Оценка форков `/review`

форков сознательно заменил 4 ревьюера форков/ARIA на предметные (Safety, Performance, Совместимость) — это **обоснованная кастомизация** для hardware-проекта. Но:
- **Потеря Adversarial Tester** — CRITICAL, нет стресс-тестов и boundary cases
- **Потеря E2E Integration** — CRITICAL, нет интеграционных тестов
- **Code Reader depth** — 1 строка vs 55 строк инструкций

---

## 4. Ревью → Завершение (`/done`)

| Аспект | форков (73 строки) | ARIA upstream (86 строк) | форков (55 строк) | Вердикт |
|--------|----------------------|--------------------------|-------------------|---------|
| **Two-Phase Commit** | Phase 1 (code) -> Phase 2 (docs via project-docs/) -> Phase 3 (push) | Идентично + sessions в STATE + ROADMAP.md + DOCUMENTATION_LIFECYCLE triggers | Phase 1 + Phase 2 + Phase 3 (кратко) | форков: сохранен Two-Phase |
| **STATE.yaml update** | status->done, commit, current->next | + completed date, sessions log | status->done, current->null | форков: **нет sessions log, нет completed** |
| **CHANGELOG format** | Строка с $COMMIT_ID | Гибридный формат (scope-таблицы) + SYS_CHANGELOG | Маркдаун блок (нет $COMMIT_ID привязки к scope) | **форков: упрощенный CHANGELOG** |
| **Verification** | grep hash в CHANGELOG и STATE | Идентично форков | Нет верификации | **форков: потеря post-commit verify** |
| **Recovery** | Таблица FAIL->Recovery | Идентично форков | Нет recovery | **форков: потеря recovery протокола** |
| **ROADMAP.md** | [ ] -> [x] | Через `/roadmap-sync` | Нет ROADMAP update | форков: потеря ROADMAP sync |

---

## 5. E2E Gate (`/e2e-gate`)

| Аспект | форков (117 строк) | ARIA upstream (104 строки) | форков (105 строк) | Вердикт |
|--------|----------------------|--------------------------|-------------------|---------|
| **Содержание** | Browser E2E (MCP Chrome + MCP Docker) | Bash-скрипты (нет UI) | **Нераскрытые плейсхолдеры** | **форков CRITICAL** |
| **Плейсхолдеры** | Раскрыты | Раскрыты (N/A, bash) | `{{E2E_BROWSER_TOOLS}}`, `{{ENV_DOCS}}`, `{{E2E_EXECUTION_CHANNEL}}`, и ещё 8+ | **форков CRITICAL: не работает** |

**форков e2e-gate.md содержит 10+ нераскрытых `{{плейсхолдеров}}`** — файл скопирован из шаблона без подстановки. Команда `/e2e-gate` в форков **полностью нефункциональна**.

---

## 6. Инструменты

| Команда | форков | ARIA | форков | Вердикт |
|---------|-----------|------|-------|---------|
| `/research` | 95 строк: полный (GitHub, context7, MCP, browser, REFERENCES.md) | 122 строки: идентичный + контуры чтения | 33 строки: краткий (GitHub, WebSearch, нет context7/MCP) | **форков: деградация** |
| `/status` | 6 строк | 10 строк | 10 строк (synced) | OK |
| `/adr-new` | нет | 81 строка | 81 строка (synced) | OK (NEW в ARIA) |
| `/roadmap-sync` | нет | 117 строк | 117 строк (synced) | OK (NEW в ARIA) |
| `/aria-sync` | 89 строк | 197 строк (+ SYS_CHANGELOG контракт, contribute-back, auto-labeling) | 197 строк (synced, но `${{ENV_DOCS}}` плейсхолдер) | **форков: `${{ENV_DOCS}}` нераскрыт** |
| `/aria-docs-audit` | нет | 165 строк | 165 строк (synced) | OK (NEW в ARIA) |

---

## 7. Дополнительные проверки

### Мертвые документы
1. **форков `e2e-gate.md`** — мертвый (нераскрытые плейсхолдеры, никогда не запустится)
2. **форков `aria-sync.md`** — содержит `${{ENV_DOCS}}` (двойной ${{ — bash не раскроет)

### Разорванные контуры
1. **форков: spec -> validate** — `/spec` форков не вызывает `validate_spec_e2e.sh`, хотя скрипт есть в `scripts/`
2. **форков: next-task -> e2e-verify** — `/next-task` форков не имеет шага E2E Verify
3. **форков: review -> adversarial tests** — `/review` форков не создает adversarial тесты (разрыв контура "тесты остаются навсегда")

### Validate скрипты: форков (.py) vs ARIA (.sh)

| Скрипт | форков (Python) | ARIA (Bash) | Эквивалентность |
|--------|-------------------|-------------|-----------------|
| validate_spec_e2e | 373 строки Python, 13 проверок | 158 строк Bash, 13 проверок | **Эквивалентны** — одни и те же 13 пунктов |
| validate_e2e_results | 432 строки Python, 10 проверок (PyYAML, JSON) | 204 строки Bash, 10 проверок (grep-based) | **Функционально эквивалентны**, bash менее точен (regex vs parsed YAML) |

### Hooks

| Hook | форков | ARIA | форков | Вердикт |
|------|-----------|------|-------|---------|
| pre-commit | В `.git/hooks/` | `scripts/hooks/pre-commit` (форматирование коммитов) | В `scripts/hooks/` через adapter | OK |
| post-commit | auto-push (`git push`) | Нет | Нет | форков-специфика |
| pre-push | Нет | `scripts/hooks/pre-push` | Нет | ARIA-only |

---

# БЛОК 2: ТЕСТ ДВУСТОРОННЕЙ СИНХРОНИЗАЦИИ fork-центр-fork

## 2A: Инкрементальное обогащение ARIA от нескольких форков

### Участники (гипотетические, кроме форков)

| Форк | Стек | Адаптер | Уникальное изменение |
|------|------|---------|---------------------|
| форков | Kotlin/Android | kotlin-android | GPS failover protocol |
| MediTrack | Python/FastAPI | python-fastapi | API rate-limiter antipattern |
| DroneNav | Kotlin/Android | kotlin-android | Battery-aware routing protocol |
| EduPlatform | Python/FastAPI | python-fastapi | Pagination pattern для API |
| SmartFarm | C#/Avalonia | csharp-avalonia | Offline-first sync protocol |
| RetailPOS | Kotlin/Android | kotlin-android | Receipt printer adapter pattern |

### Пошаговый анализ

| Шаг | Команда | Читает | Пишет | Статус | Комментарий |
|-----|---------|--------|-------|--------|-------------|
| 1. Каждый форк: `/aria-sync --contribute {тема}` | `aria-sync.md` contribute | `SYS_CHANGELOG.md` (baseline), fork files vs last sync | PR в upstream (via `gh pr create --label contribute-back`), SYS_CHANGELOG.md append | **PASS** | Протокол полный: метаданные происхождения (P-013), auto-labeling (Task 7) |
| 2. 6 PR приходят в ARIA | GitHub PR queue | — | — | **PASS** | PR template `.github/PULL_REQUEST_TEMPLATE.md` стандартизирует body |
| 3. Maintainer: `/aria-triage` | `aria-triage.md` | `gh pr list --label contribute-back`, `TRIAGE.md`, `FORKS.md` | `TRIAGE.md` (T-NNN записи), субагенты-аналитики | **PASS** | Каждый PR -> параллельный субагент анализ |
| 4. Конфликты (2 форка -> один файл) | Triage субагент | `gh pr diff` обоих PR | Отчет конфликтов | **WARN** | Конфликт описывается словесно, но **нет автоматического merge-разрешения** |
| 5. `--accept T-001 T-003 T-005` | `aria-triage.md` accept | `TRIAGE.md`, PR info | PR merge (squash), CHANGELOG.md (атрибуция), TRIAGE.md (архив), FORKS.md update | **PASS** | Атрибуция `from {FORK}, PR #{N}` в CHANGELOG — полная |
| 6. `--decline T-002` | `aria-triage.md` decline | TRIAGE.md | PR comment + close, TRIAGE.md (архив) | **PASS** | Комментарий с причиной + рекомендации |
| 7. `--discuss T-004 T-006` | `aria-triage.md` discuss | TRIAGE.md | PR comment, label `status:discussing` | **PASS** | |

### Проблемы 2A

1. **WARN: Автоматический merge конфликтов** — если DroneNav и RetailPOS оба изменили `adapter.yaml` для `kotlin-android`, triage субагент **опишет** конфликт, но **не разрешит** его автоматически. Maintainer должен вручную решить порядок merge. Это **by design**, но масштабируется плохо при 10+ форках.
2. **WARN: Порядок --accept** — если T-001 и T-003 конфликтуют, `--squash` для T-003 может дать конфликт после T-001. Протокол **не описывает** как обработать ошибку merge.

---

## 2B: Обратная актуализация — ARIA обновляет все форки

| Шаг | Команда | Читает | Пишет | Статус | Комментарий |
|-----|---------|--------|-------|--------|-------------|
| 1. `/aria-release v3.5` | `aria-release.md` | CHANGELOG.md (v3.5-dev -> v3.5), все коммиты since last tag | CHANGELOG.md update, git tag, push | **PASS** | 6 шагов, валидация pre-release, интерактивная доработка |
| 2. Уведомление форков | `aria-release.md` ШАГ 6 | `FORKS.md` (Активные форки с Repo) | `gh issue create --repo` в каждом форке | **PASS** | Опционально (можно пропустить) |
| 3. Каждый форк: `/aria-sync` (pull) | `aria-sync.md` pull | Upstream clone, `fork_sync_playbook.md`, `project_config.yaml`, `SYS_CHANGELOG.md` | Обновленные артефакты, SYS_CHANGELOG.md append | **PASS** | Полный 6-фазный протокол |
| 4. "Мое vs чужое" | `fork_sync_playbook.md` P-009 | CHANGELOG upstream (атрибуция `from {fork}`) | Отчет: `[upstream]`, `[fork-adapted]`, `[fork-originated -> upstream]` | **PASS** | Атрибуция P-015 "includes contribution from {other_fork}" |
| 5. Отклоненные форки | — | — | — | **PASS** | Получают ВСЕ принятые артефакты. Отклоненный PR не влияет на pull |

### Проблемы 2B

1. **WARN: Конфликт adapter.yaml** — если DroneNav->`adapter.yaml` (GPS) и RetailPOS->`adapter.yaml` (printer) оба приняты, форков получает merged `adapter.yaml` с обоими изменениями. Playbook пометит как `UPSTREAM_AHEAD` (clean apply). Но если форков сам менял `adapter.yaml` — будет `BOTH_DIVERGED`, потребуется ручной merge. **Это корректно по протоколу.**
2. **WARN: Плейсхолдеры при pull** — playbook R11 требует раскрытия `{{ENV_CODE}}` и др. при копировании команд. Но форков e2e-gate.md уже содержит нераскрытые плейсхолдеры — значит **предыдущий sync не выполнил R11**. Это **баг в реализации**, не в протоколе.

---

## 2C: Петля обратной связи

| Шаг | Команда | Читает | Пишет | Статус | Комментарий |
|-----|---------|--------|-------|--------|-------------|
| 1. форков: `/aria-sync --contribute gps_failover_v2` | `aria-sync.md` contribute | SYS_CHANGELOG.md (baseline), fork files | PR в upstream с метаданными (P-013) | **PASS** | Метаданные: "Обкатано в проекте: N задач" |
| 2. `/aria-triage` на PR от форков | `aria-triage.md` | PR diff, CHANGELOG.md (наличие `gps_failover` от DroneNav) | Отчет: "улучшение уже принятого артефакта" | **WARN** | Triage субагент **увидит** что артефакт уже есть в upstream, но **нет специального протокола** для "улучшение vs замена" |
| 3. `--accept` | `aria-triage.md` | — | CHANGELOG.md: `from форков, PR #{N}` — **накопительная атрибуция** | **PASS** | |
| 4. DroneNav: `/aria-sync` | `aria-sync.md` pull | Upstream v3.6 | `gps_failover_v2` from форков, отчет: `[upstream, includes contribution from форков]` | **PASS** | P-015 атрибуция цепочки |

### Проблемы 2C

1. **WARN: "Улучшение vs замена"** — если форков предлагает `gps_failover_v2`, а в upstream уже есть `gps_failover` от DroneNav, triage субагент должен решить: patch vs replace. Протокол **не формализует** это различие — аналитик решает по контексту. Для MVP это нормально, но при масштабировании нужен `--update` режим в triage.

---

# СВОДНАЯ ТАБЛИЦА ПРОБЛЕМ

| # | Severity | Источник | Проблема | Где | Рекомендация |
|---|----------|---------|----------|-----|--------------|
| **1** | **CRITICAL** | форков | `/e2e-gate.md` содержит 10+ нераскрытых `{{плейсхолдеров}}` — команда нефункциональна | `{fork-docs}/.claude/commands/e2e-gate.md` | Раскрыть плейсхолдеры из `project_config.yaml` или скопировать из ARIA с подстановкой |
| **2** | **CRITICAL** | форков | `/aria-sync.md` содержит `${{ENV_DOCS}}` — bash не раскроет двойной `${{` | `{fork-docs}/.claude/commands/aria-sync.md` строка 5 | Заменить `${{ENV_DOCS}}` -> `$форков_DOCS` |
| **3** | **CRITICAL** | форков | `/spec` потеряла E2E Testing Plan (TCM, GDQ, привязка AC->тесты, метрики) — 50% спеки | `{fork-docs}/.claude/commands/spec.md` | Добавить E2E Testing Plan секцию из ARIA upstream |
| **4** | **CRITICAL** | форков | `/spec` атакер: 5 вопросов вместо 23 формализованных векторов — quality gate ослаблен на 78% | `{fork-docs}/.claude/commands/spec.md` | Добавить 23 вектора из ARIA upstream |
| **5** | **CRITICAL** | форков | `/spec` не вызывает `validate_spec_e2e.sh` — машинная валидация не работает при наличии скрипта | `{fork-docs}/.claude/commands/spec.md` | Добавить ШАГ финализации с вызовом validate |
| **6** | **CRITICAL** | форков | `/next-task` не имеет Quality Gate субагента и E2E Verify — код уходит в production без 2 из 3 этапов проверки | `{fork-docs}/.claude/commands/next-task.md` | Добавить шаги 4-6 из ARIA upstream |
| **7** | **CRITICAL** | форков | `/review` не создает Adversarial тесты и E2E Integration тесты — разрыв контура "тесты остаются навсегда" | `{fork-docs}/.claude/commands/review.md` | Добавить adversarial + E2E субагентов |
| **8** | **HIGH** | форков | `/done` нет post-commit verification (grep hash в CHANGELOG/STATE) — нет гарантии консистентности | `{fork-docs}/.claude/commands/done.md` | Добавить Phase 3 verification из ARIA |
| **9** | **HIGH** | форков | `/done` нет recovery протокола — при FAIL commit остается невосстановленным | `{fork-docs}/.claude/commands/done.md` | Добавить Recovery таблицу из ARIA |
| **10** | **HIGH** | форков | `/research` потерял context7 MCP, browser tools — исследования ограничены базовым поиском | `{fork-docs}/.claude/commands/research.md` | Добавить инструменты из ARIA upstream |
| **11** | **HIGH** | форков | `/spec` Code Reader без детальных инструкций (imports, обратные зависимости, race conditions) | `{fork-docs}/.claude/commands/spec.md` | Скопировать Code Reader инструкции из ARIA |
| **12** | **HIGH** | ARIA | `/auto` сжат с 156 до 38 строк. `/auto` НЕ вызывает `/next-task` — выполняет свой укороченный цикл. Потери: CODE depth (4 пункта vs 8 — нет imports/chain/тестов), Quality Gate без промпта субагента (1 строка vs 15), E2E Verify полностью отсутствует, нет /clear между задачами (загрязнение контекста), нет правила 3 попыток (может зациклиться), нет /e2e-gate при завершении фазы (фаза не закрывается) | ARIA `auto.md` | Восстановить `/auto` до полного конвейера форков: 8 пунктов CODE, промпт Quality Gate, E2E Verify (4 подшага), /clear, 3 попытки, /e2e-gate |
| **13** | **HIGH** | ARIA | `validate_e2e_results.sh` (bash, regex) функционально НЕэквивалентен форков `.py` (PyYAML, JSON). Потери: нет JSON parsing action logs (проверка #6 ACTIONS пробита), нет проверки монотонности timestamps (фабрикация не обнаружится), нет индивидуальных метрик из спеки (min_action_count, min_duration_sec), regex `grep -cE '^\s+status:\s+pass'` может поймать комментарий как валидный тест | `scripts/validate_e2e_results.sh` | Портировать точно: добавить `jq` для JSON parsing, `sort -C` для монотонности timestamps, парсинг индивидуальных метрик. Либо оставить Python-версию как зависимость |
| **14** | **WARN — ИСПРАВЛЕНО** | Sync 2A | Нет обработки ошибки merge при конфликте 2+ PR | `aria-triage.md` | Добавлен ШАГ 2.1: обработка конфликта (не force-merge, статус `blocked`, продолжение пакетной обработки) |
| **15** | **WARN — ИСПРАВЛЕНО** | Sync 2C | Нет протокола "улучшение vs замена" для уже принятого артефакта | `aria-triage.md` | Добавлен ШАГ 4 п.6 (проверка перекрытия: ENHANCEMENT/REPLACEMENT/INDEPENDENT) + цепочная атрибуция в CHANGELOG |

---

# ВЕРДИКТ

## форков -> ARIA upstream: 2 HIGH РЕГРЕССИИ

ARIA v3.4 сохранила функциональность форков в `/next-task`, `/spec`, `/review`, `/done`, `/e2e-gate`, `/research`:
- Все 23 атакующих вектора на месте
- E2E Testing Plan сохранен
- Two-Phase Commit сохранен (обязателен как контракт для форков)
- Контуры замкнуты
- **Добавлены** 5 новых команд (aria-init, aria-release, aria-triage, adr-new, roadmap-sync)

**Регрессия #12: `/auto` деградировал** с 156 до 38 строк. `/auto` НЕ делегирует в `/next-task` — выполняет свой урезанный цикл. При запуске `/auto` теряются: CODE depth (4 vs 8 пунктов), промпт Quality Gate, E2E Verify, /clear, 3 попытки, /e2e-gate. Пользователь, запустивший `/auto` вместо `/next-task`, получает **ослабленный пайплайн**.

**Регрессия #13: `validate_e2e_results.sh` функционально НЕэквивалентен** форков `.py`. Bash-версия потеряла: JSON parsing action logs, проверку монотонности timestamps, индивидуальные метрики из спеки. Regex-подход уязвим к false positives (комментарий `# status: pass` пройдёт как валидный тест).

## ARIA upstream -> форков: 7 CRITICAL РЕГРЕССИЙ

форков `intentionally_custom` кастомизация **чрезмерно редуцировала** 7 команд. Объяснимо (hardware-проект, другой стек), но:
- **E2E Testing Plan** полностью отсутствует в spec
- **Quality Gate + E2E Verify** отсутствуют в пайплайне
- **e2e-gate.md** нефункционален (плейсхолдеры)
- **Adversarial тесты** не создаются

## Синхронизация: МЕХАНИЗМ РАБОТАЕТ

Протокол fork<->upstream через aria-sync / aria-triage / aria-release — **замкнут и функционален**:
- Contribute-back (fork -> upstream): PASS
- Pull (upstream -> fork): PASS
- Атрибуция цепочки (fork A -> upstream -> fork B): PASS
- Выявлены 2 WARN (конфликты при множественных PR, улучшение vs замена) — допустимы для текущего масштаба

## Итоговая матрица

| Переход | Регрессии | Расширения |
|---------|-----------|------------|
| **форков -> ARIA** | **2 HIGH — ИСПРАВЛЕНЫ 2026-04-16** | +5 команд, SYS_CHANGELOG контракт |
| **ARIA -> форков** | **7 CRITICAL, 4 HIGH** | +4 предметных ревьюера (Safety, Performance, Совместимость) |

## Исправления 2026-04-16

### Fix #12: `/auto` восстановлен до полного конвейера

`.claude/commands/auto.md`: 38 строк → 157 строк. Восстановлены все шаги по эталону форков:
- 0.1 Сервисы + 0.2 Разделение репозиториев (bash-скрипт) + 0.3 .mcp.json sync
- CODE: 8 пунктов глубокого чтения (imports, chain вызовов, тесты, "только после понимания")
- Quality Gate: полный промпт субагента (lint, types, tests, формат вывода)
- E2E Verify: 4 подшага (окружение, субагент, валидатор, миграция тестов)
- Two-Phase Commit (Phase 1-2-3)
- /clear между задачами
- Правило 3 попыток ревью
- /e2e-gate при завершении фазы
- Итог в конце

### Fix #13: Python-валидатор восстановлен

- Скопирован `validate_e2e_results.py` (432 строки, 10 проверок) из форков в `scripts/validate_e2e_results.py` — точная копия
- `.sh` сохранён как fallback
- Все команды (`auto.md`, `next-task.md`, `e2e-gate.md`) + шаблоны (`core/commands/`) переведены на вызов `.py`
- Обновлены: `fork_sync_playbook.md`, `aria-init.md`

Затронутые файлы:
- `.claude/commands/auto.md` — полная перезапись
- `.claude/commands/next-task.md` — `.sh` → `.py`
- `.claude/commands/e2e-gate.md` — `.sh` → `.py`
- `.claude/commands/aria-init.md` — добавлен `.py` в список
- `core/commands/next-task.md` — `.sh` → `.py`
- `core/commands/e2e-gate.md` — `.sh` → `.py`
- `core/commands/auto.md` — `.sh` → `.py`
- `core/protocols/fork_sync_playbook.md` — добавлен `.py` в список скриптов
- `scripts/validate_e2e_results.py` — NEW (точная копия форков)

### Fix #14: Обработка конфликтов merge в `--accept`

`.claude/commands/aria-triage.md`: добавлен ШАГ 2.1 после merge:
- Если `gh pr merge` вернул ошибку — НЕ force-merge
- Вывести конфликтные файлы, предложить rebase или ручной resolve
- Перевести T-ID в статус `blocked` (не declined)
- Продолжить с остальными `--accept` из списка

### Fix #15: Протокол "улучшение vs замена"

`.claude/commands/aria-triage.md`: добавлено:
- ШАГ 4 п.6 в промпте субагента: проверка перекрытия с уже принятыми артефактами (ENHANCEMENT / REPLACEMENT / INDEPENDENT)
- Поле `Перекрытие` в выходном формате субагента
- Цепочная атрибуция в CHANGELOG: `from {FORK2}, PR #{N2} (ENHANCEMENT of T-{NNN} from {FORK1})`

Затронутый файл:
- `.claude/commands/aria-triage.md` — 3 вставки (~25 строк)
