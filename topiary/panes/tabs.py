"""TabsPane: a group of tabbed panes (any type, nestable)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import TabbedContent, TabPane

from ..config import PaneConfig
from .command import CommandPane
from .system import SystemPane


class TabsPane(Widget):
    """A pane containing named tabs; each tab can be any pane type, including tabs."""

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
    TabsPane SystemPane {
        width: 1fr;
        height: 1fr;
        border: none;
    }
    TabsPane TabsPane {
        width: 1fr;
        height: 1fr;
        border: none;
    }
    """

    def __init__(self, pane_cfg: PaneConfig, *, refresh_rate_hz: float = 20.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._refresh_rate_hz = refresh_rate_hz

    def compose(self) -> ComposeResult:
        with TabbedContent():
            for tab_cfg in self.pane_cfg.tabs:
                # Fill in id and width defaults for inline tab entries
                tab_cfg.width = "1fr"
                if not tab_cfg.id:
                    tab_cfg.id = f"{self.pane_cfg.id}--{tab_cfg.title.lower().replace(' ', '-')}"
                tab_id = tab_cfg.id
                with TabPane(tab_cfg.title or tab_cfg.id, id=tab_id):
                    yield self._make_inner_pane(tab_cfg)

    def _make_inner_pane(self, cfg: PaneConfig) -> Widget:
        hz = self._refresh_rate_hz
        match cfg.type:
            case "system":
                return SystemPane(cfg, id=f"inner-{cfg.id}")
            case "tabs":
                return TabsPane(cfg, refresh_rate_hz=hz, id=f"inner-{cfg.id}")
            case _:
                return CommandPane(cfg, show_border=False, refresh_rate_hz=hz, id=f"inner-{cfg.id}")

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        self.border_title = self.pane_cfg.title or self.pane_cfg.id
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
