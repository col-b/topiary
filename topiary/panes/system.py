"""SystemPane: live CPU / memory / network stats using psutil."""
from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

import psutil
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig

if TYPE_CHECKING:
    pass

# Unicode block characters for sparklines (index 0 = empty, 8 = full)
_SPARK = " ▁▂▃▄▅▆▇█"
_HISTORY_LEN = 60  # samples kept for sparklines


def _fmt_bytes(n: float) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:6.1f} {unit}"
        n /= 1024.0
    return f"{n:6.1f} PB"


def _bar(pct: float, width: int) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _pct_style(pct: float) -> str:
    if pct < 60:
        return "bright_green"
    if pct < 85:
        return "yellow"
    return "bright_red"


class SystemPane(Widget):
    """Live system stats: CPU sparkline, memory bar, network rates."""

    DEFAULT_CSS = """
    SystemPane {
        border: round $panel-lighten-2;
        padding: 0 1;
        overflow: hidden;
    }
    SystemPane > Static {
        width: 100%;
    }
    """

    def __init__(
        self,
        pane_cfg: PaneConfig,
        *,
        start_immediately: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._active = start_immediately
        self._refresh_timer = None
        self._cpu_hist: deque[float] = deque(maxlen=_HISTORY_LEN)
        self._prev_net: psutil._common.snetio | None = None
        self._prev_net_time: float = 0.0
        # prime the one-shot non-blocking call
        psutil.cpu_percent(interval=None)

    def compose(self) -> ComposeResult:
        yield Static("", id="output", markup=False)

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        self.border_title = self.pane_cfg.title or self.pane_cfg.id
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
        if self._active:
            self._do_refresh()
        self._refresh_timer = self.set_interval(2, self._do_refresh, pause=not self._active)

    def activate(self) -> None:
        if self._active:
            return
        self._active = True
        if self._refresh_timer is not None:
            self._refresh_timer.resume()
            self._do_refresh()

    def deactivate(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._refresh_timer is not None:
            self._refresh_timer.pause()

    # ------------------------------------------------------------------ #
    # Refresh                                                              #
    # ------------------------------------------------------------------ #

    def _do_refresh(self) -> None:
        text = Text()
        text.append_text(self._cpu_section())
        text.append_text(self._mem_section())
        text.append_text(self._net_section())
        self.query_one("#output", Static).update(text)

    def _cpu_section(self) -> Text:
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        avg = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
        self._cpu_hist.append(avg)

        bar_w = max(10, self.content_size.width - 22)
        spark = "".join(_SPARK[min(8, int(v / 100 * 9))] for v in self._cpu_hist)
        spark = spark[-bar_w:]  # keep most recent bar_w chars

        t = Text()
        t.append("CPU  ", style="bold cyan")
        t.append(spark, style=_pct_style(avg))
        t.append(f"  {avg:5.1f}%\n")
        return t

    def _mem_section(self) -> Text:
        mem = psutil.virtual_memory()
        bar_w = max(10, self.content_size.width - 30)
        t = Text()
        t.append("MEM  ", style="bold cyan")
        t.append(f"[{_bar(mem.percent, bar_w)}]", style=_pct_style(mem.percent))
        t.append(f"  {mem.percent:5.1f}%  ")
        t.append(_fmt_bytes(mem.used), style="dim")
        t.append(" / ")
        t.append(_fmt_bytes(mem.total), style="dim")
        t.append("\n")
        return t

    def _net_section(self) -> Text:
        net = psutil.net_io_counters()
        now = time.monotonic()
        t = Text()
        t.append("NET  ", style="bold cyan")
        if self._prev_net is not None and now > self._prev_net_time:
            dt = now - self._prev_net_time
            rx = (net.bytes_recv - self._prev_net.bytes_recv) / dt
            tx = (net.bytes_sent - self._prev_net.bytes_sent) / dt
            t.append("↓ ", style="green")
            t.append(_fmt_bytes(rx), style="bright_green")
            t.append("/s   ")
            t.append("↑ ", style="magenta")
            t.append(_fmt_bytes(tx), style="bright_magenta")
            t.append("/s\n")
        else:
            t.append("(collecting...)\n", style="dim")
        self._prev_net = net
        self._prev_net_time = now
        return t
