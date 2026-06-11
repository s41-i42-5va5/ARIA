Прочитай STATE.yaml. Найди задачу с минимальным priority где status=not_started и все depends_on=done.

**PATHS: Прочитай $ARIA_DOCS/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**МОДЕЛЬ: ВСЕ субагенты ОБЯЗАНЫ запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**

**АРХИТЕКТУРА: Оркестратор (1M контекст) кодит САМ. Code-субагент НЕ используется.**
**Субагенты: quality-gate, review (4 шт. + code reader), e2e-verifier, spec (research + code reader ×2 + атакер).**
**Тестирование трёхэтапное: свой модуль (оркестратор, -q) → полный прогон (Quality Gate) → E2E verify (субагент + скрипт).**

---

## ШАГ 0: ИНФРА-ПРОВЕРКА

### 0.1 Сервисы проекта
echo "ARIA upstream не требует runtime-инфры"
Если сервисы не запущены: echo "N/A"
Если Docker/runtime не запущен — сказать пользователю как запустить.

### 0.2 Правило разделения репозиториев (ARIA Phase 1, Task 11)
```bash
FORBIDDEN_PATHS=(.claude .dev CLAUDE.md STATE.yaml CHANGELOG.md ROADMAP.md SYS_CHANGELOG.md SPEC.md STACK.md REFERENCES.md project_config.yaml PATHS.yaml docs reports backlog)
for p in "${FORBIDDEN_PATHS[@]}"; do
  if [ -e "$ARIA_CODE/$p" ]; then
    echo "VIOLATION: $ARIA_CODE/$p — ARIA-инфраструктура в code-repo ЗАПРЕЩЕНА"
    echo "См. CLAUDE.md.template правило 4 «Разделение репозиториев»"
    exit 1
  fi
done
```
FAIL → СТОП, попросить пользователя удалить артефакты.

### 0.3 .mcp.json sync (WARNING, не блокер)
Если `.mcp.json` есть в обоих репо — md5 должны совпадать.

---

## ШАГ 1: ВЫБОР И ПОКАЗ ЗАДАЧИ

1. Прочитай spec задачи: {specs}/{task_name}.md
   Если spec НЕ существует → "Задача {task_name} не имеет spec. Запусти /spec сначала."
2. Покажи: имя, файлы, acceptance criteria, ключевые риски из spec
3. Спроси: "Начинаю {task}?"

После подтверждения:
   - STATE.yaml → task.status: in_progress, current.task: имя

---

## ШАГ 2: CODE (оркестратор выполняет САМ — используй 1M контекст)

### ПЕРВЫМ ДЕЛОМ — ИЗУЧИ ПРОЕКТ:

#### Документация:
1. spec/{task_name}.md содержит ВСЁ: research, архитектуру, риски, решения
2. Прочитай read_docs из YAML-заголовка spec (ADR, SPEC-FIX, research/*, DESIGN_GUIDE, REFERENCES)
3. Прочитай нужный раздел {global_spec}

#### Код (читай ГЛУБОКО — всю ветку механизма):
4. Прочитай КАЖДЫЙ файл из "files" задачи
5. Для каждого файла: прочитай ВСЕ imports и зависимости
6. Пройди по цепочке вызовов: кто вызывает → кого вызывает → кто зависит
7. Прочитай ВСЕ файлы механизма/алгоритма, который меняешь
8. Прочитай существующие тесты — пойми стиль и покрытие

Только после полного понимания — пиши код.

### ПРАВИЛА:
- Следуй CLAUDE.md протоколу
- Повторяй паттерны из существующего кода
- Используй подход из spec (секция Research → Рекомендуемый подход)
- Учитывай риски из spec (секция Риски)
- Пиши код + тесты для КАЖДОГО acceptance criterion
- Запрещено: MVP, костыли, hardcoded пути, TODO/HACK (см. CLAUDE.md)

---

## ШАГ 3: ТЕСТИРОВАНИЕ — ЭТАП 1 (оркестратор сам, только свой модуль)

```bash
bash "$ARIA_DOCS/scripts/hooks/tests/run_all.sh"
```
Если FAIL — исправь и перезапусти. Повторяй пока зелёные.

---

## ШАГ 4: ТЕСТИРОВАНИЕ — ЭТАП 2 (Quality Gate субагент, полный прогон)

Запусти Quality Gate (Task tool, subagent_type: "general-purpose", model: "opus"):

```
Ты — Quality Gate проекта ARIA. Прогони ВСЕ проверки, верни КРАТКУЮ сводку.

Последовательно:
1. bash -n "$ARIA_DOCS/scripts/hooks/"*.sh 2>&1 || echo "bash syntax OK"
2. echo "N/A для ARIA (нет типизированных языков)"
3. bash "$ARIA_DOCS/scripts/hooks/tests/run_all.sh"

Формат (ТОЛЬКО это):
LINT: PASS/FAIL (N ошибок)
TYPES: PASS/FAIL (N ошибок)
TESTS: PASS/FAIL (passed/total)
FAILED_TESTS (если есть):
- test_file::test_name — краткая причина
VERDICT: PASS | FAIL
```
Если FAIL — исправь, перезапусти. Если PASS — продолжай.

---

## ШАГ 5: РЕВЬЮ (Code Reader + 4 параллельных ревьюера)

Запусти /review.
Если NEEDS_FIX — примени fix snippets, вернись к шагу 3.
Если PASS — продолжай.

---

## ШАГ 6: E2E VERIFY (обязательно — НЕ пропускать. После ревью, ДО /done)

### 6.1 Проверь окружение
Git доступен, gh CLI авторизован (для release/triage тестов)

### 6.2 Субагент-верификатор (Task tool, model: "opus")
Промпт: docs/audit/post-phase-1-full-audit.md
spec_path = {specs}/{task_name}.md
Выход: ARIA_CODE/e2e-results/{task}_{timestamp}.yaml + ARIA_CODE/e2e-audit/*

### 6.3 Скрипт-валидатор
```bash
python "$ARIA_CODE/scripts/validate_e2e_results.py" \
  "$ARIA_CODE/e2e-results/{yaml}" \
  "{specs}/{task}.md" \
  "$ARIA_CODE/e2e-audit/"
```
GATE_PASS → продолжай.
GATE_FAIL → вернись к 6.2 (макс 3 цикла). 3x → blocker в STATE.yaml.

### 6.4 Миграция новых тестов
Перенеси новые E2E тесты из спеки → scripts/hooks/tests/e2e_registry.md + scripts/hooks/tests/e2e_registry.md

---

## ШАГ 7: Выведи результат и предложи /done

```
✓ {task_name} готова к коммиту.
  Quality Gate: PASS ({passed}/{total} тестов)
  Review: PASS (4/4 ревьюера)
  E2E Verify: GATE_PASS ({e2e_passed}/{e2e_total})
Запускай /done для Two-Phase Commit.
```
