#!/usr/bin/env bash
# validate_spec_e2e.sh — 13-point validation of spec's E2E Testing Plan
# Usage: bash validate_spec_e2e.sh <spec_path>
# Exit codes: 0 = SPEC_E2E_PASS, 1 = SPEC_E2E_FAIL

set -u

SPEC="${1:-}"
if [ -z "$SPEC" ] || [ ! -f "$SPEC" ]; then
  echo "Usage: $0 <spec_path>"
  echo "spec file not found: $SPEC"
  exit 1
fi

FAILS=0
CHECKS=0

check() {
  local name="$1"
  local result="$2"
  CHECKS=$((CHECKS + 1))
  if [ "$result" = "PASS" ]; then
    echo "  [✓] $name"
  else
    echo "  [✗] $name"
    FAILS=$((FAILS + 1))
  fi
}

echo "Validating E2E Testing Plan in: $SPEC"
echo ""

# 1. Секция "## E2E Testing Plan" существует
if grep -qE '^## E2E Testing Plan' "$SPEC"; then
  check "1. Секция '## E2E Testing Plan' существует" PASS
else
  check "1. Секция '## E2E Testing Plan' существует" FAIL
fi

# 2. Затронутые механики заполнены (минимум 1 строка «файл → механика»)
if awk '/^### Затронутые механики/,/^###/{print}' "$SPEC" | grep -qE '^\|[^|]+\|[^|]+\|'; then
  check "2. Затронутые механики заполнены (≥1 файл→механика)" PASS
else
  check "2. Затронутые механики заполнены (≥1 файл→механика)" FAIL
fi

# Извлечь E2E секцию в отдельный файл для точных проверок внутри секции
E2E_SECTION=$(mktemp)
awk '/^## E2E Testing Plan/,/^## [^#]/' "$SPEC" > "$E2E_SECTION"
trap "rm -f $E2E_SECTION" EXIT

# 3. Есть ID тестов (E2E- или API-) внутри E2E секции
if grep -qE '(E2E-|API-)[A-Z0-9_-]+' "$E2E_SECTION"; then
  check "3. Есть ID тестов (E2E-/API-) в E2E секции" PASS
else
  check "3. Есть ID тестов (E2E-/API-) в E2E секции" FAIL
fi

# 4. Smoke-тест включён (настраивается через ENV: SMOKE_TEST_ID, default E2E-EXEC-001)
SMOKE_ID="${SMOKE_TEST_ID:-E2E-EXEC-001}"
if grep -qF "$SMOKE_ID" "$SPEC"; then
  check "4. Обязательный smoke-тест $SMOKE_ID включён" PASS
else
  check "4. Обязательный smoke-тест $SMOKE_ID включён" FAIL
fi

# 5. AC привязаны к тестам (секция «Привязка AC → Тесты» существует и имеет строки таблицы)
if awk '/^### Привязка AC/,/^###/{print}' "$SPEC" | grep -qE '^\|[^|]+\|[^|]+\|[^|]+\|'; then
  check "5. AC привязаны к тестам (таблица Привязка AC → Тесты)" PASS
else
  check "5. AC привязаны к тестам (таблица Привязка AC → Тесты)" FAIL
fi

# 6. Антипаттерны задачи ≥ 2
ANTIPAT_COUNT=$(awk '/^### Антипаттерны задачи/,/^###/{print}' "$SPEC" | grep -cE '^[-*]\s')
if [ "$ANTIPAT_COUNT" -ge 2 ]; then
  check "6. Антипаттерны задачи ≥ 2 (найдено: $ANTIPAT_COUNT)" PASS
else
  check "6. Антипаттерны задачи ≥ 2 (найдено: $ANTIPAT_COUNT)" FAIL
fi

# 7. Метрики заполнены числами (внутри E2E секции)
if grep -qE 'min_new_tests:\s*[0-9]+' "$E2E_SECTION" \
   && grep -qE 'min_regression_tests:\s*[0-9]+' "$E2E_SECTION" \
   && grep -qE 'min_total_checks:\s*[0-9]+' "$E2E_SECTION"; then
  check "7. Метрики заполнены числами (min_new_tests, min_regression_tests, min_total_checks)" PASS
else
  check "7. Метрики заполнены числами" FAIL
fi

