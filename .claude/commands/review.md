Ревью текущей задачи. Запусти ПОСЛЕ написания кода, ДО /done.
Архитектура: общий Code Reader → 4 параллельных ревьюера (Verify + CodeReview + Adversarial Tester + E2E Integration).

**PATHS: Прочитай $ARIA_DOCS/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**МОДЕЛЬ: ВСЕ субагенты ОБЯЗАНЫ запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**

1. Прочитай STATE.yaml → current.task → файлы, acceptance criteria, spec.
   Прочитай STATE.yaml → текущая фаза → все done задачи фазы (для тестов сочетаний).

---

## ШАГ 0: ОБЩИЙ CODE READER (один раз для всех ревьюеров)

Запусти Code Reader (Task tool, subagent_type: "general-purpose", model: "opus"):

```
Ты — code reader проекта ARIA. Прочитай код задачи и создай контекст для ревьюеров.

Задача: {task_name}
Файлы реализации: {files}
Файлы тестов: {tests}

## ЧИТАЙ ГЛУБОКО:
1. Каждый файл из files и tests — ПОЛНОСТЬЮ
2. Все imports → зависимости
3. Кто ВЫЗЫВАЕТ эти модули (Grep)
4. API endpoints которые используют эти модули
5. UI компоненты если затрагивается API/модель
6. Конфиги, env vars, docker-compose если релевантно

## РЕЗУЛЬТАТ (~6-10K):

## Изменённый код
- Что изменилось/добавилось (краткое описание каждого файла)
- Ключевые функции/классы с сигнатурами

## Контракты и интерфейсы
- Public API (сигнатуры с типами)
- Кто вызывает → кого вызывает

## Зависимости
- Прямые и обратные
- Shared state

## API и UI
- Endpoints (метод, путь, handler)
- UI компоненты-потребители

## Тесты
- Какие тесты написаны, что покрывают
- Fixtures используемые
```

Сохрани результат Code Reader — передашь его всем 4 ревьюерам.

---

## ШАГ 1: 4 ревьюера ПАРАЛЛЕЛЬНО (запускай ВСЕ 4 в одном сообщении)

### Субагент 1: VERIFY (Task tool, subagent_type: "general-purpose", model: "opus")

```
Ты — верификатор проекта ARIA. Проверяешь build, тесты и spec compliance.

Задача: {task_name}
Acceptance criteria: {acceptance — КАЖДЫЙ пункт}

## КОНТЕКСТ КОДА (из Code Reader):
{вставь результат Code Reader}

## SPEC:
Прочитай {specs}/{task_name}.md — секция acceptance + юзкейсы
Прочитай {global_spec} — раздел задачи (если есть)

## BUILD:
1. bash -n "$ARIA_DOCS/scripts/hooks/"*.sh 2>&1 || echo "bash syntax OK"
2. echo "N/A для ARIA (нет типизированных языков)"
3. bash "$ARIA_DOCS/scripts/hooks/tests/run_all.sh"

## SPEC COMPLIANCE:
Для КАЖДОГО acceptance criterion:
1. Найди код реализации (из контекста)
2. Найди тест (из контекста)
3. Вердикт

## КАЧЕСТВО (быстро):
- Дублирование, размер функций/файлов, именование, type hints, magic numbers
## ТЕСТЫ (быстро):
- Edge cases, изолированность, конкретные ассерты
## БЕЗОПАСНОСТЬ (быстро):
- SQL injection, path traversal, secrets в коде, input validation

## Формат:
BUILD: LINT/TYPES/TESTS — PASS/FAIL
SPEC COMPLIANCE: таблица criterion → code → test → status
QUALITY/TESTS/SECURITY: PASS/FAIL (findings)
VERDICT: PASS | NEEDS_FIX (с code fix snippets)
```

### Субагент 2: CODE REVIEW (Task tool, subagent_type: "general-purpose", model: "opus")

```
Ты — независимый code reviewer проекта ARIA. Смотри критично.

Задача: {task_name}

## КОНТЕКСТ КОДА (из Code Reader):
{вставь результат Code Reader}

## КОНТЕКСТ РЕШЕНИЙ:
Прочитай {specs}/{task_name}.md — секции:
- «Отвергнутые альтернативы» — НЕ предлагай то что уже отвергнуто
- «Лог решений» — пойми почему выбран этот подход
- read_docs из YAML-заголовка → прочитай перечисленные ADR, SPEC-FIX, research/*

Прочитай STATE.yaml — depends_on текущей задачи

ПРАВИЛО: Перед предложением альтернативы — проверь spec/{task_name}.md.
Если альтернатива уже отвергнута — НЕ предлагай повторно.

## КАЧЕСТВО КОДА:
1. Структура: функции < 30 строк, файлы < 300 строк, нет дублирования
2. Качество: type hints, нет magic numbers, нет запрещённых паттернов
3. CRITICAL антипаттерны (из CLAUDE.md): MVP, костыли, тонкие решения, single-user код, нет обработки сбоев
4. Безопасность: инъекции, secrets, input validation
5. Тесты: edge cases, изолированность, конкретные ассерты

## АРХИТЕКТУРА:
1. Соответствие {global_spec}
2. Совместимость с существующими модулями
3. Совместимость с будущими задачами (depends_on)
4. Dead code
5. Стабильность контрактов

## Формат:
CODE QUALITY: findings (CRITICAL/WARN + КАК ИСПРАВИТЬ)
ARCHITECTURE: SPEC MATCH / COMPATIBILITY / FUTURE-PROOF / DEAD CODE
VERDICT: PASS | NEEDS_FIX
```

