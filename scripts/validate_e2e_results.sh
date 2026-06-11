#!/usr/bin/env bash
# validate_e2e_results.sh — validate YAML from E2E verifier against spec's Test Coverage Matrix
# Usage: bash validate_e2e_results.sh <results_yaml> <spec_or_testfile_path> <audit_dir>
# Exit codes: 0 = GATE_PASS, 1 = GATE_FAIL
#
# 10 проверок (портировано из Python-эталона validate_e2e_results.py):
# 1. YAML обязательные поля
# 2. Полнота покрытия тестов из спеки
# 3. Каждый тест имеет status
# 4. Audit trail существует
# 5. FAIL-тесты имеют описание бага
# 6. Screenshots >5KB (не пустые заглушки)
# 7. Console log без критических ошибок (Uncaught/FATAL/TypeError)
# 8. Verdict соответствует результатам тестов
# 9. Count match — заявленное количество vs фактическое
# 10. Общий вердикт

set -u

YAML="${1:-}"
SPEC="${2:-}"
AUDIT="${3:-}"

if [ -z "$YAML" ] || [ ! -f "$YAML" ]; then
  echo "Usage: $0 <results_yaml> <spec_or_testfile_path> <audit_dir>"
  echo "results YAML not found: $YAML"
  exit 1
fi
if [ -z "$SPEC" ] || [ ! -f "$SPEC" ]; then
  echo "spec/testfile not found: $SPEC"
  exit 1
fi
if [ -z "$AUDIT" ] || [ ! -d "$AUDIT" ]; then
  echo "audit dir not found: $AUDIT"
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

echo "Validating: $YAML"
echo "Against: $SPEC"
echo ""

# 1. YAML имеет обязательные поля
if grep -qE '^spec_path:' "$YAML" && grep -qE '^timestamp:' "$YAML" && grep -qE '^tests:' "$YAML"; then
  check "1. YAML содержит spec_path/timestamp/tests" PASS
else
  check "1. YAML содержит spec_path/timestamp/tests" FAIL
fi

# 2. Результаты по всем тестам из спеки/библиотеки присутствуют
SPEC_TEST_IDS=$(grep -oE '(E2E-|API-)[A-Z0-9_-]+' "$SPEC" | sort -u)
MISSING_IDS=()
for id in $SPEC_TEST_IDS; do
  if ! grep -qF "$id" "$YAML"; then
    MISSING_IDS+=("$id")
  fi
done
if [ "${#MISSING_IDS[@]}" -eq 0 ] && [ -n "$SPEC_TEST_IDS" ]; then
  check "2. Все тесты из спеки ($(echo "$SPEC_TEST_IDS" | wc -w | tr -d ' ')) есть в результатах" PASS
elif [ -z "$SPEC_TEST_IDS" ]; then
  check "2. В спеке не найдены тест-ID (E2E-/API-)" FAIL
else
  check "2. Отсутствуют в результатах: ${MISSING_IDS[*]}" FAIL
fi

# 3. Для каждого теста — status (pass/fail)
TESTS_COUNT=$(grep -cE '^\s*-\s+id:' "$YAML" || echo 0)
STATUS_COUNT=$(grep -cE '^\s+status:\s+(pass|fail|skipped)' "$YAML" || echo 0)
if [ "$STATUS_COUNT" -ge "$TESTS_COUNT" ] && [ "$TESTS_COUNT" -gt 0 ]; then
  check "3. Каждый тест имеет status (pass/fail/skipped): $STATUS_COUNT/$TESTS_COUNT" PASS
else
  check "3. Статусов тестов ($STATUS_COUNT) < тестов ($TESTS_COUNT)" FAIL
fi

