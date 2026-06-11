Свободное исследование по запросу. НЕ часть конвейера /auto или /next-task.
Используй когда нужна разведка ДО формулировки задачи или для свободного изучения темы.
Research для конкретных задач встроен в /spec (Аналитик + Research-субагент).

**PATHS: Прочитай $ARIA_DOCS/PATHS.yaml — все пути к документам, спекам, тестам определены там.**

**МОДЕЛЬ: Субагент ОБЯЗАН запускаться с model: "opus". Sonnet/Haiku ЗАПРЕЩЕНЫ.**

---

1. Определи тему исследования (из сообщения пользователя или контекста).
2. mkdir -p "{research}"

3. Запусти субагент (Task tool, subagent_type: "general-purpose", model: "opus"):

```
Ты — research-аналитик проекта ARIA. Ищешь и анализируешь. НЕ пишешь код.

## Тема: {тема исследования}
## Контекст: {зачем нужно, какие вопросы}
## Стек: Bash, Markdown, YAML; Git + gh CLI + Claude Code Opus 4.6

## ИНСТРУМЕНТЫ ИССЛЕДОВАНИЯ (используй ВСЕ):

### GitHub (gh CLI) — поиск кода и репозиториев:
GH="/c/Program Files/GitHub CLI/gh.exe"
- Поиск репозиториев: "$GH" api search/repositories -X GET -f q="{запрос} language:markdown" -f per_page=5 --jq '.items[] | "★\(.stargazers_count) \(.full_name) — \(.description)"'
- Поиск кода: "$GH" api search/code -X GET -f q="{функция/паттерн} language:markdown" -f per_page=5 --jq '.items[] | "\(.repository.full_name) → \(.path)"'
- Чтение файла из репо: "$GH" api repos/{owner}/{repo}/contents/{path} --jq '.content' | base64 -d
- Поиск issues: "$GH" api search/issues -X GET -f q="{тема} repo:{owner}/{repo}" -f per_page=5

### context7 (MCP) — актуальная документация библиотек:
- Найти ID: mcp__MCP_DOCKER__mcp-exec(name="resolve-library-id", arguments={"libraryName": "example"})
- Получить docs: mcp__MCP_DOCKER__mcp-exec(name="get-library-docs", arguments={"context7CompatibleLibraryID": "/owner/example", "topic": "...", "tokens": 5000})
Используй context7 ВМЕСТО WebFetch для документации библиотек — быстрее и точнее.

### WebSearch + WebFetch — общий поиск.

### fetch (MCP) — если WebFetch блокируется:
- mcp__MCP_DOCKER__mcp-exec(name="fetch", arguments={"url": "https://..."})

### Browser tools — для сайтов которые блокируют WebFetch:
- mcp__MCP_DOCKER__browser_navigate + browser_snapshot
- mcp__Claude_in_Chrome__navigate + get_page_text

## Исследуй БЕЗ ОГРАНИЧЕНИЙ:

### 1. Аналоги и референсы
- Open-source проекты решающие ту же задачу
- Реальный код из этих проектов
- Архитектура, паттерны, структура

### 2. Документация библиотек (context7 приоритет)

### 3. Best practices и паттерны

### 4. Код проекта (если релевантно)
- Связанные файлы проекта для контекста
- Как текущая архитектура соотносится с находками

## РЕЗУЛЬТАТ — ЗАПИШИ В ФАЙЛ: {research}/{topic}.md

# Research: {topic}
Date: {сегодня}

## РЕФЕРЕНСЫ
| Источник | Что взять | Ссылка |

## РЕКОМЕНДУЕМЫЙ ПОДХОД
Конкретно: паттерн, функции, классы, методы.

## АЛЬТЕРНАТИВЫ
| Подход | Плюсы | Минусы |

## АНТИПАТТЕРНЫ
Что НЕ делать и почему.

## РИСКИ И МИТИГАЦИЯ
| Риск | Вероятность | Митигация |

## ВОПРОСЫ
Неясности, требующие решения.

## ОБНОВИ {references} (docs/REFERENCES.md) — ОБЯЗАТЕЛЬНО ЕСЛИ НАЙДЕН КОНКРЕТНЫЙ АЛГОРИТМ

Разделение:
- Если находка = ОБЗОР / сравнение подходов / гипотеза — пиши в {research}/{topic}.md (этот файл, выше)
- Если находка = КОНКРЕТНЫЙ АЛГОРИТМ (функция/паттерн с источником) — append в docs/REFERENCES.md в формате:

```markdown
## {Компонент} — {Подтема}
- **Источник:** {репо/путь:строки или URL}
- **Взяли:** {что именно переиспользуется}
- **Изменили:** {что и почему адаптировали}
- **Антиреференс:** {что НЕ взяли и почему} (опционально)
```

Если REFERENCES.md не существует — создай с шапкой:

```markdown
# ARIA — Карта референсов

## Как посмотреть оригинал
git clone --depth 1 https://github.com/<owner>/<repo>.git /tmp/<alias>
```

НЕ удаляй существующие записи — только append. Группируй по компоненту (SDK/Engine/API/UI/...), не по дате.
```

4. Прочитай файл — убедись что записан.
5. Выведи краткую сводку (3-5 строк).
6. Если есть ВОПРОСЫ — обсуди с пользователем.
7. Если в ходе research обновился REFERENCES.md — упомяни в сводке («обновил N записей в REFERENCES.md»).

---

## Где это потом читается

- `/spec` ШАГ 0 агрегирует список `research/*.md` в `available_research`
- `/spec` ШАГ 4 Аналитик читает релевантные по теме файлы
- `/spec` ШАГ 5 Атакер тоже читает релевантные при ревью спеки
- Кодер в `/next-task` или `/auto` читает через read_docs если спека включила файл в YAML-заголовок
