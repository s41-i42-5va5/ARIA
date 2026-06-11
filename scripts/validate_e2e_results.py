#!/usr/bin/env python3
"""
E2E Results Validator v2.0 — Слой 4 (машинная валидация).
Проверяет результаты субагента-верификатора.
НЕ LLM. Детерминированная проверка артефактов.

ARIA v3.1: 3 фикса:
- Фикс #6: Индивидуальные метрики из спеки (min_action_count, min_duration_sec)
- Фикс #9: Console log ОБЯЗАТЕЛЕН (не опционален)
- Фикс #10: extract_spec_tests ищет ID ТОЛЬКО в E2E секции

Использование:
    python validate_e2e_results.py <result.yaml> <spec.md> <audit_dir>

Выход:
    GATE_PASS (exit 0) или GATE_FAIL (exit 1) + список нарушений.
"""

import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def load_yaml(path: str) -> dict:
    """Загрузить YAML файл результатов."""
    text = Path(path).read_text(encoding="utf-8")
    if yaml:
        return yaml.safe_load(text)
    raise RuntimeError("PyYAML не установлен. Установи: pip install pyyaml")


def extract_e2e_section(spec_text: str) -> str:
    """Извлечь секцию '## E2E Testing Plan' из спеки.

    ФИКС #10: ищем ID тестов ТОЛЬКО внутри E2E секции,
    а не во всём файле спеки.

    ОГРАНИЧЕНИЕ: regex ``## (?!#)`` может оборваться досрочно если внутри
    E2E секции есть ``## заголовок`` (без ``#`` после) вне code block.
    На практике маловероятно — шаблон использует ``###`` и ``####``.
    """
    match = re.search(
        r"## E2E Testing Plan\s*\n(.*?)(?=\n## (?!#)|\Z)",
        spec_text,
        re.DOTALL,
    )
    return match.group(1) if match else ""


def extract_spec_tests(spec_path: str) -> list[str]:
    """Извлечь список обязательных test ID из секции E2E Testing Plan спеки.

    ФИКС #10: ищем ТОЛЬКО в E2E секции, не во всём файле.
    """
    text = Path(spec_path).read_text(encoding="utf-8")

    # Извлекаем E2E секцию
    e2e_section = extract_e2e_section(text)

    # Ищем ID тестов ТОЛЬКО в E2E секции
    test_ids = re.findall(r"(E2E-\w+-\d+|API-\w+-\d+)", e2e_section)

    # Убираем дубли, сохраняя порядок
    seen = set()
    unique = []
    for tid in test_ids:
        if tid not in seen:
            seen.add(tid)
            unique.append(tid)

    return unique


def extract_test_metrics(spec_path: str) -> dict[str, dict]:
    """Извлечь индивидуальные метрики для каждого нового теста из спеки.

    ФИКС #6: парсим min_action_count и min_duration_sec из секции новых тестов.
    Возвращает: {test_id: {min_action_count: N, min_duration_sec: N}}
    """
    text = Path(spec_path).read_text(encoding="utf-8")
    e2e_section = extract_e2e_section(text)

    metrics = {}

    # Ищем блоки new_tests в YAML формате
    # Паттерн: id: E2E-TASK-XXX-NNN ... min_action_count: N ... min_duration_sec: N
    test_blocks = re.findall(
        r"- id:\s*(E2E-TASK-\w+-\d+).*?(?=\n\s*- id:|\n```|\Z)",
        e2e_section,
        re.DOTALL,
    )

    for block_match in re.finditer(
        r"- id:\s*(E2E-TASK-\w+-\d+)(.*?)(?=\n\s*- id:|\n```|\Z)",
        e2e_section,
        re.DOTALL,
    ):
        tid = block_match.group(1)
        block_text = block_match.group(2)

        test_metrics = {}

        ac_match = re.search(r"min_action_count:\s*(\d+)", block_text)
        if ac_match:
            test_metrics["min_action_count"] = int(ac_match.group(1))

        dur_match = re.search(r"min_duration_sec:\s*(\d+)", block_text)
        if dur_match:
            test_metrics["min_duration_sec"] = int(dur_match.group(1))

        if test_metrics:
            metrics[tid] = test_metrics

    return metrics