### Субагент 3: ADVERSARIAL TESTER (Task tool, subagent_type: "general-purpose", model: "opus")

```
Ты — adversarial tester проекта ARIA. СЛОМАЙ код.

Задача: {task_name}
Acceptance criteria: {acceptance}
Стек: Bash, Markdown, YAML; Git + gh CLI + Claude Code Opus 4.6

## КОНТЕКСТ КОДА (из Code Reader — только public API):
{вставь из Code Reader ТОЛЬКО секцию "Контракты и интерфейсы"}

## НЕ читай документацию. Только код и API.

## Экономия контекста:
- Из существующих тестов — только имена (grep "^test_")
- Каждый тест максимум 15 строк

## Правила:
- НЕ дублируй существующие тесты
- Пиши в НОВЫЙ файл: scripts/hooks/tests/test_{task_name}_adversarial.sh
- ЗАПУСТИ: bash scripts/hooks/tests/test_{task_name}_adversarial.sh

## Категории (по 1+ из каждой):
1. Boundary / Edge cases
2. Error injection
3. Concurrency / Race conditions
4. Stress (50-100 одновременных)
5. Recovery (повторный вызов после ошибки)

## Формат:
ТЕСТЫ: N написано, PASSED/FAILED
VERDICT: PASS | BUGS_FOUND (с КАК ИСПРАВИТЬ)
```

### Субагент 4: E2E INTEGRATION (Task tool, subagent_type: "general-purpose", model: "opus")

```
Ты — E2E integration tester проекта ARIA. Сверхсложные реалистичные сценарии.

Задача: {task_name}
Acceptance criteria: {acceptance}
Завершённые задачи фазы: {done_tasks_current_phase}
Все команды, шаблоны, адаптеры — см. SPEC.md
UI-изменения: нет (ARIA не имеет UI)

## КОНТЕКСТ КОДА (из Code Reader — краткий):
{вставь из Code Reader ТОЛЬКО секции "API и UI" + "Тесты"}

## Экономия: прочитай scripts/hooks/tests/run_all.sh + один существующий e2e (образец)

## ANTI-FLAKINESS:
- НЕ time.sleep() — polling/retry с timeout
- Каждый тест идемпотентный
- Не зависеть от порядка

## ТРЕБОВАНИЯ К СЦЕНАРИЯМ:
Покрытие основных команд: /spec /auto /done /review /aria-sync /aria-triage

## ЧАСТЬ 1: API/INTEGRATION E2E (10+ тестов)
Пиши: scripts/hooks/tests/e2e_{task_name}.sh
Категории: функциональные, сочетания, узкие места, нагрузочные, регрессионные

## ЧАСТЬ 2: UI/BROWSER E2E (3+ тестов, ТОЛЬКО если затронут UI)
Если нет UI → "UI E2E: SKIPPED"
Если есть: N/A (ARIA не имеет UI, smoke через bash-скрипты) (например: mcp__Claude_in_Chrome__*, mcp__MCP_DOCKER__browser_*)

## Формат:
API E2E: N тестов, топологии, PASSED/FAILED
UI E2E: N тестов или SKIPPED
VERDICT: PASS | BUGS_FOUND (с КАК ИСПРАВИТЬ)
```

---

## ШАГ 2: СВОДКА

```
+-------------------------+---------+
| Check                   | Result  |
+-------------------------+---------+
| Verify (build+spec)     | PASS/FAIL|
| Code Review (qual+arch) | PASS/FAIL|
| Adversarial Tester      | PASS/BUGS|
| E2E Integration         | PASS/BUGS|
+-------------------------+---------+
| OVERALL                 | PASS/FAIL|
+-------------------------+---------+
```

Если ЛЮБОЙ NEEDS_FIX/FAIL/BUGS_FOUND:
- Покажи findings с fix snippets
- Примени фиксы
- Запусти /review повторно

Если ВСЕ PASS:
- Adversarial и E2E тесты остаются в кодовой базе навсегда
- Выведи: Review passed (4/4). Можно /done
