#!/usr/bin/env bash
# Установка ServerEye (Debian/Ubuntu). Запускается на самом сервере от root.
#
# Режимы определяются по .env:
#   AGENT_API_HOST=127.0.0.1  → «локальный»: бот и агент на одной машине,
#                               наружу ничего не открывается, ufw не трогаем;
#   иначе                     → «публичный»: API агента доступен извне.
set -euo pipefail

APP_DIR=/opt/servereye
SERVICE_USER=servereye
SKIP_START="${SKIP_START:-0}"
cd "$APP_DIR"

env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
set_env() {
    if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}

[ -f .env ] || { echo "❌ Нет $APP_DIR/.env"; exit 1; }
API_HOST="$(env_get AGENT_API_HOST)"; API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="$(env_get AGENT_API_PORT)"; API_PORT="${API_PORT:-8787}"
POWER="$(env_get POWER_CONTROL_ENABLED)"; POWER="${POWER:-false}"
ADMIN_ID="$(env_get ADMIN_IDS | cut -d, -f1 | tr -d ' ')"
LOCAL_MODE=false
case "$API_HOST" in 127.0.0.1|localhost) LOCAL_MODE=true ;; esac

echo "==> Режим: $([ "$LOCAL_MODE" = true ] && echo локальный || echo публичный)"

echo "==> Пакеты (ставлю только недостающие)"
NEED=()
python3 -c "import ensurepip" 2>/dev/null || NEED+=(python3-venv)
command -v ping >/dev/null 2>&1 || NEED+=(iputils-ping)
command -v curl >/dev/null 2>&1 || NEED+=(curl)
if [ "${#NEED[@]}" -gt 0 ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${NEED[@]}" >/dev/null
    echo "    установлено: ${NEED[*]}"
else
    echo "    всё уже есть"
fi

echo "==> Системный пользователь ${SERVICE_USER}"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Виртуальное окружение ($(python3 -V))"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> Настройка .env"
if [ "$LOCAL_MODE" = true ]; then
    set_env AGENT_PUBLIC_URL "http://127.0.0.1:${API_PORT}"
    echo "    API агента только на 127.0.0.1:${API_PORT}, наружу не открыт"
else
    PUBLIC_IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
    set_env AGENT_PUBLIC_URL "http://${PUBLIC_IP}:${API_PORT}"
    echo "    AGENT_PUBLIC_URL=http://${PUBLIC_IP}:${API_PORT}"
fi
chmod 600 .env
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

if [ "$LOCAL_MODE" = false ]; then
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "${API_PORT}/tcp" >/dev/null 2>&1 || true
        echo "    ufw: открыт порт ${API_PORT}"
    fi
fi

echo "==> Права на выключение"
POLKIT_RULE=/etc/polkit-1/rules.d/50-servereye-poweroff.rules
if [ "$POWER" = true ]; then
    if [ -d /etc/polkit-1/rules.d ]; then
        install -m 644 deploy/50-servereye-poweroff.rules "$POLKIT_RULE"
        echo "    polkit: пользователю ${SERVICE_USER} разрешено только выключение"
    else
        echo "    ⚠️ polkit не найден — кнопка выключения работать не будет"
    fi
else
    rm -f "$POLKIT_RULE"
    echo "    отключено (POWER_CONTROL_ENABLED=false)"
fi

# Включает и (пере)запускает юнит — или только ставит его при SKIP_START=1
activate() {
    if [ "$SKIP_START" = 1 ]; then
        systemctl disable --now "$1" >/dev/null 2>&1 || true
        echo "    $1 установлен, но НЕ запущен (SKIP_START=1)"
    else
        systemctl enable "$1" >/dev/null 2>&1
        systemctl restart "$1"
    fi
}

echo "==> Сервис бота"
install -m 644 deploy/servereye.service /etc/systemd/system/servereye.service
systemctl daemon-reload
activate servereye

if [ "$LOCAL_MODE" = true ] && [ -n "$ADMIN_ID" ]; then
    echo "==> Мониторинг этой машины (агент)"
    NAME="${LOCAL_SERVER_NAME:-Домашний сервер}"
    TOKEN="$(sudo -u "$SERVICE_USER" ./.venv/bin/python deploy/bootstrap_local.py "$ADMIN_ID" "$NAME" | tail -1)"
    case "$TOKEN" in
        SE-*)
            umask 077
            printf "SERVEREYE_TOKEN=%s\nSERVEREYE_URL=http://127.0.0.1:%s\nSERVEREYE_INTERVAL=30\n" \
                "$TOKEN" "$API_PORT" > /etc/servereye-agent.env
            chmod 600 /etc/servereye-agent.env
            install -m 644 deploy/servereye-agent.service /etc/systemd/system/servereye-agent.service
            systemctl daemon-reload
            activate servereye-agent
            echo "    сервер «${NAME}» привязан к Telegram ID ${ADMIN_ID}"
            ;;
        *)
            echo "    ⚠️ не удалось получить токен агента: $TOKEN"
            ;;
    esac
fi

[ "$SKIP_START" = 1 ] || sleep 5
echo
echo "==> Статус"
systemctl is-active servereye servereye-agent 2>/dev/null || true
echo
journalctl -u servereye -n 12 --no-pager --output=cat || true
