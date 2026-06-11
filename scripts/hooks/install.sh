#!/usr/bin/env bash
# Установка git hooks для ARIA upstream
# Запуск: bash scripts/hooks/install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Установка ARIA git hooks..."
echo "Репо: $REPO_ROOT"

# Настраиваем core.hooksPath → scripts/hooks
# Это позволяет hooks жить в репе (в .git/hooks — нет)
git -C "$REPO_ROOT" config core.hooksPath scripts/hooks

# Делаем скрипты исполняемыми (на Unix/macOS, пропускаем на Windows)
OS_TYPE="$(uname -s 2>/dev/null || echo "Unknown")"
case "$OS_TYPE" in
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows — chmod не требуется"
        ;;
    *)
        chmod +x "$SCRIPT_DIR/pre-commit" "$SCRIPT_DIR/commit-msg" "$SCRIPT_DIR/pre-push"
        ;;
esac

echo "Готово. Активные hooks:"
echo "  - pre-commit: валидация CHANGELOG_POLICY, DOCUMENTATION_LIFECYCLE"
echo "  - commit-msg: валидация COMMIT_POLICY (формат сообщения)"
echo "  - pre-push: проверка CHANGELOG при push в upstream"
echo ""
echo "Для обхода: git commit --no-verify (только для экстренных случаев)"
echo "Отключить hooks: git config --unset core.hooksPath"
