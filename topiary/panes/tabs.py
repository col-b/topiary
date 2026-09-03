"""TabsPane: a group of tabbed panes (any type, nestable)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Tab, TabbedContent, TabPane

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
        self._hover_refresh: bool = False

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
        self._set_border_title()
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width

    def _has_refresh_icon(self) -> bool:
        return self.pane_cfg.refresh > 0 or bool(self.pane_cfg.refresh_command)

    def _set_border_title(self, hover: bool = False) -> None:
        title = self.pane_cfg.title or self.pane_cfg.id
        if self._has_refresh_icon():
            icon = "[bold yellow]⟳[/bold yellow]" if hover else "⟳"
            self.border_title = f"{icon} {title}"
        else:
            self.border_title = title

    def _get_active_inner_pane(self):
        """Return the active tab's inner CommandPane, or None."""
        from .command import CommandPane
        try:
            tc = self.query_one(TabbedContent)
            active_id = tc.active
            if not active_id:
                return None
            inner = self.query_one(f"#inner-{active_id}")
            return inner if isinstance(inner, CommandPane) else None
        except Exception:
            return None

    def on_enter(self, event: object) -> None:
        if self.pane_cfg.is_interactive:
            self.add_class("pane-hover")

    def on_leave(self, event: object) -> None:
        self.remove_class("pane-hover")
        if self._hover_refresh:
            self._hover_refresh = False
            self._set_border_title(hover=False)

    def on_mouse_move(self, event: object) -> None:
        if not self._has_refresh_icon():
            return
        x, y = getattr(event, "x", -1), getattr(event, "y", -1)
        on_icon = y == 0 and x <= 3
        if on_icon != self._hover_refresh:
            self._hover_refresh = on_icon
            self._set_border_title(hover=on_icon)

    def on_click(self, event: Click) -> None:
        """Clicking ⟳ refreshes the active tab; other clicks toggle true focus."""
        if self._has_refresh_icon() and event.y == 0 and event.x <= 3:
            event.stop()
            if self.pane_cfg.refresh_command:
                import subprocess
                subprocess.Popen(
                    self.pane_cfg.refresh_command, shell=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                inner = self._get_active_inner_pane()
                if inner is not None:
                    self.run_worker(inner._run_once(), name=f"force-refresh-{self.pane_cfg.id}")
            return

        # Tab label clicks should switch tabs, not cycle pane selection.
        if isinstance(event.widget, Tab):
            return

        if not self.pane_cfg.is_interactive:
            return

        event.stop()
        app = self.app
        if app._focused_pane is self and not app._passthrough:
            self.remove_class("pane-focused")
            app._set_focused_pane(None)
            self.add_class("pane-hover")
        else:
            app._set_focused_pane(self)
