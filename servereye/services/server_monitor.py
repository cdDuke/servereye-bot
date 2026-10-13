"""Проверка доступности серверов: ICMP ping, HTTP(S), TCP-порты.

Модуль не знает про Telegram и БД — он только выполняет проверки и возвращает
структуру CheckResult. Это позволяет позже добавить новые типы проверок
(например, опрос ServerEye Agent или SNMP) без переписывания остального кода.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import platform
import re
import time
from dataclasses import dataclass, field

import httpx

from ..config import get_settings
from ..database.models import (
    CHECK_HTTP,
    CHECK_PING,
    CHECK_PING_HTTP,
    CHECK_TCP,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_WARNING,
)

log = logging.getLogger(__name__)
settings = get_settings()

_RTT_RE = re.compile(
    r"=\s*([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))?\s*ms", re.IGNORECASE
)
_LOSS_RE = re.compile(r"([\d.]+)%\s*(?:packet\s*)?loss", re.IGNORECASE)
_WIN_TIME_RE = re.compile(r"(?:Average|Среднее)\s*=\s*(\d+)\s*ms", re.IGNORECASE)
_WIN_LOSS_RE = re.compile(r"\((\d+)%\s*(?:loss|потерь)", re.IGNORECASE)


@dataclass
class PingResult:
    ok: bool
    ping: float | None = None
    ping_min: float | None = None
    ping_max: float | None = None
    ping_stddev: float | None = None
    packet_loss: float | None = None
    error: str | None = None


PING_TARGET_NAMES = {
    "8.8.8.8": "Google",
    "8.8.4.4": "Google",
    "1.1.1.1": "Cloudflare",
    "1.0.0.1": "Cloudflare",
    "77.88.8.8": "Яндекс",
    "77.88.8.1": "Яндекс",
    "9.9.9.9": "Quad9",
}


def ping_target_title(target: str | None) -> str:
    if not target:
        return "—"
    name = PING_TARGET_NAMES.get(target)
    return f"{name} ({target})" if name else target


@dataclass
class HttpResult:
    ok: bool
    status_code: int | None = None
    response_time: float | None = None
    error: str | None = None
    final_url: str | None = None


@dataclass
class TcpPortResult:
    port: int
    ok: bool
    response_time: float | None = None
    error: str | None = None


@dataclass
class CheckResult:
    status: str = STATUS_OFFLINE
    ping: float | None = None
    ping_min: float | None = None
    ping_max: float | None = None
    packet_loss: float | None = None
    response_time: float | None = None
    http_status: int | None = None
    tcp_ok: bool | None = None
    tcp_detail: str | None = None
    connection_lost: bool = False
    error: str | None = None
    ping_target: str | None = None
    ports: list[TcpPortResult] = field(default_factory=list)

    @property
    def is_up(self) -> bool:
        return self.status in (STATUS_ONLINE, STATUS_WARNING)


# --------------------------------------------------------------------- ICMP ---
def _ping_cmd(host: str, count: int, timeout: float) -> list[str]:
    system = platform.system().lower()
    if system == "windows":
        return ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    if system == "darwin":
        # на macOS -W задаётся в миллисекундах
        return ["ping", "-c", str(count), "-W", str(int(timeout * 1000)), host]
    return ["ping", "-c", str(count), "-W", str(max(1, int(timeout))), "-i", "0.3", host]


def _parse_ping(output: str) -> PingResult:
    loss: float | None = None
    match = _LOSS_RE.search(output) or _WIN_LOSS_RE.search(output)
    if match:
        loss = float(match.group(1))

    rtt = _RTT_RE.search(output)
    if rtt:
        return PingResult(
            ok=(loss or 0) < 100,
            ping=float(rtt.group(2)),
            ping_min=float(rtt.group(1)),
            ping_max=float(rtt.group(3)),
            ping_stddev=float(rtt.group(4)) if rtt.group(4) else None,
            packet_loss=loss,
        )
    win = _WIN_TIME_RE.search(output)
    if win:
        value = float(win.group(1))
        return PingResult(ok=(loss or 0) < 100, ping=value, ping_min=value, ping_max=value, packet_loss=loss)
    if loss is not None and loss >= 100:
        return PingResult(ok=False, packet_loss=loss, error="100% потеря пакетов")
    return PingResult(ok=False, packet_loss=loss, error="Нет ответа ICMP")


async def ping_host(
    host: str, count: int | None = None, timeout: float | None = None
) -> PingResult:
    """ICMP-ping через системную утилиту (не требует root, работает на Linux/macOS/Windows)."""
    count = count or settings.ping_count
    timeout = timeout or settings.ping_timeout
    cmd = _ping_cmd(host, count, timeout)
    deadline = count * (timeout + 1) + 5
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        log.warning("Утилита ping недоступна, переключаюсь на TCP-замер")
        return await _tcp_ping_fallback(host)
    except Exception as exc:  # noqa: BLE001
        return PingResult(ok=False, error=f"Не удалось запустить ping: {exc}")

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=deadline)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return PingResult(ok=False, packet_loss=100.0, error="Таймаут ping")
    output = stdout.decode(errors="ignore")
    result = _parse_ping(output)
    if not result.ok and result.ping is None and "unknown host" in output.lower():
        result.error = "Хост не найден (DNS)"
    return result


async def _tcp_ping_fallback(host: str, port: int = 443) -> PingResult:
    result = await check_tcp_port(host, port, timeout=settings.tcp_timeout)
    if result.ok:
        return PingResult(ok=True, ping=result.response_time, packet_loss=0.0)
    return PingResult(ok=False, packet_loss=100.0, error=result.error or "TCP недоступен")


async def ping_first_reachable(targets: list[str]) -> tuple[PingResult, str | None, list[str]]:
    """Пингует цели по очереди до первой ответившей.

    Возвращает (результат, кто ответил, кто не ответил). Запасные цели
    нужны, чтобы сбой одного сервиса не выглядел как пропажа интернета.
    """
    failed: list[str] = []
    last = PingResult(ok=False, error="Нет целей для пинга")
    for target in targets:
        last = await ping_host(target)
        if last.ok:
            return last, target, failed
        failed.append(target)
    return last, None, failed


# --------------------------------------------------------------------- HTTP ---
def build_http_url(host: str, port: int | None = None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    scheme = "https"
    if port in (80, 8080, 8000):
        scheme = "http"
    if port and port not in (80, 443):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


async def check_http(
    url: str, timeout: float | None = None, retries: int = 1
) -> HttpResult:
    timeout = timeout or settings.http_timeout
    last_error = "Неизвестная ошибка"
    for attempt in range(retries + 1):
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                verify=False,  # самоподписанные сертификаты не должны ронять мониторинг
                headers={"User-Agent": "ServerEye-Monitor/1.0"},
            ) as client:
                response = await client.get(url)
            elapsed = (time.perf_counter() - started) * 1000
            return HttpResult(
                ok=response.status_code < 400,
                status_code=response.status_code,
                response_time=round(elapsed, 1),
                final_url=str(response.url),
            )
        except httpx.TimeoutException:
            last_error = "Таймаут HTTP-запроса"
        except httpx.ConnectError as exc:
            last_error = f"Нет соединения: {type(exc).__name__}"
        except httpx.HTTPError as exc:
            last_error = f"HTTP-ошибка: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"Ошибка: {type(exc).__name__}"
        if attempt < retries:
            await asyncio.sleep(1.0)
    return HttpResult(ok=False, error=last_error)


# ---------------------------------------------------------------------- TCP ---
async def check_tcp_port(host: str, port: int, timeout: float | None = None) -> TcpPortResult:
    timeout = timeout or settings.tcp_timeout
    started = time.perf_counter()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        elapsed = (time.perf_counter() - started) * 1000
        return TcpPortResult(port=port, ok=True, response_time=round(elapsed, 1))
    except asyncio.TimeoutError:
        return TcpPortResult(port=port, ok=False, error="таймаут")
    except ConnectionRefusedError:
        return TcpPortResult(port=port, ok=False, error="соединение отклонено")
    except OSError as exc:
        return TcpPortResult(port=port, ok=False, error=exc.strerror or "недоступен")
    except Exception as exc:  # noqa: BLE001
        return TcpPortResult(port=port, ok=False, error=type(exc).__name__)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------- общая проверка ---
async def run_check(
    host: str,
    check_type: str,
    ports: list[int] | None = None,
    http_url: str | None = None,
    ping_warning: float = 100.0,
    ping_critical: float = 300.0,
    loss_warning: float = 5.0,
    ping_targets: list[str] | None = None,
) -> CheckResult:
    """Выполняет проверку сервера в соответствии с выбранным типом.

    ping_targets — пинговать не сам host, а эти адреса (для машины, на которой
    работает бот: так измеряется её интернет, а не 0 ms до самой себя).
    """
    result = CheckResult()
    via: dict[str, object] = {}

    async def ping_via_targets() -> PingResult:
        ping, target, failed = await ping_first_reachable(ping_targets or [])
        via["target"], via["failed"] = target, failed
        return ping

    ports = ports or []
    errors: list[str] = []
    up_signals: list[bool] = []

    do_ping = check_type in (CHECK_PING, CHECK_PING_HTTP) or check_type == CHECK_TCP
    do_http = check_type in (CHECK_HTTP, CHECK_PING_HTTP)
    do_tcp = check_type == CHECK_TCP or (ports and check_type != CHECK_HTTP)

    tasks: dict[str, asyncio.Task] = {}
    if do_ping:
        tasks["ping"] = asyncio.create_task(
            ping_via_targets() if ping_targets else ping_host(host)
        )
    if do_http:
        url = build_http_url(host, ports[0] if ports else None, http_url)
        tasks["http"] = asyncio.create_task(check_http(url, retries=1))
    if do_tcp and ports:
        tasks["tcp"] = asyncio.create_task(
            asyncio.gather(*[check_tcp_port(host, p) for p in ports])
        )

    if not tasks:  # страховка: всегда есть хотя бы ping
        tasks["ping"] = asyncio.create_task(ping_host(host))

    done = {}
    for key, task in tasks.items():
        try:
            done[key] = await task
        except Exception as exc:  # noqa: BLE001
            log.exception("Проверка %s для %s упала", key, host)
            errors.append(f"{key}: {type(exc).__name__}")

    ping_result: PingResult | None = done.get("ping")
    if ping_targets:
        result.ping_target = via.get("target")  # type: ignore[assignment]
        failed = via.get("failed") or []
        if ping_result is not None and ping_result.ok and failed:
            names = ", ".join(ping_target_title(t) for t in failed)  # type: ignore[union-attr]
            errors.append(f"не ответил {names}, замер через {ping_target_title(result.ping_target)}")
        elif ping_result is not None and not ping_result.ok:
            ping_result.error = "Нет интернета: не отвечают " + ", ".join(
                ping_target_title(t) for t in ping_targets
            )
    if ping_result is not None:
        result.ping = ping_result.ping
        result.ping_min = ping_result.ping_min
        result.ping_max = ping_result.ping_max
        result.packet_loss = ping_result.packet_loss
        if ping_result.error:
            errors.append(ping_result.error)
        # ICMP может быть заблокирован файрволом — это не приговор,
        # поэтому ping учитывается как сигнал только если других проверок нет
        if check_type in (CHECK_PING,):
            up_signals.append(ping_result.ok)

    http_result: HttpResult | None = done.get("http")
    if http_result is not None:
        result.http_status = http_result.status_code
        result.response_time = http_result.response_time
        if http_result.error:
            errors.append(http_result.error)
        up_signals.append(http_result.ok)

    tcp_results: list[TcpPortResult] | None = done.get("tcp")
    if tcp_results:
        result.ports = list(tcp_results)
        ok_ports = [p for p in tcp_results if p.ok]
        result.tcp_ok = len(ok_ports) > 0
        result.tcp_detail = ", ".join(
            f"{p.port}:{'✅' if p.ok else '❌'}" for p in tcp_results
        )[:255]
        if check_type == CHECK_TCP:
            up_signals.append(len(ok_ports) == len(tcp_results))
        if not result.response_time and ok_ports:
            result.response_time = min(
                (p.response_time for p in ok_ports if p.response_time is not None),
                default=None,
            )
        for p in tcp_results:
            if not p.ok:
                errors.append(f"порт {p.port}: {p.error}")

    # если для ping+http ICMP молчит, но HTTP отвечает — сервер жив
    if check_type == CHECK_PING_HTTP and ping_result is not None and not up_signals:
        up_signals.append(ping_result.ok)

    alive = any(up_signals) if up_signals else bool(ping_result and ping_result.ok)
    partial = up_signals and not all(up_signals)

    if not alive:
        result.status = STATUS_OFFLINE
        result.connection_lost = True
    elif (
        partial
        or (result.ping is not None and result.ping >= ping_critical)
        or (result.packet_loss or 0) >= max(loss_warning, 1)
        or (result.http_status is not None and result.http_status >= 400)
    ):
        result.status = STATUS_WARNING
    elif result.ping is not None and result.ping >= ping_warning:
        result.status = STATUS_WARNING
    else:
        result.status = STATUS_ONLINE

    if errors:
        result.error = "; ".join(errors)[:250]
    return result


async def run_checks_parallel(
    items: list[dict],
    concurrency: int | None = None,
    sink: dict[int, CheckResult] | None = None,
) -> dict[int, CheckResult]:
    """Параллельная проверка списка серверов с ограничением нагрузки.

    items: [{"id": 1, "host": ..., "check_type": ..., "ports": [...], ...}, ...]
    sink: словарь, который заполняется по мере готовности — удобно для
    отображения прогресса в интерфейсе.
    """
    limit = concurrency or settings.max_concurrent_checks
    semaphore = asyncio.Semaphore(max(1, limit))
    results: dict[int, CheckResult] = sink if sink is not None else {}

    async def worker(item: dict) -> None:
        async with semaphore:
            try:
                results[item["id"]] = await run_check(
                    host=item["host"],
                    check_type=item["check_type"],
                    ports=item.get("ports"),
                    http_url=item.get("http_url"),
                    ping_warning=item.get("ping_warning", 100.0),
                    ping_critical=item.get("ping_critical", 300.0),
                    loss_warning=item.get("loss_warning", 5.0),
                    ping_targets=item.get("ping_targets"),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Не удалось проверить сервер %s", item.get("host"))
                results[item["id"]] = CheckResult(
                    status=STATUS_OFFLINE, error=f"Внутренняя ошибка: {type(exc).__name__}"
                )

    await asyncio.gather(*(worker(item) for item in items))
    return results


def is_private_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host in ("localhost", "127.0.0.1")
