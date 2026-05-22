"""TabsPane: a group of tabbed panes (any type, nestable)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import TabbedContent, TabPane

from ..config import PaneConfig
from .factory import make_pane


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
    TabsPane ContentSwitcher {
        height: 1fr;
    }
    TabsPane TabPane {
        height: 1fr;
        padding: 0;
    }
    TabsPane TabPane > * {
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
            for i, tab_cfg in enumerate(self.pane_cfg.tabs):
                if not tab_cfg.id:
                    slug = tab_cfg.title.lower().replace(" ", "-") or f"tab{i}"
                    tab_cfg.id = f"{self.pane_cfg.id}--{slug}"
                tab_cfg.width = "1fr"
                tab_cfg.height = "1fr"
                with TabPane(tab_cfg.title or tab_cfg.id, id=tab_cfg.id):
                    yield make_pane(
                        tab_cfg,
                        self._refresh_rate_hz,
                        show_border=False,
                        id_prefix=f"inner-{tab_cfg.id}",  # avoid duplicate ID with TabPane
                    )

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        self.border_title = self.pane_cfg.title or self.pane_cfg.id
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