def validate(result_path: str, spec_path: str, audit_dir: str) -> tuple[bool, list[str]]:
    """
    Основная валидация. Возвращает (gate_pass, errors).

    10 проверок:
    1. ПОЛНОТА      — все тесты из спеки проведены
    2. БИНАРНОСТЬ   — результат строго PASS или FAIL
    3. FAIL-ПОЛЯ    — FAIL содержит обязательные поля
    4. СКРИНШОТЫ    — файлы существуют и > 5KB
    5. NETWORK LOG  — файлы существуют и непустые
    6. ACTION LOG   — файлы существуют, индивидуальные метрики из спеки
    7. CONSOLE      — ОБЯЗАТЕЛЕН, нет критических JS ошибок при PASS
    8. AGENT        — agent = "verifier"
    9. VERDICT      — соответствует результатам
    10. COUNT       — заявленное количество = фактическому
    """
    errors: list[str] = []

    # Загрузка результатов
    try:
        results = load_yaml(result_path)
    except Exception as e:
        errors.append(f"Не удалось загрузить YAML результатов: {e}")
        return False, errors

    # Загрузка спеки
    try:
        spec_tests = extract_spec_tests(spec_path)
    except Exception as e:
        errors.append(f"Не удалось загрузить спеку: {e}")
        return False, errors

    # ФИКС #6: загрузка индивидуальных метрик для новых тестов
    try:
        test_metrics = extract_test_metrics(spec_path)
    except Exception:
        test_metrics = {}

    tests = results.get("tests", [])
    audit = Path(audit_dir)

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 1: ПОЛНОТА — все тесты из спеки проведены
    # ═══════════════════════════════════════════
    result_test_ids = [t.get("id", "") for t in tests]
    missing = set(spec_tests) - set(result_test_ids)
    if missing:
        errors.append(f"[1-ПОЛНОТА] ПРОПУЩЕНЫ тесты из спеки: {sorted(missing)}")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 2: БИНАРНОСТЬ — только PASS или FAIL
    # ═══════════════════════════════════════════
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        result = test.get("result", "")
        if result not in ("PASS", "FAIL"):
            errors.append(
                f"[2-БИНАРНОСТЬ] {tid}: недопустимый результат '{result}' "
                f"(допустимо ТОЛЬКО PASS или FAIL)"
            )

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 3: FAIL-ПОЛЯ — обязательные поля при FAIL
    # ═══════════════════════════════════════════
    fail_required_fields = ["failed_check", "expected", "actual", "screenshot"]
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        if test.get("result") == "FAIL":
            for field in fail_required_fields:
                if field not in test or not test[field]:
                    errors.append(f"[3-FAIL-ПОЛЯ] {tid}: FAIL без обязательного поля '{field}'")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 4: СКРИНШОТЫ — существуют и > 5KB
    # ═══════════════════════════════════════════
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        ss = test.get("screenshot", "")
        if not ss:
            errors.append(f"[4-СКРИНШОТ] {tid}: поле screenshot отсутствует или пустое")
            continue
        ss_path = audit / ss
        if not ss_path.exists():
            errors.append(f"[4-СКРИНШОТ] {tid}: файл '{ss}' НЕ НАЙДЕН в {audit_dir}")
        elif ss_path.stat().st_size < 5000:
            size = ss_path.stat().st_size
            errors.append(
                f"[4-СКРИНШОТ] {tid}: файл '{ss}' = {size} bytes "
                f"(< 5KB — пустой или битый)"
            )

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 5: NETWORK LOG — существует и непустой
    # ═══════════════════════════════════════════
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        net_file = test.get("network_log", "")
        if not net_file:
            errors.append(f"[5-NETWORK] {tid}: поле network_log отсутствует")
            continue
        net_path = audit / net_file
        if not net_path.exists():
            errors.append(
                f"[5-NETWORK] {tid}: network log '{net_file}' НЕ НАЙДЕН — "
                f"нет доказательств что тест проводился через браузер"
            )
        else:
            try:
                content = net_path.read_text(encoding="utf-8").strip()
                if not content or content in ("[]", "{}"):
                    errors.append(
                        f"[5-NETWORK] {tid}: network log ПУСТ — "
                        f"0 HTTP запросов зафиксировано"
                    )
            except Exception as e:
                errors.append(f"[5-NETWORK] {tid}: ошибка чтения network log: {e}")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 6: ACTION LOG — ФИКС: индивидуальные метрики из спеки
    # ═══════════════════════════════════════════
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        act_file = test.get("action_log", "")
        if not act_file:
            errors.append(f"[6-ACTIONS] {tid}: поле action_log отсутствует")
            continue
        act_path = audit / act_file
        if not act_path.exists():
            errors.append(f"[6-ACTIONS] {tid}: action log '{act_file}' НЕ НАЙДЕН")
        else:
            try:
                actions = json.loads(act_path.read_text(encoding="utf-8"))

                # Получаем индивидуальные метрики или глобальные fallback
                individual = test_metrics.get(tid, {})
                required_actions = individual.get("min_action_count", 3)  # fallback: 3
                required_duration = individual.get("min_duration_sec", 5)  # fallback: 5

                # Минимум действий (индивидуальный или глобальный)
                if len(actions) < required_actions:
                    errors.append(
                        f"[6-ACTIONS] {tid}: {len(actions)} действий — "
                        f"слишком мало (минимум {required_actions})"
                    )

                # Timestamps монотонно возрастают
                if len(actions) >= 2:
                    timestamps = [a.get("ts", 0) for a in actions]
                    if timestamps != sorted(timestamps):
                        errors.append(
                            f"[6-ACTIONS] {tid}: timestamps НЕ последовательны — "
                            f"возможная фабрикация"
                        )

                    # Длительность (индивидуальная или глобальная)
                    duration_sec = (timestamps[-1] - timestamps[0]) / 1000
                    if duration_sec < required_duration:
                        errors.append(
                            f"[6-ACTIONS] {tid}: длительность {duration_sec:.1f}с — "
                            f"подозрительно быстро (минимум {required_duration}с)"
                        )

            except json.JSONDecodeError:
                errors.append(f"[6-ACTIONS] {tid}: action log — невалидный JSON")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 7: CONSOLE — ФИКС #9: ОБЯЗАТЕЛЕН (не опционален)
    # ═══════════════════════════════════════════
    critical_patterns = [
        "Uncaught",
        "ChunkLoadError",
        "FATAL",
        "ReferenceError",
        "TypeError: Cannot read",
    ]
    for test in tests:
        tid = test.get("id", "UNKNOWN")
        con_file = test.get("console_log", "")

        # ФИКС #9: console log ОБЯЗАТЕЛЕН, не опционален
        if not con_file:
            errors.append(
                f"[7-CONSOLE] {tid}: поле console_log ОТСУТСТВУЕТ — "
                f"console log ОБЯЗАТЕЛЕН для каждого теста"
            )
            continue

        con_path = audit / con_file
        if not con_path.exists():
            errors.append(
                f"[7-CONSOLE] {tid}: console log '{con_file}' НЕ НАЙДЕН — "
                f"console log ОБЯЗАТЕЛЕН"
            )
        else:
            try:
                console_text = con_path.read_text(encoding="utf-8")
                if test.get("result") == "PASS":
                    for pattern in critical_patterns:
                        if pattern in console_text:
                            errors.append(
                                f"[7-CONSOLE] {tid}: PASS заявлен, но console содержит "
                                f"критическую ошибку '{pattern}'"
                            )
                            break  # одной ошибки достаточно
            except Exception as e:
                errors.append(f"[7-CONSOLE] {tid}: ошибка чтения console log: {e}")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 8: AGENT — тесты проведены верификатором
    # ═══════════════════════════════════════════
    agent = results.get("agent", "unknown")
    if agent != "verifier":
        errors.append(
            f"[8-AGENT] Тесты проведены агентом '{agent}', ожидался 'verifier'"
        )

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 9: VERDICT — соответствует результатам
    # ═══════════════════════════════════════════
    has_fail = any(t.get("result") == "FAIL" for t in tests)
    verdict = results.get("summary", {}).get("verdict", "")
    if has_fail and verdict != "FAIL":
        errors.append(
            f"[9-VERDICT] Verdict '{verdict}' при наличии FAIL тестов — "
            f"verdict должен быть FAIL"
        )
    if not has_fail and verdict == "FAIL":
        errors.append(f"[9-VERDICT] Verdict 'FAIL' но все тесты PASS — ошибка отчёта")

    # ═══════════════════════════════════════════
    # ПРОВЕРКА 10: COUNT — заявленное = фактическому
    # ═══════════════════════════════════════════
    summary = results.get("summary", {})
    declared_total = summary.get("total", -1)
    actual_total = len(tests)
    if declared_total != actual_total:
        errors.append(
            f"[10-COUNT] Заявлено {declared_total} тестов, "
            f"в файле {actual_total} — несоответствие"
        )

    declared_pass = summary.get("pass", -1)
    actual_pass = sum(1 for t in tests if t.get("result") == "PASS")
    if declared_pass != actual_pass:
        errors.append(
            f"[10-COUNT] Заявлено pass={declared_pass}, фактически pass={actual_pass}"
        )

    declared_fail = summary.get("fail", -1)
    actual_fail = sum(1 for t in tests if t.get("result") == "FAIL")
    if declared_fail != actual_fail:
        errors.append(
            f"[10-COUNT] Заявлено fail={declared_fail}, фактически fail={actual_fail}"
        )

    gate_pass = len(errors) == 0
    return gate_pass, errors


