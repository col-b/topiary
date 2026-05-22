"""Shared pane factory — maps PaneConfig.type to the right Widget.

All containers (SplitPane, TabsPane, TopiaryApp) use make_pane() so the
full type set is available everywhere without duplication.  Imports are
lazy (inside the function body) to avoid circular import issues.
"""
from __future__ import annotations

from ..config import PaneConfig


def make_pane(
    cfg: PaneConfig,
    refresh_rate_hz: float,
    *,
    show_border: bool = True,
    id_prefix: str = "",
) -> "Widget":  # type: ignore[name-defined]  # noqa: F821
    from textual.widget import Widget  # noqa: F401

    from .command import CommandPane
    from .split import SplitPane
    from .system import SystemPane
    from .tabs import TabsPane

    widget_id = cfg.id or f"{id_prefix}-anon"

    match cfg.type:
        case "system":
            return SystemPane(cfg, id=widget_id)
        case "tabs":
            return TabsPane(cfg, refresh_rate_hz=refresh_rate_hz, id=widget_id)
        case "column":
            return SplitPane(cfg, direction="column", refresh_rate_hz=refresh_rate_hz, id=widget_id)
        case "row":
            return SplitPane(cfg, direction="row", refresh_rate_hz=refresh_rate_hz, id=widget_id)
        case _:  # "command" and anything else
            return CommandPane(
                cfg,
                show_border=show_border,
                refresh_rate_hz=refresh_rate_hz,
                id=widget_id,
            )
