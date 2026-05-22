"""SplitPane: a vertical ("column") or horizontal ("row") stack of sub-panes.

Supports arbitrary nesting — sub-panes can themselves be column, row, tabs,
command, or system panes.
"""
from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.widget import Widget

from ..config import PaneConfig


class SplitPane(Widget):
    """A vertical or horizontal container of sub-panes, recursively nestable."""

    DEFAULT_CSS = """
    SplitPane {
        border: none;
        padding: 0;
    }
    """

    def __init__(
        self,
        pane_cfg: PaneConfig,
        *,
        direction: Literal["column", "row"] = "column",
        refresh_rate_hz: float = 20.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._direction = direction
        self._refresh_rate_hz = refresh_rate_hz

    def on_mount(self) -> None:
        self.styles.layout = "vertical" if self._direction == "column" else "horizontal"
        self.styles.width = self.pane_cfg.width
        self.styles.height = self.pane_cfg.height

    def compose(self) -> ComposeResult:
        from .factory import make_pane

        for i, sub_cfg in enumerate(self.pane_cfg.panes):
            if not sub_cfg.id:
                sub_cfg.id = f"{self.pane_cfg.id or 'split'}--sub{i}"
            pane = make_pane(sub_cfg, self._refresh_rate_hz, id_prefix=sub_cfg.id)
            if self._direction == "column":
                pane.styles.height = sub_cfg.height
                pane.styles.width = "1fr"
            else:
                pane.styles.width = sub_cfg.width
                pane.styles.height = "1fr"
            yield pane