def main():
    if len(sys.argv) < 4:
        print("Usage: python validate_e2e_results.py <result.yaml> <spec.md> <audit_dir>")
        print()
        print("  result.yaml  — YAML файл результатов верификатора")
        print("  spec.md      — спека задачи (содержит E2E Testing Plan)")
        print("  audit_dir    — директория с артефактами (скриншоты, логи)")
        print()
        print("  ARIA v3.1 фиксы:")
        print("  - Индивидуальные метрики из спеки (min_action_count, min_duration_sec)")
        print("  - Console log ОБЯЗАТЕЛЕН (не опционален)")
        print("  - extract_spec_tests ищет ID ТОЛЬКО в E2E секции")
        sys.exit(2)

    result_path = sys.argv[1]
    spec_path = sys.argv[2]
    audit_dir = sys.argv[3]

    # Проверка существования файлов
    if not Path(result_path).exists():
        print(f"GATE_FAIL: файл результатов не найден: {result_path}")
        sys.exit(1)
    if not Path(spec_path).exists():
        print(f"GATE_FAIL: файл спеки не найден: {spec_path}")
        sys.exit(1)
    if not Path(audit_dir).is_dir():
        print(f"GATE_FAIL: директория аудита не найдена: {audit_dir}")
        sys.exit(1)

    gate_pass, errors = validate(result_path, spec_path, audit_dir)

    print()
    print("=" * 60)
    print(f"  E2E VALIDATION: {'GATE_PASS' if gate_pass else 'GATE_FAIL'}")
    print("=" * 60)

    if errors:
        print(f"\n  Нарушений: {len(errors)}\n")
        for i, err in enumerate(errors, 1):
            print(f"  {i:2d}. {err}")
    else:
        print("\n  Все 10 проверок пройдены. Артефакты валидны.")

    print()
    print("=" * 60)

    sys.exit(0 if gate_pass else 1)


if __name__ == "__main__":
    main()