# 4. Audit trail существует для каждого теста
AUDIT_FILES=$(find "$AUDIT" -maxdepth 2 -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$AUDIT_FILES" -ge "$TESTS_COUNT" ] && [ "$TESTS_COUNT" -gt 0 ]; then
  check "4. Audit trail присутствует ($AUDIT_FILES артефактов для $TESTS_COUNT тестов)" PASS
else
  check "4. Audit trail неполный ($AUDIT_FILES для $TESTS_COUNT тестов)" FAIL
fi

# 5. FAIL-тесты имеют описание бага
FAIL_COUNT=$(grep -cE '^\s+status:\s+fail' "$YAML" || echo 0)
if [ "$FAIL_COUNT" -gt 0 ]; then
  # у каждого fail должно быть поле bug или reason
  BUG_REASON_COUNT=$(grep -cE '^\s+(bug|reason|error):' "$YAML" || echo 0)
  if [ "$BUG_REASON_COUNT" -ge "$FAIL_COUNT" ]; then
    check "5. FAIL-тесты имеют описание бага ($BUG_REASON_COUNT/$FAIL_COUNT)" PASS
  else
    check "5. FAIL-тесты без описания бага ($BUG_REASON_COUNT описаний для $FAIL_COUNT fail)" FAIL
  fi
else
  check "5. Нет FAIL-тестов — проверка пропущена" PASS
fi

# 6. Screenshots >5KB (не пустые заглушки) — портировано из Python-эталона
SCREENSHOT_FILES=$(find "$AUDIT" -maxdepth 3 -type f \( -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" \) 2>/dev/null)
if [ -n "$SCREENSHOT_FILES" ]; then
  SMALL_SCREENSHOTS=0
  TOTAL_SCREENSHOTS=0
  while IFS= read -r screenshot; do
    TOTAL_SCREENSHOTS=$((TOTAL_SCREENSHOTS + 1))
    FILE_SIZE=$(wc -c < "$screenshot" | tr -d ' ')
    if [ "$FILE_SIZE" -lt 5120 ]; then
      SMALL_SCREENSHOTS=$((SMALL_SCREENSHOTS + 1))
      echo "    WARN: маленький screenshot (<5KB): $screenshot ($FILE_SIZE bytes)"
    fi
  done <<< "$SCREENSHOT_FILES"
  if [ "$SMALL_SCREENSHOTS" -eq 0 ]; then
    check "6. Screenshots >5KB ($TOTAL_SCREENSHOTS файлов, все валидного размера)" PASS
  else
    check "6. Screenshots >5KB ($SMALL_SCREENSHOTS из $TOTAL_SCREENSHOTS < 5KB — возможно пустые заглушки)" FAIL
  fi
else
  check "6. Screenshots — нет файлов в audit (допустимо для API-тестов)" PASS
fi

# 7. Console log без критических ошибок (Uncaught/FATAL/TypeError) — портировано из Python-эталона
CONSOLE_FILES=$(find "$AUDIT" -maxdepth 3 -type f -name "*.log" -o -name "*console*" 2>/dev/null)
if [ -n "$CONSOLE_FILES" ]; then
  CRITICAL_ERRORS=0
  while IFS= read -r logfile; do
    if grep -qEi '(Uncaught|FATAL|TypeError|ReferenceError|SyntaxError)' "$logfile" 2>/dev/null; then
      CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
      echo "    CRITICAL: ошибки в консоли: $logfile"
      grep -Ei '(Uncaught|FATAL|TypeError|ReferenceError|SyntaxError)' "$logfile" | head -3
    fi
  done <<< "$CONSOLE_FILES"
  if [ "$CRITICAL_ERRORS" -eq 0 ]; then
    check "7. Console log без критических ошибок" PASS
  else
    check "7. Console log содержит критические ошибки ($CRITICAL_ERRORS файлов)" FAIL
  fi
else
  check "7. Console log — нет лог-файлов в audit (допустимо)" PASS
fi

# 8. Verdict соответствует результатам тестов — портировано из Python-эталона
# Если YAML содержит verdict: pass, но есть fail-тесты — несоответствие
YAML_VERDICT=$(grep -oE '^\s*verdict:\s*(pass|fail)' "$YAML" | head -1 | awk '{print $2}' || echo "")
PASSED=$(grep -cE '^\s+status:\s+pass' "$YAML" || echo 0)
FAILED=$(grep -cE '^\s+status:\s+fail' "$YAML" || echo 0)
SKIPPED=$(grep -cE '^\s+status:\s+skipped' "$YAML" || echo 0)

if [ -n "$YAML_VERDICT" ]; then
  if [ "$YAML_VERDICT" = "pass" ] && [ "$FAILED" -gt 0 ]; then
    check "8. Verdict соответствие: verdict=pass но $FAILED тестов failed" FAIL
  elif [ "$YAML_VERDICT" = "fail" ] && [ "$FAILED" -eq 0 ]; then
    check "8. Verdict соответствие: verdict=fail но 0 тестов failed" FAIL
  else
    check "8. Verdict соответствует результатам (verdict=$YAML_VERDICT, failed=$FAILED)" PASS
  fi
else
  check "8. Verdict отсутствует в YAML — допустимо (определяется по status)" PASS
fi

# 9. Count match — заявленное количество vs фактическое — портировано из Python-эталона
YAML_TOTAL=$(grep -oE '^\s*total:\s*[0-9]+' "$YAML" | head -1 | grep -oE '[0-9]+' || echo "")
if [ -n "$YAML_TOTAL" ]; then
  if [ "$YAML_TOTAL" -eq "$TESTS_COUNT" ]; then
    check "9. Count match: заявлено $YAML_TOTAL, фактически $TESTS_COUNT" PASS
  else
    check "9. Count match: заявлено $YAML_TOTAL, фактически $TESTS_COUNT — РАСХОЖДЕНИЕ" FAIL
  fi
else
  # total не указан — проверяем что passed+failed+skipped = tests_count
  COMPUTED=$((PASSED + FAILED + SKIPPED))
  if [ "$COMPUTED" -eq "$TESTS_COUNT" ] && [ "$TESTS_COUNT" -gt 0 ]; then
    check "9. Count match: pass+fail+skipped=$COMPUTED = tests=$TESTS_COUNT" PASS
  elif [ "$TESTS_COUNT" -eq 0 ]; then
    check "9. Count match: 0 тестов в YAML" FAIL
  else
    check "9. Count match: pass+fail+skipped=$COMPUTED != tests=$TESTS_COUNT" FAIL
  fi
fi

# 10. Общий вердикт
echo ""
echo "Тесты: $TESTS_COUNT (pass: $PASSED, fail: $FAILED, skipped: $SKIPPED)"
echo "Проверок валидатора: $CHECKS, провалено: $FAILS"

if [ "$FAILS" -eq 0 ] && [ "$FAILED" -eq 0 ]; then
  echo "GATE_PASS"
  exit 0
else
  echo "GATE_FAIL — $FAILED тестов failed, $FAILS проверок валидатора"
  exit 1
fi
