# ARIA — Roadmap

> **Автогенерируется** командой `/roadmap-sync` из `docs/SPEC.md` секция 6 (Roadmap) + `STATE.yaml` прогресс.
> **Пользователь не редактирует этот файл вручную** — правит SPEC.md секцию 6, затем запускает `/roadmap-sync`.
> **Валидатор:** pre-commit hook проверяет согласованность с SPEC.md (правило 1 живого документа — исключение hook-читателя).

Последнее обновление: 2026-04-15
Источник истины: `docs/SPEC.md` секция 6

---

## Phase 1: Стабилизация и автоматизация

**Spec:** [docs/spec/phase-1-stabilization.md](docs/spec/phase-1-stabilization.md) (v2.4)
**Цель:** Новый пользователь разворачивает ARIA одной командой. Владелец upstream обрабатывает contribute-back и уведомляет форки.
**Результат:** production-grade инструмент с замкнутыми контурами, живыми документами, разделением репо, гарантией не-регрессии относительно форков.
**Прогресс:** 1/16 задач (6%)

### P1 — Фундамент

- [x] **living-doc-rule** (Task 11) — правила живого документа, необходимости, разделения репо + контракты SYS_CHANGELOG/research ✓ 2026-04-15 TBD
- [ ] **spec-md-infrastructure** (Task 8) — SPEC.md.template + SPEC.md upstream + расширенный STATE.yaml.template (files/tests/acceptance/commit)
- [ ] **mcp-infrastructure** (Task 2) — .mcp.json.template + mcp_servers в adapter.yaml × 3
- [ ] **references-regression-fix** (Task 13) — /spec ШАГ 2 пишет в REFERENCES, формат Источник/Взяли/Изменили/Антиреференс, контракт research/
- [ ] **commands-regression-fix** (Task 15) — порт spec.md/review.md/next-task.md + validate скрипты
- [ ] **aria-init** (Task 1) — стартовый промпт + SPEC-генерация + project_config.commands
- [ ] **roadmap-populate** (Task 3) — автоген ROADMAP.md из SPEC.md + pre-commit валидатор

### P2 — Экосистема

- [ ] **aria-stack-md** (Task 9) — STACK.md upstream + контур /done/pre-commit/audit
- [ ] **triage-md** (Task 5) — TRIAGE.md буфер входящих предложений
- [ ] **forks-md** (Task 6) — FORKS.md реестр форков с Repo-полем
- [ ] **github-infrastructure** (Task 7) — PR template + 12 labels + auto-labeling в /aria-sync --contribute
- [ ] **aria-triage** (Task 4) — пакетная обработка PR через gh CLI
- [ ] **contributions-md-delete** (Task 12) — удаление CONTRIBUTIONS.md + гибридный CHANGELOG
- [ ] **aria-release-notify** (Task 10) — FIX: ШАГ 7 читает FORKS.md (не CONTRIBUTIONS)
- [ ] **readme-upstream** (Task 14) — README.md upstream (внешняя аудитория, контур через /done)
- [ ] **forks-cleanup-migration** (Task 16) — очистка реликтов 6 апреля в code-repo форков 

### Блокеры
нет

---

## Phase 2: Расширение (видение)

> Детали — при старте фазы. Сейчас только направления развития, задачи не разбиты.

### Возможные направления
- Новые адаптеры (Go, Rust, Swift, Flutter, Node)
- MCP-сервер для ARIA (команды как MCP-tools)
- CLI-обёртка `aria` (независимость от Claude Code)
- Git worktrees — параллельная работа над задачами
- Matrix совместимости: какие команды работают с какими адаптерами
- E2E тесты для команд ARIA (integration tests с реальным git repo)
- `/aria-propagate` — массовое уведомление форков (pull из upstream + автоапплай NEW)

---

## История релизов

- v3.3 (2026-04-14) — Documentation lifecycle + Mechanical policy enforcement
- v3.0 (2026-04-14) — Первичное ядро (из форков)
