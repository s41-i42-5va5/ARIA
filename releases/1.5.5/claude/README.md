# ARIA 1.5.5 for Claude Code — переносимая тестовая версия

Эту папку можно целиком скопировать на другой Windows 10/11 x64 ПК. Для ARIA не нужны
внешние Python и Git. MCP-сервер отсутствует.

До установки ARIA на тестовом ПК должны быть установлены и авторизованы Claude Code, а также
поддерживаемый Git Bash или WSL. Claude Code, Anthropic credentials и Git Bash/WSL в эту папку
не входят.

Быстрый запуск:

1. `CHECK_RELEASE.cmd`
2. `INSTALL.cmd`
3. `RUN_ARIA.cmd claude status`
4. `RUN_ARIA.cmd doctor`

`INSTALL.cmd` устанавливает текущему Windows-пользователю skill `aria-project` и защитный
`PreToolUse` hook. Пользовательские настройки Claude сохраняются.

Полная инструкция: [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md).

Реализованный функционал: [ФУНКЦИОНАЛ.md](ФУНКЦИОНАЛ.md).

Проверки поставки: [ПРОВЕРКА_ПОСТАВКИ.md](ПРОВЕРКА_ПОСТАВКИ.md).

Это локально проверенный test candidate. Живой запуск Claude Code и multi-user GitHub-приёмка
пока не выполнены и явно отмечены в manifest как `pending`.
