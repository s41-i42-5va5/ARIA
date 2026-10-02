---
name: aria-review
description: Ревьюить spec, компонент или репозиторий через project-aware ARIA с заранее фиксированным scope; не исправлять код без отдельного запроса.
---

# ARIA Review

1. Запустить `aria run --project <project> --task "<что проверить>" --intent review --target-type spec|component|repository --target <path>`.
2. Открыть `scope_path` и подтвердить, что root — зарегистрированный code/project-docs root, а target, file count и SHA соответствуют запросу.
3. Не расширять и не сужать scope после старта. Новый scope требует нового run.
4. Для component/repository review до выбора и запуска тестов создать свободную `outputs/functional-coverage.md` по headings из manifest. По каждому существенному поведению разобрать функции, поля/входы/выходы/состояния, реальные bindings, взаимодействия, ошибки, границы, вложенность/композицию и test obligations. В карте спроектировать минимум один Mission Supertest на runtime-компонент: цель продуктовой миссии, SUT focus, покрываемые возможности, соседние ноды/слои, failure hypotheses, oracle/invariants, load/fault profile, side-effect read-backs и ограничения доказательства. При наличии spec опираться на неё; без spec различать observed, inferred и unknown по коду. Не строить формальную матрицу всех комбинаций и не смешивать несовместимые режимы искусственно: выбирать содержательные risk-bearing связки.
5. Независимый `functional_coverage_reviewer` ищет пропущенные функции и проверяет соответствие тестов карте. Он не подменяет фактические runtime tests.
6. Проверить каждое измерение `manifest.assurance_plan.review_dimensions`; каждый captured source-файл отметить `reviewed`, а non-source boundaries учесть только через точный excluded inventory.
7. Findings содержат severity, file:line, фактическое evidence, влияние, production root cause и рекомендуемый root fix. Неопределённость называть неопределённостью: симптом или гипотезу не выдавать за доказанную причину.
8. Выполнить обязательные verification classes и Mission Supertest. Он обязан проходить через реальный пользовательский/runtime путь и соседние компоненты, пытаться нарушить ключевые invariants, давать фактические read-backs и не считаться PASS при подмене browser/live infrastructure mock-слоем. Repository review является assurance campaign: E2E/adversarial/concurrency/load/stress/recovery/full regression плюс честная оценка soak/chaos/migration/rollback/security isolation.
9. Сразу после review вывести пользователю полный problem report: отдельно подтверждённые дефекты, риски/unknown, проблемы окружения/конфигурации, не-баги и проверенное отсутствие проблем. Для каждой проблемы предложить промышленное продуктовое решение корневой причины, учитывающее архитектуру, безопасность, данные, совместимость/миграцию, наблюдаемость, эксплуатацию и масштаб проекта, плюс проверки приёмки. Не предлагать локальный обход, подавление симптома или избыточную платформу. Не изменять код без отдельного запроса.
10. Закрыть run одним result со scope SHA, functional coverage path/SHA/scope SHA, findings, file/dimension coverage, verification evidence, role artifacts, read-back и closure. Свежий `SYSTEM_MAP.yaml` candidate допустим только в deep repository review, обязан сохранять существующую карту и быть привязан к текущему Git/worktree.
11. Role artifacts создавать после финальных scope/output/map artifacts и привязать к `target_kind`/`target_sha256` из скрытой `_role-target`.
