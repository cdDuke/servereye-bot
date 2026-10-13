#!/usr/bin/env bash
# Полное удаление ServerEye с сервера. Запускается на сервере от root.
# Системные пакеты не удаляются — они могут быть нужны другому софту.
set -uo pipefail

APP_DIR=/opt/servereye
SERVICE_USER=servereye

echo "==> Останавливаю сервисы"
for unit in servereye-agent servereye; do
    systemctl disable --now "$unit" >/dev/null 2>&1 && echo "    $unit остановлен" || true
    rm -f "/etc/systemd/system/${unit}.service"
done
systemctl daemon-reload
systemctl reset-failed servereye servereye-agent >/dev/null 2>&1 || true

echo "==> Удаляю файлы"
rm -rf "$APP_DIR"
rm -f /etc/servereye-agent.env /etc/polkit-1/rules.d/50-servereye-poweroff.rules

echo "==> Удаляю пользователя ${SERVICE_USER}"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    pkill -u "$SERVICE_USER" >/dev/null 2>&1 || true
    userdel "$SERVICE_USER" >/dev/null 2>&1 && echo "    удалён"
fi

echo "==> Правило файрвола"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "8787/tcp"; then
    ufw delete allow 8787/tcp >/dev/null 2>&1 && echo "    правило 8787/tcp удалено"
else
    echo "    правил не найдено"
fi

echo
echo "==> Проверка"
systemctl list-unit-files 'servereye*' --no-legend 2>/dev/null | grep -q . && echo "    ⚠️ юниты остались" || echo "    юнитов нет"
[ -e "$APP_DIR" ] && echo "    ⚠️ $APP_DIR остался" || echo "    $APP_DIR удалён"
id -u "$SERVICE_USER" >/dev/null 2>&1 && echo "    ⚠️ пользователь остался" || echo "    пользователя нет"
ss -ltn 2>/dev/null | grep -q ":8787 " && echo "    ⚠️ порт 8787 занят" || echo "    порт 8787 свободен"
