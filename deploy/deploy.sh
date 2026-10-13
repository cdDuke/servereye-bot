#!/usr/bin/env bash
# Локальный деплой ServerEye на сервер по SSH.
#
#   ./deploy/deploy.sh <ssh-цель> [--with-db]
#   SKIP_START=1 ./deploy/deploy.sh server    # установить, но не запускать
#
# <ssh-цель> — всё, что понимает ssh: root@1.2.3.4 или алиас из ~/.ssh/config.
# Если пользователь не root, команды выполняются через sudo (нужен NOPASSWD).
#
# Конфигурация сервера берётся из deploy/.env.server — отдельный токен бота,
# чтобы копии не конфликтовали (два процесса с одним токеном Telegram не допускает).
set -euo pipefail

TARGET="${1:?Укажите цель, например root@1.2.3.4 или алиас из ~/.ssh/config}"
WITH_DB="${2:-}"
SKIP_START="${SKIP_START:-0}"
APP_DIR=/opt/servereye
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_ENV="$LOCAL_DIR/deploy/.env.server"

if [ ! -f "$SERVER_ENV" ]; then
    echo "❌ Нет файла deploy/.env.server (шаблон: deploy/.env.server.example)"
    exit 1
fi
if grep -q "ВСТАВЬТЕ" "$SERVER_ENV"; then
    echo "❌ В deploy/.env.server не заполнен BOT_TOKEN"
    exit 1
fi

SUDO="$(ssh "$TARGET" '[ "$(id -u)" = 0 ] || echo sudo -n')"
if [ -n "$SUDO" ] && ! ssh "$TARGET" "sudo -n true" 2>/dev/null; then
    echo "❌ Пользователю на сервере нужен sudo без пароля"
    exit 1
fi

EXCLUDES=(
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc'
  --exclude '.git' --exclude 'logs' --exclude '.DS_Store'
  --exclude '.env'
  --exclude 'deploy/.env.server'
)
if [ "$WITH_DB" != "--with-db" ]; then
  EXCLUDES+=(--exclude 'servereye.db' --exclude 'servereye.db-wal' --exclude 'servereye.db-shm')
fi

echo "==> Готовлю сервер (rsync)"
ssh "$TARGET" "command -v rsync >/dev/null 2>&1 || ($SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync >/dev/null)"

echo "==> Копирую проект в ${TARGET}:${APP_DIR}"
ssh "$TARGET" "$SUDO mkdir -p $APP_DIR"
rsync -az --delete "${EXCLUDES[@]}" --rsync-path="$SUDO rsync" "$LOCAL_DIR/" "$TARGET:$APP_DIR/"

echo "==> Загружаю серверный .env"
ssh "$TARGET" "umask 077; cat > /tmp/servereye.env.$$ && $SUDO install -m 600 /tmp/servereye.env.$$ $APP_DIR/.env; rm -f /tmp/servereye.env.$$" < "$SERVER_ENV"

echo "==> Устанавливаю"
ssh "$TARGET" "$SUDO env SKIP_START=$SKIP_START bash $APP_DIR/deploy/install_server.sh"

echo
echo "Готово. Полезные команды на сервере:"
echo "  systemctl status servereye servereye-agent"
echo "  journalctl -u servereye -f"
