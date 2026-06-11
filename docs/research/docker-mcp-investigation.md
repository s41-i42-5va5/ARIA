# Docker + MCP Server — исследование (R8)

> **Дата:** 2026-04-16
> **Контекст:** Phase 2 аудит задача R8 — MCP-сервер для Docker в ARIA-адаптерах

## Проблема

Адаптеры ARIA (python-fastapi, kotlin-android, csharp-avalonia) декларируют MCP-серверы в `adapter.yaml`. Для python-fastapi есть 4 MCP-сервера, но Docker-related MCP отсутствует.

## Текущее состояние

- `adapters/python-fastapi/adapter.yaml` — 4 MCP-сервера (postgres, filesystem, web, context7)
- Docker управляется через `docker compose` в bash-командах `/auto` и `/next-task`
- Нет MCP-сервера для управления Docker контейнерами из Claude Code

## Варианты

### 1. MCP Docker Server (community)
- `@anthropic/mcp-docker` или аналог
- Позволяет Claude видеть состояние контейнеров, логи, перезапускать
- Плюс: нативная интеграция с Claude Code
- Минус: зависимость от стороннего MCP-сервера, потенциальные проблемы безопасности

### 2. Bash-обёртки в инфра-проверке (текущий подход)
- `docker compose ps`, `docker compose up -d`
- Плюс: простой, работает, не требует дополнительных зависимостей
- Минус: нет реактивного мониторинга, только проверка при запуске /auto

### 3. Декларация в adapter.yaml как опциональный MCP
- Добавить `docker` в `mcp_servers` с `required: false`
- При `/aria-init` — предложить установку если Docker используется
- Плюс: стандартизация через ARIA-инфраструктуру

## Рекомендация

Вариант 2 (текущий) достаточен для ARIA. Docker MCP — опциональное улучшение для форков с тяжёлой Docker-инфраструктурой. Можно добавить в `adapter.yaml` как `required: false` при наличии стабильного community MCP-сервера.

## Статус

Исследование завершено. Действий не требуется. При появлении стабильного Docker MCP — добавить в python-fastapi адаптер.
