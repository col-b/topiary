"""CommandPane: runs any shell command in a PTY and displays its output."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig
from ..runner import TerminalRunner

_DEFAULT_RESTART_DELAY = 2.0  # seconds to wait before restarting after unexpected exit


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
    """

    def __init__(
        self,
        pane_cfg: PaneConfig,
        *,
        show_border: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._show_border = show_border
        self._runner: TerminalRunner | None = None

    def compose(self) -> ComposeResult:
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
        rows = max(4, self.content_size.height) or 24
        cols = max(10, self.content_size.width) or 80
        runner = TerminalRunner(
            self.pane_cfg.command or "echo 'no command configured'",
            rows,
            cols,
        )
        self._runner = runner
        try:
            await runner.start(on_update=self._refresh_display, cwd=self.pane_cfg.cwd)
            await runner.wait()
        finally:
            self._runner = None
            await runner.stop()

    # ------------------------------------------------------------------ #
    # Display + resize                                                     #
    # ------------------------------------------------------------------ #

    def _refresh_display(self) -> None:
        if self._runner:
            try:
                self.query_one("#output", Static).update(self._runner.render())
            except Exception:
                pass

    def on_resize(self) -> None:
        if self._runner:
            self._runner.resize(
                max(4, self.content_size.height) or 24,
                max(10, self.content_size.width) or 80,
            )
