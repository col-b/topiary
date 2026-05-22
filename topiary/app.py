"""Main Textual application."""
from __future__ import annotations

import asyncio
from pathlib import Path

from rich.table import Table as RichTable
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from .config import AppConfig, PaneConfig, load_config
from .panes.command import PANE_PERF
from .panes.factory import make_pane


class PerfOverlay(Static):
    """Floating overlay showing per-pane render times. Toggle with 'd'."""

    DEFAULT_CSS = """
    PerfOverlay {
        dock: right;
        width: 62;
        height: auto;
        background: $surface;
        border: round $warning;
        padding: 0 1;
        layer: overlay;
        display: none;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_stats)

    def _refresh_stats(self) -> None:
        if not self.display:
            return
        table = RichTable(title="Pane Perf (press d to close)", expand=True, show_lines=False)
        table.add_column("Pane", style="cyan", max_width=16)
        table.add_column("Rndr/s", justify="right")
        table.add_column("s2r ms", justify="right")
        table.add_column("upd ms", justify="right")
        table.add_column("tot ms", justify="right")
        table.add_column("max ms", justify="right")
        table.add_column("Vis", justify="center")
        rows = sorted(PANE_PERF.items(), key=lambda x: -x[1].get("render_ms_last", 0))
        for _pid, s in rows:
            last = s["render_ms_last"]
            color = "red" if last > 30 else ("yellow" if last > 10 else "green")
            table.add_row(
                s["title"],
                f"{s['render_hz']:.1f}",
                f"[{color}]{s.get('s2r_ms', 0):.1f}[/{color}]",
                f"{s.get('update_ms', 0):.1f}",
                f"[{color}]{last:.1f}[/{color}]",
                f"{s['render_ms_max']:.1f}",
                "✓" if s["visible"] else "·",
            )
        self.update(table)



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
        Binding("d", "toggle_perf", "Debug perf", show=False),
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
        self.screen.styles.background = self.config_data.background
        self.run_worker(self._watch_config(), exclusive=True, name="config-watcher")

    # ------------------------------------------------------------------ #
    # Composition                                                          #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield PerfOverlay(id="perf-overlay")
        for i, row_cfg in enumerate(self.config_data.rows):
            panes: list[Widget] = []
            for pane_cfg in row_cfg.panes:
                pane = self._make_pane(pane_cfg)
                panes.append(pane)
            row = Horizontal(*panes, id=f"row-{i}", classes="row")
            row.styles.height = row_cfg.height
            yield row

    def _make_pane(self, pane_cfg: PaneConfig) -> Widget:
        hz = self.config_data.refresh_rate_hz
        return make_pane(pane_cfg, hz, id_prefix=pane_cfg.id or "pane")

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
        self.screen.styles.background = self.config_data.background
        self.notify("Config reloaded ↺", timeout=2)

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #

    async def action_reload(self) -> None:
        """Manually force a config reload (same as saving the file)."""
        await self._reload_config()

    def action_toggle_perf(self) -> None:
        """Toggle the performance overlay."""
        overlay = self.query_one("#perf-overlay", PerfOverlay)
        overlay.display = not overlay.display
        if overlay.display:
            overlay._refresh_stats()

    # ------------------------------------------------------------------ #
    # Resize: collapse panes below their min_width                        #
    # ------------------------------------------------------------------ #

    def on_resize(self) -> None:
        term_width = self.size.width
        for pane in self.query(".pane-collapsible"):
            min_w = getattr(pane, "_min_width", 0)
            pane.display = not (min_w and term_width < min_w)

