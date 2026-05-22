"""Main Textual application."""
from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget

from .config import AppConfig, PaneConfig, load_config
from .panes.command import CommandPane
from .panes.system import SystemPane
from .panes.tabs import TabsPane


class TopiaryApp(App):
    """Topiary — a configurable TUI dashboard."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    .row {
        layout: horizontal;
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("r", "action_reload", "Reload config"),
    ]

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        super().__init__()
        self.config_data = config
        self.config_path = config_path

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def on_mount(self) -> None:
        self.title = self.config_data.title
        self.run_worker(self._watch_config(), exclusive=True, name="config-watcher")

    # ------------------------------------------------------------------ #
    # Composition                                                          #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        for i, row_cfg in enumerate(self.config_data.rows):
            panes: list[Widget] = []
            for pane_cfg in row_cfg.panes:
                pane = self._make_pane(pane_cfg)
                panes.append(pane)
            row = Horizontal(*panes, id=f"row-{i}", classes="row")
            row.styles.height = row_cfg.height
            yield row

    def _make_pane(self, pane_cfg: PaneConfig) -> Widget:
        match pane_cfg.type:
            case "system":
                return SystemPane(pane_cfg, id=pane_cfg.id)
            case "tabs":
                return TabsPane(pane_cfg, id=pane_cfg.id)
            case _:  # "command" and anything unknown
                return CommandPane(pane_cfg, id=pane_cfg.id)

    # ------------------------------------------------------------------ #
    # Config file watcher                                                  #
    # ------------------------------------------------------------------ #

    async def _watch_config(self) -> None:
        """Poll config file mtime; reload automatically when it changes."""
        try:
            last_mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        while True:
            await asyncio.sleep(1)
            try:
                mtime = self.config_path.stat().st_mtime
            except OSError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                await self._reload_config()

    async def _reload_config(self) -> None:
        try:
            new_config = load_config(self.config_path)
        except Exception as exc:
            self.notify(f"Config error: {exc}", severity="error", timeout=6)
            return

        self.config_data = new_config

        # Remove all existing rows (Textual cancels their workers on removal)
        for row in list(self.query(".row")):
            await row.remove()

        # Brief pause to let worker cancellation + process cleanup propagate
        await asyncio.sleep(0.2)

        # Remount with the new config
        for i, row_cfg in enumerate(self.config_data.rows):
            panes = [self._make_pane(p) for p in row_cfg.panes]
            row = Horizontal(*panes, id=f"row-{i}", classes="row")
            row.styles.height = row_cfg.height
            await self.mount(row)

        self.title = self.config_data.title
        self.notify("Config reloaded ↺", timeout=2)

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #

    async def action_reload(self) -> None:
        """Manually force a config reload (same as saving the file)."""
        await self._reload_config()

    # ------------------------------------------------------------------ #
    # Resize: collapse panes below their min_width                        #
    # ------------------------------------------------------------------ #

    def on_resize(self) -> None:
        term_width = self.size.width
        for pane in self.query(".pane-collapsible"):
            min_w = getattr(pane, "_min_width", 0)
            pane.display = not (min_w and term_width < min_w)

