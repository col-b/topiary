"""CommandPane: runs any shell command in a PTY and displays its output."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Static

from ..config import PaneConfig
from ..log import log
from ..runner import TerminalRunner

_SCROLLABLE_ROWS = 200  # virtual PTY height for scrollable panes

# Module-level perf registry updated by every CommandPane on each render.
# Keys are pane ids; read by PerfOverlay in app.py.
PANE_PERF: dict[str, dict[str, Any]] = {}


class CommandPane(Widget):
    """Runs a shell command in a PTY; restarts after exit per `refresh` setting."""

    DEFAULT_CSS = """
    CommandPane {
        border: round $panel-lighten-2;
        padding: 0;
        overflow: hidden;
    }
    CommandPane > Static {
        width: 1fr;
        height: 1fr;
    }
    CommandPane > VerticalScroll {
        width: 1fr;
        height: 1fr;
        scrollbar-size-vertical: 0;
    }
    CommandPane > VerticalScroll > Static {
        width: 100%;
        height: auto;
    }
    """

    def __init__(
        self,
        pane_cfg: PaneConfig,
        *,
        show_border: bool = True,
        refresh_rate_hz: float = 20.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._show_border = show_border
        self._refresh_rate_hz = max(1.0, refresh_rate_hz)
        self._runner: TerminalRunner | None = None
        self._dirty: bool = False
        self._run_lock = asyncio.Lock()
        self._run_index: int = 0
        # perf counters
        self._pty_updates: int = 0
        self._render_count: int = 0
        self._render_ms_total: float = 0.0
        self._render_ms_max: float = 0.0
        self._render_ms_last: float = 0.0
        self._render_ts: deque[float] = deque(maxlen=60)  # timestamps of recent renders
        self._log = log.getChild(f"pane.{pane_cfg.id}")
        self._last_log_ts: float = 0.0   # monotonic time of last periodic perf log
        self._hover_refresh: bool = False  # true when mouse is over top border row

    def compose(self) -> ComposeResult:
        if self.pane_cfg.scrollable:
            with VerticalScroll(id="scroll"):
                yield Static("", id="output", markup=False)
        else:
            yield Static("", id="output", markup=False)

    def on_mount(self) -> None:
        self.styles.width = self.pane_cfg.width
        if self._show_border:
            self._set_border_title()
        else:
            self.styles.border = ("none", "transparent")
        if self.pane_cfg.min_width:
            self.add_class("pane-collapsible")
            self._min_width = self.pane_cfg.min_width
        self.set_interval(1 / self._refresh_rate_hz, self._maybe_refresh)
        self.run_worker(self._run_loop(), exclusive=True, name=f"cmd-{self.pane_cfg.id}")

    def _set_border_title(self, hover: bool = False) -> None:
        if not self._show_border:
            return
        title = self.pane_cfg.title or self.pane_cfg.id
        if self.pane_cfg.refresh > 0:
            icon = "[bold yellow]⟳[/bold yellow]" if hover else "⟳"
            self.border_title = f"{icon} {title}"
        else:
            self.border_title = title

    def on_mouse_move(self, event: object) -> None:
        if self.pane_cfg.refresh <= 0:
            return
        x, y = getattr(event, "x", -1), getattr(event, "y", -1)
        on_icon = y == 0 and x <= 3
        if on_icon != self._hover_refresh:
            self._hover_refresh = on_icon
            self._set_border_title(hover=on_icon)

    def on_enter(self, event: object) -> None:
        if self.pane_cfg.is_interactive:
            self.add_class("pane-hover")

    def on_leave(self, event: object) -> None:
        self.remove_class("pane-hover")
        if self._hover_refresh:
            self._hover_refresh = False
            self._set_border_title(hover=False)

    def on_unmount(self) -> None:
        """Synchronously kill child process on widget removal or app exit."""
        if self._runner is not None:
            self._runner.kill_sync()
            self._runner = None

    async def restart(self) -> None:
        """Kill the running process and restart the run loop from scratch."""
        self._log.info("restart requested")
        if self._runner is not None:
            self._runner.kill_sync()
            self._runner = None
        
        # Cancel any existing worker with the same name
        worker_name = f"cmd-{self.pane_cfg.id}"
        for worker in self.workers:
            if worker.name == worker_name and not worker.is_finished:
                self._log.info("cancelling existing worker: %s", worker.name)
                worker.cancel()
        
        # Clear the display and show restart message
        output_widget = self.query_one("#output", Static)
        output_widget.update("")
        await asyncio.sleep(0.05)  # Brief pause for clear to be visible
        output_widget.update("↺ Restarting...")
        
        # Keep _dirty = True so the first output will render
        self._dirty = True
        # Reset perf counters so stats reflect the new process, not the old one
        self._pty_updates = 0
        self._render_count = 0
        self._render_ms_total = 0.0
        self._render_ms_max = 0.0
        self._render_ms_last = 0.0
        self._last_log_ts = 0.0
        
        # Wait for old worker/process to fully exit
        await asyncio.sleep(0.3)
        
        # Clear restart message before starting
        output_widget.update("")
        
        # Start fresh worker
        self.run_worker(self._run_loop(), exclusive=True, name=worker_name)
        self._log.info("restart worker launched")

    def on_button_pressed(self, event: object) -> None:
        pass  # no longer used — kept so subclasses aren't broken

    def on_click(self, event: Click) -> None:
        """Clicking the ⟳ triggers a refresh; other clicks cycle focus state."""
        if self.pane_cfg.refresh > 0 and event.y == 0 and event.x <= 3:
            event.stop()
            self.run_worker(self._run_once(), name=f"force-refresh-{self.pane_cfg.id}")
            return

        # Only cycle focus for interactive panes.
        if not self.pane_cfg.is_interactive:
            return

        event.stop()
        # Cycle: unfocused → focused → selected → unfocused (+hover since mouse is still over)
        app = self.app
        if self.has_class("pane-active"):
            app._set_focused_pane(None)
            self.add_class("pane-hover")
        elif self.has_class("pane-focused"):
            self.remove_class("pane-focused")
            self.add_class("pane-active")
        else:
            app._set_focused_pane(self)

    # ------------------------------------------------------------------ #
    # Run loop                                                             #
    # ------------------------------------------------------------------ #

    async def _run_loop(self) -> None:
        """Run once (refresh==0) or periodically (refresh>0)."""
        # Wait for Textual to complete its first layout pass so content_size is
        # known before we create the PTY.  Without this, panes start with the
        # 24×80 fallback and then immediately receive SIGWINCH when the real
        # layout fires — some apps (btop) crash if SIGWINCH arrives during init.
        for _ in range(40):
            if self.content_size.width > 0 and self.content_size.height > 0:
                break
            await asyncio.sleep(0.05)
        self._log.info("pane starting  cmd=%.80s", self.pane_cfg.command or "(none)")
        try:
            await self._run_once()
            while self.pane_cfg.refresh > 0:
                await asyncio.sleep(self.pane_cfg.refresh)
                await self._run_once()
        except Exception:
            self._log.exception("unhandled error in _run_loop")
        finally:
            if self._runner is not None:
                self._runner.kill_sync()
                self._runner = None

    async def _run_once(self) -> None:
        async with self._run_lock:
            self._run_index += 1
            run_index = self._run_index
            if self.pane_cfg.scrollable:
                rows, cols = _SCROLLABLE_ROWS, max(10, self.content_size.width or 80)
            else:
                rows = max(4, self.content_size.height or 24)
                cols = max(10, self.content_size.width or 80)
            runner = TerminalRunner(
                self.pane_cfg.command or "echo 'no command configured'",
                rows,
                cols,
            )
            self._runner = runner
            t_start = time.monotonic()
            try:
                await runner.start(on_update=self._mark_dirty, cwd=self.pane_cfg.cwd)
                await runner.wait()
                elapsed = time.monotonic() - t_start
                self._log.info(
                    "run #%d finished  exit=%s  elapsed=%.1fs  pty_bytes=%d",
                    run_index,
                    runner.exit_code,
                    elapsed,
                    runner.bytes_received,
                )
            except Exception:
                self._log.exception("error in _run_once #%d", run_index)
            finally:
                if self._dirty:
                    try:
                        text = runner.render(trim_trailing=self.pane_cfg.scrollable)
                        self.query_one("#output", Static).update(text)
                        self._dirty = False
                    except Exception:
                        pass
                self._runner = None
                runner.kill_sync()

    # ------------------------------------------------------------------ #
    # Display + resize                                                     #
    # ------------------------------------------------------------------ #

    def _mark_dirty(self) -> None:
        """Called by TerminalRunner on every PTY read — O(1), just flip a flag."""
        self._dirty = True
        self._pty_updates += 1

    def _maybe_refresh(self) -> None:
        """Timer callback: render only if new PTY data has arrived since last frame."""
        if not self._dirty or self._runner is None:
            return
        self._dirty = False
        t0 = time.perf_counter()
        try:
            rich_text = self._runner.render(trim_trailing=self.pane_cfg.scrollable)
        except Exception:
            self._log.exception("error in render()")
            return
        t1 = time.perf_counter()
        try:
            output = self.query_one("#output", Static)
            if self.pane_cfg.scrollable:
                scroller = self.query_one("#scroll", VerticalScroll)
                at_bottom = scroller.is_vertical_scroll_end
                output.update(rich_text)
                if self.pane_cfg.follow and at_bottom:
                    scroller.scroll_end(animate=False)
            else:
                output.update(rich_text)
        except Exception:
            self._log.exception("error in Static.update()")
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._record_perf(elapsed_ms, (t1 - t0) * 1000)

    def _record_perf(self, elapsed_ms: float, render_ms: float) -> None:
        self._render_count += 1
        self._render_ms_total += elapsed_ms
        self._render_ms_last = elapsed_ms
        if elapsed_ms > self._render_ms_max:
            self._render_ms_max = elapsed_ms
        now = time.monotonic()
        self._render_ts.append(now)
        cutoff = now - 10.0
        recent = sum(1 for t in self._render_ts if t >= cutoff)
        render_hz = recent / 10.0
        update_ms = elapsed_ms - render_ms
        PANE_PERF[self.pane_cfg.id] = {
            "title": self.pane_cfg.title or self.pane_cfg.id,
            "pty_updates": self._pty_updates,
            "render_count": self._render_count,
            "render_ms_last": elapsed_ms,
            "render_ms_avg": self._render_ms_total / self._render_count,
            "render_ms_max": self._render_ms_max,
            "s2r_ms": render_ms,
            "update_ms": update_ms,
            "render_hz": render_hz,
            "visible": self.visible,
        }
        # Periodic perf summary to log (every 30s)
        if now - self._last_log_ts >= 30.0:
            self._last_log_ts = now
            pty_bytes = self._runner.bytes_received if self._runner else 0
            self._log.info(
                "perf  render=%.1f/s  s2r=%.1fms  upd=%.1fms  tot=%.1fms  "
                "max=%.1fms  pty_updates=%d  pty_bytes=%d",
                render_hz,
                render_ms,
                update_ms,
                elapsed_ms,
                self._render_ms_max,
                self._pty_updates,
                pty_bytes,
            )

    def on_resize(self) -> None:
        if self.pane_cfg.restart_on_resize:
            self.run_worker(self.restart(), name=f"resize-restart-{self.pane_cfg.id}")
            return
        if self._runner:
            cols = max(10, self.content_size.width) or 80
            if self.pane_cfg.scrollable:
                self._runner.resize(_SCROLLABLE_ROWS, cols)
            else:
                self._runner.resize(max(4, self.content_size.height) or 24, cols)
