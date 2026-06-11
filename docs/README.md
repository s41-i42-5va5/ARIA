# ARIA — AI-powered Development Framework

**ARIA** (Autonomous Research & Implementation Architecture) — фреймворк управления разработкой с Claude Code.

Универсальная система, которая превращает Claude Code из "умного помощника" в **управляемый конвейер разработки** с:
- Спецификациями перед кодом (/spec)
- Многоуровневым ревью (/review)
- Автоматическим режимом (/auto)
- Quality Gates
- Двухфазным коммитом (код + docs)
- Протоколом решения проблем (3 whys, 3 files, 3 attempts)

## Быстрый старт

### 1. Выбери адаптер

| Адаптер | Стек | Источник |
|---------|------|----------|
| `python-fastapi` | Python + FastAPI + React | Обкатан в форках (37+ задач) |
| `kotlin-android` | Kotlin + Jetpack Compose | Обкатан в форках |
| `csharp-avalonia` | C# + Avalonia UI | Обкатан в форках |

### 2. Скопируй в свой проект

```bash
# Создай структуру docs (Obsidian)
mkdir -p ~/Obsidian/W3_MyProject/{docs/{spec,ADR,research},tests/e2e,.claude/commands}

# Скопируй ядро + адаптер
cp ARIA/core/CLAUDE.md.template ~/Obsidian/W3_MyProject/CLAUDE.md
cp ARIA/core/commands/*.md ~/Obsidian/W3_MyProject/.claude/commands/
cp ARIA/core/templates/*.template ~/Obsidian/W3_MyProject/

# Адаптируй шаблоны: замени {{плейсхолдеры}} на свои значения
```

### 3. Настрой junction в репо

```bash
# В репо проекта
mklink /J project-docs "C:\path\to\Obsidian\W3_MyProject"
```

### 4. Установи переменные окружения

```bash
export MYPROJECT_DOCS="/path/to/Obsidian/W3_MyProject"
export MYPROJECT_CODE="/path/to/repo"
```

## Архитектура

```
ARIA/
├── core/                           # Универсальное ядро
│   ├── CLAUDE.md.template          # Шаблон CLAUDE.md (правила, протоколы)
│   ├── commands/                   # 11 команд конвейера
│   │   ├── auto.md                 # Автономный режим
│   │   ├── done.md                 # Завершение задачи + коммит
│   │   ├── spec.md                 # Создание спецификации
│   │   ├── next-task.md            # Выбор следующей задачи
│   │   ├── review.md               # 4-стороннее ревью
│   │   ├── research.md             # Свободное исследование
│   │   ├── status.md               # Статус проекта
│   │   ├── aria-sync.md            # Синхронизация с upstream
│   │   ├── aria-release.md         # Релиз новой версии ARIA
│   │   ├── aria-docs-audit.md      # Аудит документации
│   │   └── adr-new.md              # Создание ADR
│   ├── protocols/                  # Мета-протоколы
│   │   └── fork_sync_playbook.md   # Протокол синхронизации форков
│   └── templates/                  # Шаблоны конфигов
│       ├── PATHS.yaml.template
│       ├── project_config.yaml.template
│       ├── STATE.yaml.template
│       └── STACK.md.template
│
├── adapters/                       # Стек-специфичные расширения
│   ├── python-fastapi/             # Python backend + React frontend
│   │   ├── adapter.yaml            # Антипаттерны, команды, commit format
│   │   └── hooks/pre-commit        # Soft warnings для форка
│   ├── kotlin-android/             # Android native app
│   │   ├── adapter.yaml
│   │   └── hooks/pre-commit        # Soft warnings для форка
│   └── csharp-avalonia/            # Кроссплатформенное .NET приложение
│       ├── adapter.yaml
│       └── hooks/pre-commit        # Soft warnings для форка
│
├── docs/                    # Документация самой ARIA
│   ├── README.md            # Этот файл (обзор docs/)
│   ├── SPEC.md              # Генеральная спека ARIA
│   ├── STACK.md             # Технологический стек
│   ├── ARIA_GUIDE.md        # Гайд для пользователей
│   ├── policies/            # CHANGELOG/COMMIT/DOCUMENTATION_LIFECYCLE
│   ├── ADR/                 # Архитектурные решения
│   └── research/            # Обзоры и гипотезы
│
├── README.md                # Внешняя витрина + секция «Контрибьюция»
├── FORKS.md                 # Реестр известных форков
├── TRIAGE.md                # Входящие PR от форков (maintainer)
└── CHANGELOG.md             # История версий ARIA (гибридный формат: scope + таблицы с атрибуцией)
```

## Принципы ARIA

### 1. Spec-First
Код пишется ТОЛЬКО по спецификации. Нет spec → нет кода.

### 2. Adversarial Review
Каждая спека проходит через "атакера" — субагента, который ищет слабости.

### 3. Two-Phase Commit
Код и документация НИКОГДА не смешиваются в одном коммите.

### 4. 3-3-3 Protocol
- **3 whys** — ищи корневую причину, не симптом
- **3 files** — если фикс тянет 3+ файлов, это не локальный баг
- **3 attempts** — не решил за 3 попытки → эскалация

### 5. Production-Grade Only
Нет MVP. Нет "потом доделаем". Каждое решение — production quality.

## Как развивается ARIA

ARIA развивается через **проекты-форки**:

```
ARIA upstream ──fork──→ Проект A (адаптирует)
                        ├── находит ценное решение
                        └── contribute → ARIA upstream

ARIA upstream ──fork──→ Проект B (получает обновления)
                        ├── находит ценное решение
                        └── contribute → ARIA upstream
```

Команда `/aria-sync` в каждом проекте показывает diff с upstream и предлагает интеграцию.
