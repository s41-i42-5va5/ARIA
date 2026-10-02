---
name: aria-test
description: Выбирать минимально достаточный focused, E2E или full уровень и подтверждать фактический результат, а не только exit code.
---

# ARIA Test

При наличии execution contract канонический путь исполнения — `aria verify`; ручной запуск
не создаёт допустимого receipt и не закрывает required execution class.

1. Выполнить классы из `manifest.assurance_plan`: focused для локального поведения; E2E/adversarial для cross-layer/deep; concurrency/load/stress для shared state; recovery/chaos для отказов; migration/rollback для схем; full/soak для release и широкого blast radius.
2. Для каждого runtime-компонента в component/repository review и standard/deep build выполнить минимум один Mission Supertest. Это не «большой unit test», а одна осмысленная продуктовая миссия с фокусом на SUT, реальными соседними нодами/слоями и попыткой сломать ключевые invariants. Она объединяет только совместимые возможности и применимые E2E/adversarial/concurrency/load/stress/recovery воздействия; остальные функции получают отдельные векторные тесты.
3. Зафиксировать purpose, SUT focus, covered capabilities, neighbors, failure hypotheses, oracle/invariants, initial state, actions, expected и forbidden result, side effects, correlation, parallelism/load profile, injected faults, read-backs, actual result и limitations. Load величина выводится из SLO/ёмкости/риска; искусственная сложность не считается качеством.
4. Использовать канонические команды реального code root. Browser/UI проверять настоящим браузером. Controlled deterministic provider допустим для воспроизводимого load/fault пути, но live-provider readiness требует отдельного live canary; отсутствующая внешняя инфраструктура остаётся `BLOCKED/UNPROVEN`.
5. После каждого теста прочитать фактический output, изменённый deliverable или обратный side effect.
6. FAIL означает анализ production root cause; тест и fixture не подгонять под дефект.
7. Не объявлять PASS при незакрытой uncertainty, отсутствующей инфраструктуре или несовпадении setup с пользовательским путём.
8. Сразу после тестирования вывести пользователю найденные проблемы, а не только статусы тестов. Разделить product defects, риски/unknown, environment/config blockers и не-баги. Для каждой проблемы дать evidence, impact, доказанный production root cause либо следующий шаг его установления, промышленный root fix и проверки приёмки. Не считать workaround, подавление ошибки или изменение fixture ремонтом.
9. В closure сохранить classes, command, exit code, raw-output path/SHA, excerpt, реально присутствующий в output, actual result и `class_evidence`. Один Mission Supertest может доказывать несколько classes, но каждому нужны собственные proof excerpts из raw output; concurrency/load/stress/soak дополнительно дают JSON metrics excerpt из того же output. Raw logs остаются в runtime.
