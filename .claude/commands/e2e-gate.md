E2E Gate. Полная регрессия через N/A (ARIA не имеет UI, smoke через bash-скрипты). Запускается ПОСЛЕ ЗАВЕРШЕНИЯ ФАЗЫ.
Каждый файл библиотеки = отдельный субагент (последовательно — один браузер).

**PATHS: Прочитай $ARIA_DOCS/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**МОДЕЛЬ: ВСЕ субагенты ОБЯЗАНЫ запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**
**КРИТИЧЕСКОЕ ТРЕБОВАНИЕ**: ВСЕ E2E тесты — ТОЛЬКО через bash-скрипты (нет UI).

---

## ШАГ 1: ПОДГОТОВКА

1. Проверь окружение: Git доступен, gh CLI авторизован (для release/triage тестов)
2. Прочитай реестр: scripts/hooks/tests/e2e_registry.md
3. Определи список файлов библиотеки — все файлы в scripts/hooks/tests/e2e_registry.md и scripts/hooks/tests/e2e_registry.md

---

## ШАГ 2: ПРОГОН БИБЛИОТЕКИ (субагенты последовательно)

Для КАЖДОГО файла — отдельный субагент (Task tool, model: "opus"):

```
Ты — E2E верификатор ARIA. Прогони ВСЕ тесты из файла.

Прочитай правила: scripts/hooks/tests/e2e_rules.md
Прочитай тесты: {путь_к_файлу}

Выполни КАЖДЫЙ тест по описанным шагам. БУКВАЛЬНО.
Протокол audit trail: audit trail: timestamp, command, stdout/stderr, exit code.
Screenshot на каждой контрольной точке.

Запиши результат в YAML:
  ARIA_CODE/e2e-results/gate_{filename}_{timestamp}.yaml

Формат ответа:
ФАЙЛ: {имя}
ТЕСТОВ: {N}
PASS: {N}
FAIL: {N}
БАГИ: [список с воспроизведением]
```

Порядок прогона (smoke/API первым):
1. smoke_commands.md (если API/smoke сломан — E2E бессмысленны)
2. e2e_spec.md e2e_done.md e2e_review.md (P0)
3. e2e_aria_sync.md e2e_aria_triage.md e2e_aria_release.md (P1)

**Правило раннего выхода:** если smoke-файлы дали >50% FAIL — СТОП, чинить.

---

## ШАГ 3: ВАЛИДАЦИЯ КАЖДОГО YAML

Для каждого файла результатов:
```bash
python "$ARIA_CODE/scripts/validate_e2e_results.py" \
  "$ARIA_CODE/e2e-results/gate_{filename}.yaml" \
  "{тест_файл}" \
  "$ARIA_CODE/e2e-audit/"
```

Примечание: для gate валидатор проверяет полноту по файлу библиотеки (не по спеке задачи).

---

## ШАГ 4: CROSS-FEATURE СЦЕНАРИИ

Прочитай STATE.yaml → все задачи фазы с status=done.
Создай 3+ сценария комбинирующих функционал из РАЗНЫХ задач.

Пример: задача A = breakpoints, задача B = worker pool.
Cross-test: BP в workflow через worker + concurrent запуск второго + Replay Step.

---

## ШАГ 5: ОТЧЁТ

```
## E2E GATE REPORT — Phase {N}

### Окружение
- Сервисы: Git доступен, gh CLI отзывается, files все на месте
- Ошибки runtime: [нет / список]

### Библиотека ({N} файлов, {X}/{Y} тестов PASS)
| # | Файл | Тестов | PASS | FAIL | GATE |

### Cross-feature ({N}/{M})
| # | Сценарий | Результат |

### БАГИ
| # | Описание | Серьёзность | Тест ID | Воспроизведение |

### ВЕРДИКТ: GATE_PASS / GATE_FAIL
```

Если GATE_FAIL:
1. Исправь баги
2. Перезапусти /e2e-gate (макс 3 цикла)
3. После 3 циклов → blocker, фаза НЕ закрывается

Если GATE_PASS:
Фаза закрыта. Выведи итог.
