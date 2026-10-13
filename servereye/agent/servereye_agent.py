#!/usr/bin/env python3
"""ServerEye Agent — сбор метрик нагрузки и отправка их в ServerEye.

Устанавливается на сам сервер. Собирает только метрики (CPU, RAM, диск, сеть,
uptime, температуры CPU/GPU, батарея) и отправляет их по HTTPS/HTTP с персональным токеном сервера.
Агент не выполняет никаких команд и не требует root/Administrator.

Запуск:
    python servereye_agent.py --server https://servereye.example.com --token SE-xxxx

Переменные окружения (альтернатива аргументам):
    SERVEREYE_URL, SERVEREYE_TOKEN, SERVEREYE_INTERVAL, SERVEREYE_DISK
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import socket
import sys
import time
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    print("Требуется psutil:  pip install psutil httpx", file=sys.stderr)
    raise SystemExit(1)

try:
    import httpx
except ImportError:  # pragma: no cover
    print("Требуется httpx:  pip install psutil httpx", file=sys.stderr)
    raise SystemExit(1)

VERSION = "1.1"
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10

log = logging.getLogger("servereye-agent")
_running = True


# --------------------------------------------------------------------- сбор ---
def detect_os() -> str:
    system = platform.system()
    if system == "Linux":
        try:
            data = {}
            with open("/etc/os-release", encoding="utf-8") as handle:
                for line in handle:
                    if "=" in line:
                        key, _, value = line.partition("=")
                        data[key.strip()] = value.strip().strip('"')
            name = data.get("PRETTY_NAME") or data.get("NAME")
            if name:
                return name
        except OSError:
            pass
        return f"Linux {platform.release()}"
    if system == "Windows":
        release = platform.win32_ver()[0]
        edition = ""
        try:
            edition = platform.win32_edition() or ""
        except Exception:  # noqa: BLE001
            pass
        return f"Windows {release} {edition}".strip()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{system} {platform.release()}"


def default_disk_path() -> str:
    return "C:\\" if platform.system() == "Windows" else "/"


class NetCounter:
    """Считает мгновенную скорость сети по разнице счётчиков."""

    def __init__(self) -> None:
        self.last_time: float | None = None
        self.last_recv = 0
        self.last_sent = 0

    def rates(self) -> tuple[float | None, float | None, int, int]:
        counters = psutil.net_io_counters()
        now = time.monotonic()
        recv, sent = counters.bytes_recv, counters.bytes_sent
        down = up = None
        if self.last_time is not None:
            elapsed = now - self.last_time
            if elapsed > 0:
                down = max(0.0, (recv - self.last_recv) * 8 / elapsed / 1_000_000)
                up = max(0.0, (sent - self.last_sent) * 8 / elapsed / 1_000_000)
        self.last_time, self.last_recv, self.last_sent = now, recv, sent
        return down, up, recv, sent


# Датчики, которые не относятся к процессору
_NON_CPU_SENSORS = ("amdgpu", "nouveau", "radeon", "i915", "xe", "nvme", "iwlwifi",
                    "ath", "mt7", "wifi", "bat", "pch_", "spd", "drivetemp")
# Приоритет «честных» датчиков процессора
_CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal",
                "cpu-thermal", "thinkpad", "dell_smm", "acpitz")


def _sensor_readings() -> dict:
    getter = getattr(psutil, "sensors_temperatures", None)
    if getter is None:
        return {}
    try:
        return getter() or {}
    except Exception:  # noqa: BLE001
        return {}


def read_cpu_temperature(readings: dict | None = None) -> float | None:
    """Температура процессора: пакет целиком, а не случайный датчик."""
    readings = readings if readings is not None else _sensor_readings()
    for chip in _CPU_SENSORS:
        entries = readings.get(chip)
        if not entries:
            continue
        # Intel: «Package id 0», AMD: «Tdie» точнее «Tctl» (у Tctl бывает смещение)
        for wanted in ("package id 0", "tdie", "tctl", "cpu"):
            for entry in entries:
                if (entry.label or "").lower().startswith(wanted) and entry.current:
                    return round(entry.current, 1)
        values = [e.current for e in entries if e.current]
        if values:
            return round(max(values), 1)
    for chip, entries in readings.items():
        if chip.lower().startswith(_NON_CPU_SENSORS):
            continue
        values = [e.current for e in entries if e.current]
        if values:
            return round(max(values), 1)
    return None


def _nvidia_sleeping() -> bool:
    """Спит ли дискретная NVIDIA (runtime D3).

    Любой вызов nvidia-smi будит карту, поэтому спящую не трогаем —
    иначе агент сам не давал бы ей экономить энергию и греться меньше.
    """
    base = "/sys/bus/pci/devices"
    try:
        devices = os.listdir(base)
    except OSError:
        return False
    for dev in devices:
        try:
            with open(f"{base}/{dev}/vendor", encoding="utf-8") as handle:
                if handle.read().strip() != "0x10de":
                    continue
            with open(f"{base}/{dev}/class", encoding="utf-8") as handle:
                if not handle.read().strip().startswith("0x03"):
                    continue
            with open(f"{base}/{dev}/power/runtime_status", encoding="utf-8") as handle:
                return handle.read().strip() == "suspended"
        except OSError:
            continue
    return False


def read_gpu_temperature(readings: dict | None = None) -> tuple[float | None, str | None]:
    """(температура, название) видеокарты. NVIDIA — через nvidia-smi.

    Для спящей NVIDIA возвращает (None, "NVIDIA · сон").
    """
    import shutil
    import subprocess

    if shutil.which("nvidia-smi") and platform.system() == "Linux" and _nvidia_sleeping():
        return None, "NVIDIA · сон"
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout.strip().splitlines()
            if out:
                temp, _, name = out[0].partition(",")
                return round(float(temp), 1), name.strip() or "NVIDIA"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    readings = readings if readings is not None else _sensor_readings()
    for chip, title in (("amdgpu", "AMD Radeon"), ("radeon", "AMD Radeon"),
                        ("nouveau", "NVIDIA (nouveau)"), ("i915", "Intel Graphics"),
                        ("xe", "Intel Graphics")):
        entries = readings.get(chip)
        if not entries:
            continue
        for entry in entries:
            if (entry.label or "").lower() in ("edge", "junction", "gpu", "") and entry.current:
                return round(entry.current, 1), title
    return None, None


def _mains_online() -> bool | None:
    """Подключён ли сетевой адаптер — по типу «Mains», а не по имени.

    psutil ищет только AC/AC0, а у многих ноутбуков адаптер называется
    ACAD, ADP1 и т.п. Без этого при статусе батареи «Not charging»
    детектор отключения света не видел бы состояние сети.
    """
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        try:
            with open(f"{base}/{name}/type", encoding="utf-8") as handle:
                if handle.read().strip() != "Mains":
                    continue
            with open(f"{base}/{name}/online", encoding="utf-8") as handle:
                return handle.read().strip() == "1"
        except OSError:
            continue
    return None


def read_battery() -> dict[str, Any]:
    getter = getattr(psutil, "sensors_battery", None)
    if getter is None:
        return {}
    try:
        battery = getter()
    except Exception:  # noqa: BLE001
        return {}
    if battery is None:
        return {}
    plugged = _mains_online() if platform.system() == "Linux" else None
    if plugged is None and battery.power_plugged is not None:
        plugged = bool(battery.power_plugged)
    data: dict[str, Any] = {
        "battery_percent": round(float(battery.percent), 1),
        "power_plugged": plugged,
    }
    secs = battery.secsleft
    if isinstance(secs, (int, float)) and secs > 0 and secs not in (
        psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN
    ):
        data["battery_secs_left"] = float(secs)
    return data


def collect(net: NetCounter, disk_path: str) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(disk_path)
    down, up, total_recv, total_sent = net.rates()

    # psutil.used считает по-разному на Linux, Windows и macOS (кэш, wired, shared).
    # Чтобы «использовано» всегда сходилось с процентом, берём total - available.
    available = float(getattr(memory, "available", memory.free))
    used = max(0.0, float(memory.total) - available)

    payload: dict[str, Any] = {
        "version": VERSION,
        "os": detect_os(),
        "hostname": socket.gethostname(),
        "cpu_percent": round(psutil.cpu_percent(interval=1.0), 1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_total_mb": round(memory.total / 1024 / 1024, 1),
        "ram_used_mb": round(used / 1024 / 1024, 1),
        "ram_free_mb": round(memory.free / 1024 / 1024, 1),
        "ram_available_mb": round(available / 1024 / 1024, 1),
        "ram_percent": round(memory.percent, 2),
        "disk_total_bytes": float(disk.total),
        "disk_used_bytes": float(disk.used),
        "disk_free_bytes": float(disk.free),
        "disk_percent": round(disk.percent, 1),
        "uptime_seconds": round(time.time() - psutil.boot_time(), 0),
        "process_count": len(psutil.pids()),
    }

    try:
        swap = psutil.swap_memory()
        payload["swap_percent"] = round(swap.percent, 1)
    except Exception:  # noqa: BLE001
        pass

    # Load Average есть только на Unix — на Windows psutil его эмулирует,
    # поэтому там мы его сознательно не отправляем.
    if platform.system() != "Windows" and hasattr(os, "getloadavg"):
        try:
            load1, load5, load15 = os.getloadavg()
            payload.update({"load_1": round(load1, 2), "load_5": round(load5, 2), "load_15": round(load15, 2)})
        except OSError:
            pass

    if down is not None:
        payload["net_down_mbps"] = round(down, 2)
        payload["net_up_mbps"] = round(up or 0, 2)
    payload["net_total_recv_bytes"] = float(total_recv)
    payload["net_total_sent_bytes"] = float(total_sent)

    readings = _sensor_readings()
    cpu_temp = read_cpu_temperature(readings)
    if cpu_temp is not None:
        payload["temperature_c"] = cpu_temp
    gpu_temp, gpu_name = read_gpu_temperature(readings)
    if gpu_temp is not None:
        payload["gpu_temp_c"] = gpu_temp
    if gpu_name:
        payload["gpu_name"] = gpu_name
    payload.update(read_battery())
    return payload


# ------------------------------------------------------------------ отправка ---
def send(client: httpx.Client, url: str, token: str, payload: dict[str, Any]) -> bool:
    try:
        response = client.post(
            url,
            json=payload,
            headers={"X-Agent-Token": token, "User-Agent": f"ServerEye-Agent/{VERSION}"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        log.warning("Сеть недоступна: %s", type(exc).__name__)
        return False

    if response.status_code == 200:
        return True
    if response.status_code == 401:
        log.error("Токен отклонён сервером. Проверьте --token (возможно, он перевыпущен).")
    elif response.status_code == 429:
        log.warning("Слишком частая отправка — увеличьте интервал.")
    else:
        log.warning("Сервер ответил %s: %s", response.status_code, response.text[:200])
    return False


def verify(client: httpx.Client, base: str, token: str) -> bool:
    try:
        response = client.get(
            f"{base}/api/v1/agent/check",
            headers={"X-Agent-Token": token},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        log.warning("Не удалось связаться с ServerEye: %s", type(exc).__name__)
        return False
    if response.status_code == 200:
        data = response.json()
        log.info("Подключено к ServerEye. Сервер: %s", data.get("server"))
        return True
    log.error("Проверка токена не пройдена (%s)", response.status_code)
    return False


def _stop(signum, frame) -> None:  # noqa: ARG001
    global _running
    _running = False
    log.info("Получен сигнал остановки")


def main() -> int:
    parser = argparse.ArgumentParser(description="ServerEye Agent")
    parser.add_argument("--server", default=os.getenv("SERVEREYE_URL"), help="URL ServerEye, например http://1.2.3.4:8770")
    parser.add_argument("--token", default=os.getenv("SERVEREYE_TOKEN"), help="Токен агента (SE-...)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("SERVEREYE_INTERVAL", DEFAULT_INTERVAL)))
    parser.add_argument("--disk", default=os.getenv("SERVEREYE_DISK") or default_disk_path())
    parser.add_argument("--once", action="store_true", help="Отправить метрики один раз и выйти")
    parser.add_argument("--insecure", action="store_true", help="Не проверять TLS-сертификат")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.server or not args.token:
        parser.error("Нужны --server и --token (или переменные SERVEREYE_URL / SERVEREYE_TOKEN)")
    if not args.token.startswith("SE-"):
        parser.error("Токен должен начинаться с SE-")

    base = args.server.rstrip("/")
    if "://" not in base:
        base = "http://" + base
    url = f"{base}/api/v1/metrics"
    interval = max(MIN_INTERVAL, args.interval)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("ServerEye Agent %s | %s | %s", VERSION, detect_os(), socket.gethostname())
    log.info("Отправка метрик на %s каждые %s сек", url, interval)

    net = NetCounter()
    backoff = interval
    with httpx.Client(verify=not args.insecure, follow_redirects=True) as client:
        verify(client, base, args.token)
        net.rates()  # первый замер, чтобы посчитать скорость сети
        while _running:
            started = time.monotonic()
            try:
                payload = collect(net, args.disk)
            except Exception as exc:  # noqa: BLE001
                log.exception("Не удалось собрать метрики: %s", exc)
                payload = None

            if payload is not None:
                if send(client, url, args.token, payload):
                    backoff = interval
                    log.debug("Метрики отправлены: CPU %.0f%%, RAM %.1f%%",
                              payload.get("cpu_percent", 0), payload.get("ram_percent", 0))
                else:
                    backoff = min(backoff * 2, 900)
                    log.info("Повтор через %s сек", backoff)

            if args.once:
                break

            elapsed = time.monotonic() - started
            sleep_for = max(1.0, backoff - elapsed)
            end = time.monotonic() + sleep_for
            while _running and time.monotonic() < end:
                time.sleep(0.5)

    log.info("Агент остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
