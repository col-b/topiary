"""Main Textual application."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.table import Table as RichTable
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane

from .config import AppConfig, PaneConfig, load_config
from .log import log
from .panes.command import PANE_PERF
from .panes.factory import make_pane


# ── PTY key map for passthrough mode ─────────────────────────────────────────

_KEY_TO_PTY: dict[str, bytes] = {
    "enter":      b"\r",
    "tab":        b"\t",
    "shift+tab":  b"\x1b[Z",
    "backspace":  b"\x7f",
    "delete":     b"\x1b[3~",
    "up":         b"\x1b[A",
    "down":       b"\x1b[B",
    "right":      b"\x1b[C",
    "left":       b"\x1b[D",
    "home":       b"\x1b[H",
    "end":        b"\x1b[F",
    "page_up":    b"\x1b[5~",
    "page_down":  b"\x1b[6~",
    "ctrl+a": b"\x01", "ctrl+b": b"\x02", "ctrl+c": b"\x03",
    "ctrl+d": b"\x04", "ctrl+e": b"\x05", "ctrl+f": b"\x06",
    "ctrl+g": b"\x07", "ctrl+h": b"\x08", "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c", "ctrl+n": b"\x0e", "ctrl+o": b"\x0f",
    "ctrl+p": b"\x10", "ctrl+q": b"\x11", "ctrl+r": b"\x12",
    "ctrl+s": b"\x13", "ctrl+t": b"\x14", "ctrl+u": b"\x15",
    "ctrl+v": b"\x16", "ctrl+w": b"\x17", "ctrl+x": b"\x18",
    "ctrl+y": b"\x19", "ctrl+z": b"\x1a",
    "f1": b"\x1bOP",  "f2": b"\x1bOQ",  "f3": b"\x1bOR",  "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}


def _key_to_pty_bytes(event: events.Key) -> bytes | None:
    """Convert a Textual Key event to PTY bytes for passthrough mode."""
    if event.key in _KEY_TO_PTY:
        return _KEY_TO_PTY[event.key]
    if event.character and len(event.character) == 1:
        return event.character.encode("utf-8")
    return None


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


class HelpOverlay(Static):
    """Help overlay showing key bindings and attribution. Toggle with 'h'."""

    DEFAULT_CSS = """
    HelpOverlay {
        dock: top;
        width: 70;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 3;
        layer: overlay;
        display: none;
    }
    """

    def on_mount(self) -> None:
        from rich.text import Text
        help_text = Text(justify="center")
        help_text.append("╭─ TOPIARY ─╮\n", style="bold cyan")
        help_text.append("Created mostly by Copilot with some clb-help\n\n", style="dim")
        help_text.append("Key Bindings:\n", style="bold yellow")
        help_text.append("  h           ", style="cyan")
        help_text.append("Toggle this help\n")
        help_text.append("  Ctrl+R      ", style="cyan")
        help_text.append("Restart pane / Reload config / Passthrough\n")
        help_text.append("  Ctrl+Q      ", style="cyan")
        help_text.append("Quit topiary\n")
        help_text.append("  Ctrl+C      ", style="cyan")
        help_text.append("Quit topiary\n")
        help_text.append("  Ctrl+D      ", style="cyan")
        help_text.append("Toggle performance overlay\n")
        help_text.append("  Tab         ", style="cyan")
        help_text.append("Focus next pane\n")
        help_text.append("  Shift+Tab   ", style="cyan")
        help_text.append("Focus previous pane\n")
        help_text.append("  Enter       ", style="cyan")
        help_text.append("Enter passthrough mode (interact with pane)\n")
        help_text.append("  Escape      ", style="cyan")
        help_text.append("Exit passthrough mode\n\n")
        help_text.append("Press h to close", style="dim italic")
        self.update(help_text)
        log.info("HelpOverlay mounted")



class TopiaryApp(App):
    """Topiary — a configurable TUI dashboard."""

    REFRESH_RATE = 10  # compositor at 10 Hz instead of default 60 Hz

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    .row {
        layout: horizontal;
        width: 100%;
    }
    .pane-focused {
        border: round $accent;
        border-title-color: $accent;
        border-title-style: bold;
    }
    .pane-active {
        border: round $success;
        border-title-color: $success;
        border-title-style: bold;
    }
    """

    BINDINGS = [
        Binding("h",         "toggle_help",      "Help",        priority=True),
        Binding("ctrl+q",    "quit",             "Quit",        priority=True),
        Binding("ctrl+c",    "quit",             "Quit",        priority=True),
        Binding("ctrl+r",    "restart_pane",     "Restart pane", priority=True),
        Binding("ctrl+d",    "toggle_perf",      "Perf",        priority=True),
        Binding("tab",       "focus_next_pane",  "Next pane",   priority=True),
        Binding("shift+tab", "focus_prev_pane",  "Prev pane",   priority=True),
        Binding("enter",     "activate_pane",    "Interact",    priority=True),
        Binding("escape",    "deactivate_pane",  "Exit",        priority=True),
    ]

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        super().__init__()
        self.config_data = config
        self.config_path = config_path
        self._focused_pane: Widget | None = None
        # In passthrough mode all keys are forwarded to _passthrough_target's PTY.
        # _passthrough_target may differ from _focused_pane when a TabsPane is focused
        # (we route into its active inner CommandPane).
        self._passthrough: bool = False
        self._passthrough_target: Widget | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def on_mount(self) -> None:
        self.title = self.config_data.title
        self.screen.styles.background = self.config_data.background
        pane_count = sum(len(r.panes) for r in self.config_data.rows)
        log.info("app mounted  panes=%d  refresh_rate=%.1fhz  title=%r",
                 pane_count, self.config_data.refresh_rate_hz, self.config_data.title)
        self.run_worker(self._watch_config(), exclusive=True, name="config-watcher")

    # ------------------------------------------------------------------ #
    # Composition                                                          #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield PerfOverlay(id="perf-overlay")
        yield HelpOverlay(id="help-overlay")
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
        log.info("reloading config  path=%s", self.config_path)
        try:
            new_config = load_config(self.config_path)
        except Exception as exc:
            log.warning("config reload failed: %s", exc)
            self.notify(f"Config error: {exc}", severity="error", timeout=6)
            return

        self.config_data = new_config
        self._set_focused_pane(None)  # stale widget refs after rebuild

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

    async def action_restart_pane(self) -> None:
        """Ctrl+R — three modes:
        1. Passthrough: forward \\x12 to PTY
        2. Pane focused: restart that pane
        3. No pane focused: reload config
        """
        log.info("restart_pane action called  passthrough=%s  focused=%s", 
                 self._passthrough, self._focused_pane)
        from .panes.command import CommandPane
        from .panes.tabs import TabsPane

        if self._passthrough:
            t = self._passthrough_target
            if t is not None and isinstance(t, CommandPane):
                runner = t._runner
                if runner is not None and runner._master_fd >= 0:
                    try:
                        os.write(runner._master_fd, b"\x12")
                    except OSError:
                        pass
            return

        if self._focused_pane is None:
            # No pane focused → reload config
            log.info("no pane focused, reloading config")
            await self._reload_config()
            return
        target: Widget = self._focused_pane
        if isinstance(target, TabsPane):
            target = self._get_active_tab_pane(target) or target
        if not isinstance(target, CommandPane):
            log.warning("focused pane is not CommandPane: %s", type(target))
            return

        label = target.pane_cfg.title or target.pane_cfg.id
        log.info("restart_pane  pane=%s", target.id)
        await target.restart()
        self.notify(f"↺  Restarting {label}", timeout=2)

    def action_toggle_perf(self) -> None:
        """Toggle the performance overlay."""
        overlay = self.query_one("#perf-overlay", PerfOverlay)
        overlay.display = not overlay.display
        if overlay.display:
            overlay._refresh_stats()

    def action_toggle_help(self) -> None:
        """Toggle the help overlay."""
        log.info("toggle_help action called")
        overlay = self.query_one("#help-overlay", HelpOverlay)
        overlay.display = not overlay.display
        log.info("help overlay display=%s", overlay.display)

    # -- Pane focus --------------------------------------------------------

    def action_focus_next_pane(self) -> None:
        """Tab → advance focus highlight to the next pane."""
        panes = self._collect_focusable()
        if not panes:
            return
        if self._focused_pane not in panes:
            idx = 0
        else:
            idx = (panes.index(self._focused_pane) + 1) % len(panes)
        self._set_focused_pane(panes[idx])

    def action_focus_prev_pane(self) -> None:
        """Shift+Tab → move focus highlight to the previous pane."""
        panes = self._collect_focusable()
        if not panes:
            return
        if self._focused_pane not in panes:
            idx = len(panes) - 1
        else:
            idx = (panes.index(self._focused_pane) - 1) % len(panes)
        self._set_focused_pane(panes[idx])

    def action_activate_pane(self) -> None:
        """Enter → start passthrough on the focused CommandPane (or its active tab)."""
        from .panes.command import CommandPane
        from .panes.tabs import TabsPane

        if self._focused_pane is None:
            return

        # For TabsPane, route into the active tab's inner CommandPane
        target: Widget = self._focused_pane
        if isinstance(target, TabsPane):
            target = self._get_active_tab_pane(target) or target

        if not isinstance(target, CommandPane):
            return
        if target._runner is None:
            return

        self._passthrough = True
        self._passthrough_target = target
        log.debug("passthrough ON  pane=%s", target.id)
        # Visual: focused pane (may be a TabsPane wrapper) turns green
        self._focused_pane.remove_class("pane-focused")
        self._focused_pane.add_class("pane-active")
        # Title indicator on the target pane
        saved = target.border_title or ""
        target._passthrough_title_saved = saved
        if saved:
            target.border_title = f"▶ {saved}"

    def action_deactivate_pane(self) -> None:
        """Escape → exit passthrough → focused; second Escape → unfocus."""
        if self._passthrough:
            from .panes.command import CommandPane
            # Signal the PTY process to clear any interactive selection state.
            t = self._passthrough_target
            if t is not None and isinstance(t, CommandPane):
                runner = t._runner
                if runner is not None and runner._master_fd >= 0:
                    try:
                        os.write(runner._master_fd, b"\x1b")
                    except OSError:
                        pass
            self._passthrough = False
            log.debug("passthrough OFF  pane=%s", t.id if t else "none")
            # Restore title on the target pane
            if t is not None:
                saved = getattr(t, "_passthrough_title_saved", None)
                if saved is not None:
                    t.border_title = saved
            self._passthrough_target = None
            # Back to focused (highlighted) state
            if self._focused_pane is not None:
                self._focused_pane.remove_class("pane-active")
                self._focused_pane.add_class("pane-focused")
        elif self._focused_pane is not None:
            self._set_focused_pane(None)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Block app bindings in passthrough mode so keys flow to on_key → PTY."""
        if self._passthrough:
            # deactivate_pane exits passthrough; restart_pane forwards \x12 to PTY
            return action in ("deactivate_pane", "restart_pane")
        # deactivate_pane requires a focused pane
        if action == "deactivate_pane":
            return self._focused_pane is not None
        # restart_pane works both with and without focus (reload vs restart)
        return True

    def on_key(self, event: events.Key) -> None:
        """In passthrough mode: forward all keys to PTY.
        In focused (nav) mode: scroll the pane with arrow/page keys.
        """
        if self._passthrough:
            if self._passthrough_target is None:
                return
            from .panes.command import CommandPane
            if not isinstance(self._passthrough_target, CommandPane):
                return
            # Escape handled by deactivate_pane binding (fires before on_key)
            if event.key == "escape":
                return
            runner = self._passthrough_target._runner
            if runner is None or runner._master_fd < 0:
                return
            key_bytes = _key_to_pty_bytes(event)
            if key_bytes is not None:
                try:
                    os.write(runner._master_fd, key_bytes)
                except OSError:
                    pass
            event.stop()

        elif self._focused_pane is not None:
            # Scroll the focused pane with arrow / page keys (no passthrough needed)
            _SCROLL_KEYS = {"up", "down", "page_up", "page_down", "home", "end"}
            if event.key in _SCROLL_KEYS:
                self._scroll_focused_pane(event.key)
                event.stop()

    # -- Focus helpers -----------------------------------------------------

    def _collect_focusable(self) -> list[Widget]:
        """Walk the widget tree and collect top-level CommandPane + TabsPane instances.

        SplitPane and Horizontal are transparent to focus (we recurse through them).
        TabsPane is treated as a single focusable unit (not recursed into).
        """
        from .panes.command import CommandPane
        from .panes.system import SystemPane
        from .panes.tabs import TabsPane

        result: list[Widget] = []

        def walk(widget: Widget) -> None:
            for child in widget.children:
                if not child.display:
                    continue
                if isinstance(child, (CommandPane, TabsPane, SystemPane)):
                    result.append(child)
                else:
                    walk(child)

        walk(self.screen)
        return result

    def _set_focused_pane(self, widget: Widget | None) -> None:
        """Set visual focus highlight; clears passthrough as a side-effect."""
        if self._focused_pane is not None:
            self._focused_pane.remove_class("pane-focused", "pane-active")
        self._focused_pane = widget
        self._passthrough = False
        self._passthrough_target = None
        if widget is not None:
            widget.add_class("pane-focused")

    def _scroll_focused_pane(self, key: str) -> None:
        """Scroll the VerticalScroll of the focused pane (or its active tab)."""
        from .panes.command import CommandPane
        from .panes.tabs import TabsPane
        from textual.containers import VerticalScroll

        pane = self._focused_pane
        if isinstance(pane, TabsPane):
            pane = self._get_active_tab_pane(pane) or pane
        if not isinstance(pane, CommandPane):
            return
        try:
            scroller = pane.query_one(VerticalScroll)
        except Exception:
            return  # non-scrollable pane — nothing to do
        match key:
            case "up":        scroller.scroll_up(animate=False)
            case "down":      scroller.scroll_down(animate=False)
            case "page_up":   scroller.scroll_page_up(animate=False)
            case "page_down": scroller.scroll_page_down(animate=False)
            case "home":      scroller.scroll_home(animate=False)
            case "end":       scroller.scroll_end(animate=False)

    def _get_active_tab_pane(self, tabs_pane: Widget) -> Widget | None:
        """Return the active tab's inner CommandPane from a TabsPane."""
        from .panes.command import CommandPane
        try:
            tc = tabs_pane.query_one(TabbedContent)
            active_id = tc.active  # e.g. "dev--glam"
            if not active_id:
                return None
            inner = tabs_pane.query_one(f"#inner-{active_id}")
            return inner if isinstance(inner, CommandPane) else None
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Resize: collapse panes below their min_width                        #
    # ------------------------------------------------------------------ #

    def on_resize(self) -> None:
        term_width = self.size.width
        for pane in self.query(".pane-collapsible"):
            min_w = getattr(pane, "_min_width", 0)
            pane.display = not (min_w and term_width < min_w)

