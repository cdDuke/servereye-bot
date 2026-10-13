# ServerEye Agent

Небольшой сервис, который собирает метрики нагрузки сервера и отправляет их
в ServerEye. Только сбор данных: агент не выполняет команды, не открывает
портов и не требует root / Administrator.

## Что отправляет

CPU (% и число ядер), Load Average (Linux/macOS), RAM (total / used / free /
available / %), диск (total / used / free / %), сетевой трафик (↓/↑ Mbps и
суммарные счётчики), uptime, число процессов, температура (если доступна),
имя хоста и версия ОС.

## Установка

```bash
pip install -r requirements.txt        # psutil, httpx
python servereye_agent.py --server http://ВАШ_АДРЕС:8787 --token SE-xxxxxxxxxxxxxxxx
```

Токен выдаёт бот: **🔐 Agent** на карточке сервера.

## Аргументы

| Аргумент | Переменная окружения | По умолчанию |
|---|---|---|
| `--server` | `SERVEREYE_URL` | — |
| `--token` | `SERVEREYE_TOKEN` | — |
| `--interval` | `SERVEREYE_INTERVAL` | `60` секунд (минимум 10) |
| `--disk` | `SERVEREYE_DISK` | `/` или `C:\` |
| `--once` | — | отправить один раз и выйти |
| `--insecure` | — | не проверять TLS-сертификат |
| `--verbose` | — | подробный лог |

## Поведение при сбоях

Если ServerEye недоступен, агент повторяет отправку с нарастающей паузой
(до 15 минут) и продолжает работать. Бот, не получая heartbeat дольше
`AGENT_OFFLINE_AFTER_SECONDS`, отдельно сообщит «Agent не отвечает» — это
не то же самое, что «сервер недоступен».
