#!/usr/bin/env bash
# Запуск ServerEye. Логи — logs/servereye.log
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m servereye.main "$@"
