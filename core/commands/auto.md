Автоматический режим. Выполняй задачи непрерывно без остановки.

**PATHS: Прочитай ${{ENV_DOCS}}/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**МОДЕЛЬ: ВСЕ субагенты ОБЯЗАНЫ запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**

**АРХИТЕКТУРА: Оркестратор (1M контекст) кодит САМ. Code-субагент НЕ используется.**
**Субагенты: quality-gate, review (4 шт. + code reader), e2e-verifier, spec (research + code reader + атакер).**
**Тестирование трёхэтапное: свой модуль (оркестратор, -q) → полный прогон (Quality Gate) → E2E verify (субагент + скрипт).**

Цикл:

0. ИНФРА-ПРОВЕРКА (тихо, без вывода если всё ок):

   0.1 Сервисы проекта:
   {{INFRA_CHECK_COMMAND}}
   Если сервисы не запущены: {{INFRA_START_COMMAND}}
   Если Docker/runtime не запущен — сказать пользователю как запустить.

   0.2 ПРОВЕРКА РАЗДЕЛЕНИЯ РЕПОЗИТОРИЕВ (правило ARIA Phase 1, Task 11):

   Проверить что в ${{ENV_CODE}} НЕТ следующих ARIA-артефактов:
   - .claude/ (любой глубины — команды живут только в ${{ENV_DOCS}})
   - .dev/ (реликт старой rsync-модели — запрещён)
   - CLAUDE.md в корне (правила в ${{ENV_DOCS}}/CLAUDE.md)
   - STATE.yaml, CHANGELOG.md, ROADMAP.md, SYS_CHANGELOG.md в корне
   - SPEC.md, STACK.md, REFERENCES.md, project_config.yaml, PATHS.yaml в корне
   - docs/, reports/, backlog/ в корне (проектные документы в ${{ENV_DOCS}})

   Команда:
   ```bash
   FORBIDDEN_PATHS=(.claude .dev CLAUDE.md STATE.yaml CHANGELOG.md ROADMAP.md SYS_CHANGELOG.md SPEC.md STACK.md REFERENCES.md project_config.yaml PATHS.yaml docs reports backlog)
   for p in "${FORBIDDEN_PATHS[@]}"; do
     if [ -e "${{ENV_CODE}}/$p" ]; then
       echo "VIOLATION: ${{ENV_CODE}}/$p существует — ARIA-инфраструктура в code-repo ЗАПРЕЩЕНА"
       echo "См. правило разделения репозиториев (ARIA Phase 1, Task 11)"
       echo "Действие: удалить из code-repo. Всё ARIA-хозяйство живёт ТОЛЬКО в ${{ENV_DOCS}}"
       exit 1
     fi
   done
   ```

   Если найдено хотя бы одно нарушение — СТОП. Не продолжать работу.
   Пользователь должен удалить артефакты из code-repo перед следующим запуском.

   0.3 ПРОВЕРКА .mcp.json (стандарт Claude Code — разрешён в обоих репо если контент идентичен):
   Если ${{ENV_CODE}}/.mcp.json существует — сверить с ${{ENV_DOCS}}/.mcp.json (md5sum).
   Расхождение — предупреждение пользователю (не блокер).

1. Прочитай STATE.yaml → найди следующую задачу (минимальный priority, status=not_started, depends_on=done)

2. Прочитай spec задачи: {specs}/{task_name}.md
   Если spec НЕ существует → СТОП: "Задача {task_name} не имеет spec. Запусти /spec сначала."
   Пойми: acceptance criteria, файлы, юзкейсы, рекомендуемый подход, риски, E2E Testing Plan.

