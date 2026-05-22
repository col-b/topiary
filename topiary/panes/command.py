"""CommandPane: runs an arbitrary shell command and displays its stdout."""
from __future__ import annotations

import asyncio
import os

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig

# ANSI reset issued before each run so stale colour codes don't bleed over
_ANSI_RESET = "\x1b[0m"


class CommandPane(Widget):
    """Runs a shell command every `refresh` seconds and displays stdout."""

    DEFAULT_CSS = """
    CommandPane {
        border: round $panel-lighten-2;
        padding: 0;
        overflow: hidden scroll;
    }
    CommandPane > Static {
        width: 100%;
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
        self._running = False

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
        # first run immediately, then on interval
        self.run_worker(self._do_refresh(), exclusive=True, name=f"cmd-{self.pane_cfg.id}")
        self.set_interval(self.pane_cfg.refresh, self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        if not self._running:
            self.run_worker(
                self._do_refresh(), exclusive=True, name=f"cmd-{self.pane_cfg.id}"
            )

    async def _do_refresh(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            output = await self._run_command()
            text = Text.from_ansi(_ANSI_RESET + output)
            self.query_one("#output", Static).update(text)
        finally:
            self._running = False

    async def _run_command(self) -> str:
        if not self.pane_cfg.command:
            return "[dim]No command configured[/dim]"
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLUMNS": str(max(10, self.content_size.width)),
            "LINES": str(max(4, self.content_size.height)),
        }
        try:
            proc = await asyncio.create_subprocess_shell(
                self.pane_cfg.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            return "\x1b[31mCommand timed out after 30 s\x1b[0m"
        except Exception as exc:
            return f"\x1b[31mError: {exc}\x1b[0m"
