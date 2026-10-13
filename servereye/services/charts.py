"""Графики для Telegram.

Картинки рисует matplotlib (в отдельном потоке, чтобы не блокировать event loop).
Если matplotlib не установлен или рендер упал — вызывающий код показывает
текстовый график, поэтому раздел «Графики» работает всегда.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

try:  # pragma: no cover - зависит от окружения
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    MATPLOTLIB_AVAILABLE = True
except Exception:  # noqa: BLE001
    MATPLOTLIB_AVAILABLE = False
    log.warning("matplotlib недоступен — графики будут текстовыми")

BG = "#0e1621"
FG = "#e6edf3"
GRID = "#22303c"
ACCENT = "#4aa3ff"
ACCENT2 = "#57d38c"
WARN = "#ffb02e"
CRIT = "#ff5c5c"


# Шрифт DejaVu не содержит эмодзи — они рисуются пустыми квадратами
_EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u200D]")


def _plain(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _style(ax, title: str, ylabel: str) -> None:
    title, ylabel = _plain(title), _plain(ylabel)
    ax.set_facecolor(BG)
    ax.set_title(title, color=FG, fontsize=13, pad=12)
    ax.set_ylabel(ylabel, color=FG, fontsize=10)
    ax.grid(True, color=GRID, linewidth=0.7, alpha=0.8)
    ax.tick_params(colors=FG, labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(GRID)


def _render(
    series: list[tuple[str, list[datetime], list[float], str]],
    title: str,
    ylabel: str,
    hours: int,
    threshold: tuple[float, str] | None = None,
) -> bytes | None:
    if not MATPLOTLIB_AVAILABLE:
        return None
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=130)
    fig.patch.set_facecolor(BG)
    _style(ax, title, ylabel)

    plotted = False
    for label, xs, ys, color in series:
        if not xs:
            continue
        plotted = True
        ax.plot(xs, ys, color=color, linewidth=2.0, label=_plain(label), marker="o", markersize=2.5)
        ax.fill_between(xs, ys, color=color, alpha=0.12)

    if not plotted:
        plt.close(fig)
        return None

    if threshold is not None:
        value, label = threshold
        ax.axhline(value, color=CRIT, linestyle="--", linewidth=1.2, alpha=0.8, label=_plain(label))

    if hours <= 24:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    elif hours <= 24 * 7:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate(rotation=0, ha="center")

    if len(series) > 1 or threshold is not None:
        legend = ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
        legend.get_frame().set_alpha(0.9)

    buffer = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", facecolor=BG)
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


def _render_bars(
    labels: list[str], values: list[float], title: str, ylabel: str, colors: list[str]
) -> bytes | None:
    if not MATPLOTLIB_AVAILABLE:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.0), dpi=130)
    fig.patch.set_facecolor(BG)
    _style(ax, title, ylabel)
    bars = ax.bar([_plain(x) for x in labels], values, color=colors, width=0.55)
    lo = min(values) if values else 0
    ax.set_ylim(max(0, lo - 2), 100.5)
    for rect, value in zip(bars, values):
        ax.text(
            rect.get_x() + rect.get_width() / 2,
            rect.get_height() + 0.1,
            f"{value:.2f}%",
            ha="center",
            color=FG,
            fontsize=10,
        )
    buffer = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", facecolor=BG)
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


async def line_chart(
    series: list[tuple[str, list[datetime], list[float], str]],
    title: str,
    ylabel: str,
    hours: int = 24,
    threshold: tuple[float, str] | None = None,
) -> bytes | None:
    try:
        return await asyncio.to_thread(_render, series, title, ylabel, hours, threshold)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось нарисовать график")
        return None


async def bar_chart(
    labels: list[str], values: list[float], title: str, ylabel: str, colors: list[str] | None = None
) -> bytes | None:
    colors = colors or [ACCENT if v >= 99 else WARN if v >= 95 else CRIT for v in values]
    try:
        return await asyncio.to_thread(_render_bars, labels, values, title, ylabel, colors)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось нарисовать график")
        return None