3. CODE (оркестратор выполняет САМ — используй 1M контекст):

   ## ПЕРВЫМ ДЕЛОМ — ИЗУЧИ ПРОЕКТ:

   ### Документация:
   1. spec/{task_name}.md содержит ВСЁ: research, архитектуру, риски, решения
   2. Прочитай read_docs из YAML-заголовка spec (ADR, SPEC-FIX, DESIGN_GUIDE)
   3. Прочитай нужный раздел {global_spec}

   ### Код (читай ГЛУБОКО):
   4. Прочитай КАЖДЫЙ файл из "files" задачи
   5. Для каждого файла: прочитай ВСЕ imports и зависимости
   6. Пройди по цепочке вызовов: кто вызывает → кого вызывает → кто зависит
   7. Прочитай ВСЕ файлы механизма/алгоритма, который меняешь
   8. Прочитай существующие тесты — пойми стиль и покрытие

   Только после полного понимания — пиши код.

   ## ПРАВИЛА:
   - Следуй CLAUDE.md протоколу
   - Повторяй паттерны из существующего кода
   - Используй подход из spec (секция Research → Рекомендуемый подход)
   - Учитывай риски из spec (секция Риски)
   - Пиши код + тесты для КАЖДОГО acceptance criterion
   - Запрещено: {{FORBIDDEN_PATTERNS_COMPACT}} (см. CLAUDE.md)

4. ТЕСТИРОВАНИЕ — ЭТАП 1 (оркестратор сам, только свой модуль):

   ```bash
   {{TEST_MODULE_COMMAND}}
   ```
   Если FAIL — исправь и перезапусти. Повторяй пока зелёные.

5. ТЕСТИРОВАНИЕ — ЭТАП 2 (Quality Gate субагент, полный прогон):

   Запусти Quality Gate (Task tool, subagent_type: "general-purpose", model: "opus"):
   ```
   Ты — Quality Gate проекта {{PROJECT_NAME}}. Прогони ВСЕ проверки, верни КРАТКУЮ сводку.

   Последовательно:
   1. {{LINT_COMMAND}}
   2. {{TYPECHECK_COMMAND}}
   3. {{TEST_COMMAND}}

   Формат (ТОЛЬКО это):
   LINT: PASS/FAIL (N ошибок)
   TYPES: PASS/FAIL (N ошибок)
   TESTS: PASS/FAIL (passed/total)
   FAILED_TESTS (если есть):
   - test_file::test_name — краткая причина
   VERDICT: PASS | FAIL
   ```
   Если FAIL — исправь, перезапусти. Если PASS — продолжай.

6. РЕВЬЮ (Code Reader + 4 параллельных ревьюера):
   Запусти /review.
   Если NEEDS_FIX — примени fix snippets, вернись к шагу 4.
   Если PASS — продолжай.

7. E2E VERIFY (обязательно — НЕ пропускать. После ревью кода, ДО коммита):
   7.1 Проверь окружение: {{E2E_SETUP_CHECKLIST}}
   7.2 Субагент-верификатор (Task tool, model: "opus"):
       Промпт: {{E2E_VERIFIER_PROMPT_REF}}
       spec_path = {specs}/{task_name}.md
       Выход: {{ENV_CODE}}/e2e-results/{task}_{timestamp}.yaml + {{ENV_CODE}}/e2e-audit/*
   7.3 Скрипт-валидатор:
       ```bash
       python "${{ENV_CODE}}/scripts/validate_e2e_results.py" \
         "${{ENV_CODE}}/e2e-results/{yaml}" \
         "{specs}/{task}.md" \
         "${{ENV_CODE}}/e2e-audit/"
       ```
       GATE_PASS → продолжай. GATE_FAIL → вернись к 7.2 (макс 3 цикла). 3x → blocker.
   7.4 Мигрируй новые тесты: spec → {{E2E_FRONTEND_REGISTRY}} + {{E2E_REGISTRY}}

8. /done → коммит, push, STATE update.

9. Выведи: {task} done ({SHA}). Перехожу к следующей.

10. /clear — очистка контекста перед следующей задачей.

11. Перейди к шагу 1.

Не спрашивай подтверждения. Не останавливайся между задачами.
Максимум 3 попытки ревью на задачу — если не проходит, остановись и сообщи.

Когда все задачи текущей фазы status: done → запусти /e2e-gate (полная регрессия фазы).
Если /e2e-gate FAIL — исправь, перезапусти (макс 3 цикла). Если PASS — фаза закрыта.

В конце выведи итог: список завершённых задач с SHA коммитов + E2E Gate результат.
