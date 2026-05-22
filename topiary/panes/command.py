"""CommandPane: runs any shell command in a PTY and displays its output."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig
from ..runner import TerminalRunner

_DEFAULT_RESTART_DELAY = 2.0  # seconds to wait before restarting after unexpected exit
_SCROLLABLE_ROWS = 500        # virtual PTY height for scrollable panes


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
        self._dirty: bool = False  # set by PTY callback; consumed by display timer

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
        # Render at configured Hz; the PTY reader just sets _dirty between frames.
        self.set_interval(1 / self._refresh_rate_hz, self._maybe_refresh)
        self.run_worker(self._run_loop(), exclusive=True, name=f"cmd-{self.pane_cfg.id}")

    # ------------------------------------------------------------------ #
    # Run loop                                                             #
    # ------------------------------------------------------------------ #

    async def _run_loop(self) -> None:
        """Start the command, wait for it to exit, restart after delay — forever."""
        try:
            while True:
                await self._run_once()
                # refresh > 0  →  explicit interval between restarts
                # refresh == 0 →  long-running command; brief safety delay before restart
                delay = self.pane_cfg.refresh if self.pane_cfg.refresh > 0 else _DEFAULT_RESTART_DELAY
                await asyncio.sleep(delay)
        finally:
            if self._runner:
                await self._runner.stop()
                self._runner = None

    async def _run_once(self) -> None:
        if self.pane_cfg.scrollable:
            rows, cols = _SCROLLABLE_ROWS, max(10, self.content_size.width) or 80
        else:
            rows = max(4, self.content_size.height) or 24
            cols = max(10, self.content_size.width) or 80
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
            self._runner = None
            await runner.stop()

    # ------------------------------------------------------------------ #
    # Display + resize                                                     #
    # ------------------------------------------------------------------ #

    def _mark_dirty(self) -> None:
        """Called by TerminalRunner on every PTY read — just flip the flag."""
        self._dirty = True

    def _maybe_refresh(self) -> None:
        """Timer callback: render only if new PTY data has arrived since last frame."""
        if not (self._dirty and self._runner):
            return
        self._dirty = False
        try:
            output = self.query_one("#output", Static)
            output.update(self._runner.render(trim_trailing=self.pane_cfg.scrollable))
            if self.pane_cfg.scrollable:
                scroller = self.query_one("#scroll", VerticalScroll)
                # Only auto-scroll if user hasn't manually scrolled up
                if scroller.is_vertical_scroll_end:
                    scroller.scroll_end(animate=False)
        except Exception:
            pass

    def on_resize(self) -> None:
        if self._runner:
            cols = max(10, self.content_size.width) or 80
            if self.pane_cfg.scrollable:
                # Keep the tall virtual rows; only sync the column width
                self._runner.resize(_SCROLLABLE_ROWS, cols)
            else:
                self._runner.resize(max(4, self.content_size.height) or 24, cols)