# 8. TCM существует (внутри E2E секции)
if grep -qE '^### (Test Coverage Matrix|TCM)' "$E2E_SECTION"; then
  check "8. TCM существует в E2E секции" PASS
else
  check "8. TCM существует в E2E секции" FAIL
fi

# 9. TCM полная — все 7 подсекций (поиск ТОЛЬКО внутри E2E секции через regex)
# Извлечь TCM секцию из E2E секции
TCM_SECTION=$(awk '/^### (Test Coverage Matrix|TCM)/,/^### [^T]/' "$E2E_SECTION")
SUBSECTIONS=("Функционал" "Риски" "Регрессии" "Контракты" "Маршруты данных" "Зависимости" "Окружение")
MISSING=()
for s in "${SUBSECTIONS[@]}"; do
  if ! echo "$TCM_SECTION" | grep -qE "####\s*[0-9]*\.?\s*$s"; then
    MISSING+=("$s")
  fi
done
if [ "${#MISSING[@]}" -eq 0 ]; then
  check "9. TCM содержит все 7 подсекций (в E2E секции)" PASS
else
  check "9. TCM содержит все 7 подсекций в E2E секции (отсутствуют: ${MISSING[*]})" FAIL
fi

# 10. Каждая строка TCM имеет "Тест?" = да (поиск ТОЛЬКО внутри TCM секции E2E)
TCM_LINES_WITH_YES=$(echo "$TCM_SECTION" | grep -cE '\|\s*да\s*\|' || echo 0)
TCM_LINES_WITH_NO=$(echo "$TCM_SECTION" | grep -cE '\|\s*нет\s*\|' || echo 0)
if [ "$TCM_LINES_WITH_YES" -ge 3 ] && [ "$TCM_LINES_WITH_NO" -eq 0 ]; then
  check "10. TCM строки имеют Тест?=да (да:$TCM_LINES_WITH_YES, нет:$TCM_LINES_WITH_NO)" PASS
else
  check "10. TCM строки имеют Тест?=да (да:$TCM_LINES_WITH_YES, нет:$TCM_LINES_WITH_NO)" FAIL
fi

# 11. GDQ секция существует (внутри E2E секции)
if grep -qE '^### (Golden Data Questions|ФАЗА 3: Golden Data Questions|GDQ)' "$E2E_SECTION"; then
  check "11. GDQ секция существует (в E2E секции)" PASS
else
  check "11. GDQ секция существует (в E2E секции)" FAIL
fi

# 12. GDQ все 10 вопросов имеют ответы (внутри E2E секции)
GDQ_QUESTIONS=$(awk '/^### (Golden Data Questions|ФАЗА 3:|GDQ)/,/^### /{print}' "$E2E_SECTION" | grep -cE '^\s*[0-9]+\.')
if [ "$GDQ_QUESTIONS" -ge 10 ]; then
  check "12. GDQ содержит ≥10 вопросов (в E2E секции, найдено: $GDQ_QUESTIONS)" PASS
else
  check "12. GDQ содержит ≥10 вопросов (в E2E секции, найдено: $GDQ_QUESTIONS)" FAIL
fi

# 13. Новые тесты ≥ min_new_tests (считаем ТОЛЬКО в E2E секции, не по всему файлу)
MIN_NEW=$(grep -oE 'min_new_tests:\s*[0-9]+' "$E2E_SECTION" | head -1 | grep -oE '[0-9]+' || echo 0)
NEW_TEST_IDS=$(grep -cE '(E2E-|API-)[A-Z0-9_-]+' "$E2E_SECTION")
if [ -n "$MIN_NEW" ] && [ "$NEW_TEST_IDS" -ge "$MIN_NEW" ]; then
  check "13. Новых тестов в E2E секции ($NEW_TEST_IDS) ≥ min_new_tests ($MIN_NEW)" PASS
else
  check "13. Новых тестов в E2E секции ($NEW_TEST_IDS) ≥ min_new_tests ($MIN_NEW)" FAIL
fi

echo ""
echo "Всего проверок: $CHECKS"
echo "Провалено: $FAILS"

if [ "$FAILS" -eq 0 ]; then
  echo "SPEC_E2E_PASS"
  exit 0
else
  echo "SPEC_E2E_FAIL — вернись к спеке и исправь перечисленное"
  exit 1
fi
