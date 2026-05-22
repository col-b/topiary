"""Main Textual application."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget

from .config import AppConfig, PaneConfig
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
        Binding("r", "refresh_all", "Refresh"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config_data = config

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
    # Actions                                                             #
    # ------------------------------------------------------------------ #

    def action_refresh_all(self) -> None:
        """Force-refresh every CommandPane immediately."""
        for pane in self.query(CommandPane):
            pane._running = False
            pane.run_worker(
                pane._do_refresh(), exclusive=True, name=f"cmd-{pane.pane_cfg.id}"
            )

    # ------------------------------------------------------------------ #
    # Resize: collapse panes below their min_width                       #
    # ------------------------------------------------------------------ #

    def on_resize(self) -> None:
        term_width = self.size.width
        for pane in self.query(".pane-collapsible"):
            # panes store their min_width in a custom attribute
            min_w = getattr(pane, "_min_width", 0)
            if min_w and term_width < min_w:
                pane.display = False
            else:
                pane.display = True
