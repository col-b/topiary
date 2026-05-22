"""CommandPane: runs any shell command in a PTY and displays its output."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig
from ..runner import TerminalRunner

_SCROLLABLE_ROWS = 200  # virtual PTY height for scrollable panes

# Module-level perf registry updated by every CommandPane on each render.
# Keys are pane ids; read by PerfOverlay in app.py.
PANE_PERF: dict[str, dict[str, Any]] = {}


class CommandPane(Widget):
    """Runs a shell command in a PTY; restarts after exit per `refresh` setting."""

    DEFAULT_CSS = """
    CommandPane {
        border: round $panel-lighten-2;
        padding: 0;
        overflow: hidden;
    }
    CommandPane > Static {
        width: 1fr;
        height: 1fr;
    }
    CommandPane > VerticalScroll {
        width: 1fr;
        height: 1fr;
        scrollbar-size-vertical: 0;
    }
    CommandPane > VerticalScroll > Static {
        width: 100%;
        height: auto;
    }
    """

    def __init__(
        self,
        pane_cfg: PaneConfig,
        *,
        show_border: bool = True,
        refresh_rate_hz: float = 20.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._show_border = show_border
        self._refresh_rate_hz = max(1.0, refresh_rate_hz)
        self._runner: TerminalRunner | None = None
        self._dirty: bool = False
        # perf counters
        self._pty_updates: int = 0
        self._render_count: int = 0
        self._render_ms_total: float = 0.0
        self._render_ms_max: float = 0.0
        self._render_ms_last: float = 0.0
        self._render_ts: deque[float] = deque(maxlen=60)  # timestamps of recent renders

    def compose(self) -> ComposeResult:
        if self.pane_cfg.scrollable:
            with VerticalScroll(id="scroll"):
                yield Static("", id="output", markup=False)
        else:
            yield Static("", id="output", markup=False)

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        if self._show_border:
            self.border_title = self.pane_cfg.title or self.pane_cfg.id
        else:
            self.styles.border = ("none", "transparent")
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
        self.set_interval(1 / self._refresh_rate_hz, self._maybe_refresh)
        self.run_worker(self._run_loop(), exclusive=True, name=f"cmd-{self.pane_cfg.id}")

    def on_unmount(self) -> None:
        """Synchronously kill child process on widget removal or app exit.

        Called by Textual for every descendant when any ancestor is removed,
        so config-reload and 'q' both reliably clean up.
        """
        if self._runner is not None:
            self._runner.kill_sync()
            self._runner = None

    # ------------------------------------------------------------------ #
    # Run loop                                                             #
    # ------------------------------------------------------------------ #

    async def _run_loop(self) -> None:
        """Run once (refresh==0) or periodically (refresh>0)."""
        try:
            await self._run_once()
            while self.pane_cfg.refresh > 0:
                await asyncio.sleep(self.pane_cfg.refresh)
                await self._run_once()
        finally:
            # on_unmount may have already called kill_sync(); stop() is a no-op then.
            if self._runner is not None:
                await self._runner.stop()
                self._runner = None

    async def _run_once(self) -> None:
        if self.pane_cfg.scrollable:
            rows, cols = _SCROLLABLE_ROWS, max(10, self.content_size.width or 80)
        else:
            # Use sensible defaults when widget hasn't been laid out yet
            # (e.g. inactive tab has content_size (0,0))
            rows = max(4, self.content_size.height or 24)
            cols = max(10, self.content_size.width or 80)
        runner = TerminalRunner(
            self.pane_cfg.command or "echo 'no command configured'",
            rows,
            cols,
        )
        self._runner = runner
        try:
            await runner.start(on_update=self._mark_dirty, cwd=self.pane_cfg.cwd)
            await runner.wait()
        finally:
            # Fast commands (df -h, gcal) can exit before the first timer tick.
            # Do one final render here so output is never silently lost.
            if self._dirty:
                try:
                    text = runner.render(trim_trailing=self.pane_cfg.scrollable)
                    self.query_one("#output", Static).update(text)
                    self._dirty = False
                except Exception:
                    pass
            self._runner = None
            await runner.stop()

    # ------------------------------------------------------------------ #
    # Display + resize                                                     #
    # ------------------------------------------------------------------ #

    def _mark_dirty(self) -> None:
        """Called by TerminalRunner on every PTY read — O(1), just flip a flag."""
        self._dirty = True
        self._pty_updates += 1

    def _maybe_refresh(self) -> None:
        """Timer callback: render only if new PTY data has arrived since last frame."""
        if not self._dirty or self._runner is None:
            return
        self._dirty = False
        t0 = time.perf_counter()
        try:
            rich_text = self._runner.render(trim_trailing=self.pane_cfg.scrollable)
        except Exception:
            return
        t1 = time.perf_counter()
        try:
            output = self.query_one("#output", Static)
            if self.pane_cfg.scrollable:
                scroller = self.query_one("#scroll", VerticalScroll)
                at_bottom = scroller.is_vertical_scroll_end
                output.update(rich_text)
                if self.pane_cfg.follow and at_bottom:
                    scroller.scroll_end(animate=False)
            else:
                output.update(rich_text)
        except Exception:
            pass
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._record_perf(elapsed_ms, (t1 - t0) * 1000)

    def _record_perf(self, elapsed_ms: float, render_ms: float) -> None:
        self._render_count += 1
        self._render_ms_total += elapsed_ms
        self._render_ms_last = elapsed_ms
        if elapsed_ms > self._render_ms_max:
            self._render_ms_max = elapsed_ms
        now = time.monotonic()
        self._render_ts.append(now)
        cutoff = now - 10.0
        recent = sum(1 for t in self._render_ts if t >= cutoff)
        render_hz = recent / 10.0
        PANE_PERF[self.pane_cfg.id] = {
            "title": self.pane_cfg.title or self.pane_cfg.id,
            "pty_updates": self._pty_updates,
            "render_count": self._render_count,
            "render_ms_last": elapsed_ms,
            "render_ms_avg": self._render_ms_total / self._render_count,
            "render_ms_max": self._render_ms_max,
            "s2r_ms": render_ms,       # screen_to_rich() alone
            "update_ms": elapsed_ms - render_ms,  # Static.update() alone
            "render_hz": render_hz,
            "visible": self.visible,
        }

    def on_resize(self) -> None:
        if self._runner:
            cols = max(10, self.content_size.width) or 80
            if self.pane_cfg.scrollable:
                self._runner.resize(_SCROLLABLE_ROWS, cols)
            else:
                self._runner.resize(max(4, self.content_size.height) or 24, cols)
