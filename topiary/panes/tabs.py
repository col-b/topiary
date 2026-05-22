"""TabsPane: a group of tabbed command panes."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import TabbedContent, TabPane

from ..config import PaneConfig
from .command import CommandPane


class TabsPane(Widget):
    """A pane that contains multiple named tabs, each running a command."""

    DEFAULT_CSS = """
    TabsPane {
        border: round $panel-lighten-2;
        padding: 0;
        overflow: hidden;
    }
    TabsPane TabbedContent {
        height: 1fr;
    }
    TabsPane TabPane {
        padding: 0;
    }
    TabsPane CommandPane {
        width: 1fr;
        height: 1fr;
        border: none;
    }
    """

    def __init__(self, pane_cfg: PaneConfig, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg

    def compose(self) -> ComposeResult:
        with TabbedContent():
            for tab_cfg in self.pane_cfg.tabs:
                # Build a minimal PaneConfig per tab (no outer border)
                inner_cfg = PaneConfig(
                    id=f"{self.pane_cfg.id}--{tab_cfg.title.lower().replace(' ', '-')}",
                    type="command",
                    title=tab_cfg.title,
                    command=tab_cfg.command,
                    refresh=tab_cfg.refresh,
                    width="1fr",
                )
                with TabPane(tab_cfg.title, id=inner_cfg.id):
                    yield CommandPane(inner_cfg, show_border=False, id=f"cmd-{inner_cfg.id}")

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        self.border_title = self.pane_cfg.title or self.pane_cfg.id
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
