"""CommandPane: runs any shell command in a PTY and displays its output."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime
from pathlib import Path
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
        start_immediately: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.pane_cfg = pane_cfg
        self._show_border = show_border
        self._refresh_rate_hz = max(1.0, refresh_rate_hz)
        # False for tabs that aren't the initially-selected one -- TabsPane
        # starts/stops these explicitly via activate()/deactivate() so only
        # the visible tab's process is ever running.
        self._active: bool = start_immediately
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
        self._skip_count: int = 0           # _maybe_refresh no-ops (not dirty)
        self._throttle_count: int = 0       # _maybe_refresh no-ops (rate-capped)
        self._last_render_ts: float = 0.0   # monotonic time of last actual render
        self._skip_ts: deque[float] = deque(maxlen=200)  # timestamps of recent skips
        self._bytes_sample_ts: float = 0.0  # monotonic time of last bytes/s sample
        self._bytes_sample_start: int = 0   # bytes_received at last sample
        self._bytes_per_sec: float = 0.0    # rolling bytes/s estimate
        self._log = log.getChild(f"pane.{pane_cfg.id}")
        self._last_log_ts: float = 0.0   # monotonic time of last periodic perf log
        self._hover_refresh: bool = False  # true when mouse is over top border row
        self._schedule_time: dtime | None = self._parse_schedule(pane_cfg.schedule)
        self._last_scheduled_date: date | None = None  # date on which schedule last fired

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
        render_hz = self._refresh_rate_hz
        if self.pane_cfg.interactive and self.pane_cfg.max_render_hz is None:
            render_hz = max(render_hz, 30.0)
        self.set_interval(1 / render_hz, self._maybe_refresh)
        if self._active:
            self.run_worker(self._run_loop, exclusive=True, name=f"cmd-{self.pane_cfg.id}")

    def activate(self) -> None:
        """Start (or resume) this pane's command. Called by TabsPane when its
        tab becomes the selected one -- runs immediately rather than waiting
        for whatever refresh/watch interval the pane is configured with."""
        if self._active:
            return
        self._active = True
        self.run_worker(self._run_loop, exclusive=True, name=f"cmd-{self.pane_cfg.id}")

    def deactivate(self) -> None:
        """Stop this pane's command. Called by TabsPane when its tab is no
        longer selected, so background tabs don't burn CPU running commands
        nobody is looking at."""
        if not self._active:
            return
        self._active = False
        if self._runner is not None:
            self._runner.kill_sync()
            self._runner = None
        worker_name = f"cmd-{self.pane_cfg.id}"
        for worker in self.workers:
            if worker.name == worker_name and not worker.is_finished:
                worker.cancel()

    def _has_refresh_icon(self) -> bool:
        """True if this pane shows the refresh icon (timer, schedule, or refresh_command)."""
        return (self.pane_cfg.refresh > 0
                or bool(self.pane_cfg.schedule)
                or bool(self.pane_cfg.refresh_command))

    def _set_border_title(self, hover: bool = False) -> None:
        if not self._show_border:
            return
        title = self.pane_cfg.title or self.pane_cfg.id
        if self._has_refresh_icon():
            icon = "[bold yellow]⟳[/bold yellow]" if hover else "⟳"
            self.border_title = f"{icon} {title}"
        else:
            self.border_title = title

    def on_mouse_move(self, event: object) -> None:
        if not self._has_refresh_icon():
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

    def _fire_refresh_command(self) -> None:
        """Run refresh_command as a fire-and-forget subprocess."""
        import subprocess
        cmd = self.pane_cfg.refresh_command
        self._log.info("firing refresh_command: %s", cmd)
        try:
            subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            self._log.error("refresh_command failed: %s", e)

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
        self._skip_count = 0
        self._throttle_count = 0
        self._last_render_ts = 0.0
        self._skip_ts.clear()
        self._bytes_sample_ts = 0.0
        self._bytes_sample_start = 0
        self._bytes_per_sec = 0.0
        
        # Wait for old worker/process to fully exit
        await asyncio.sleep(0.3)
        
        # Clear restart message before starting
        output_widget.update("")
        
        # Start fresh worker
        self.run_worker(self._run_loop, exclusive=True, name=worker_name)
        self._log.info("restart worker launched")

    def on_button_pressed(self, event: object) -> None:
        pass  # no longer used — kept so subclasses aren't broken

    def on_click(self, event: Click) -> None:
        """Clicking the ⟳ triggers a refresh; other clicks toggle true focus."""
        if self._has_refresh_icon() and event.y == 0 and event.x <= 3:
            event.stop()
            if self.pane_cfg.refresh_command:
                self._fire_refresh_command()
            else:
                self.run_worker(self._run_once(), name=f"force-refresh-{self.pane_cfg.id}")
            return

        # Only cycle focus for interactive panes.
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

    # ------------------------------------------------------------------ #
    # Run loop                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_schedule(value: str | None) -> dtime | None:
        """Parse "HH:MM" schedule string → time object, or None if unset/invalid."""
        if not value:
            return None
        try:
            h, m = value.strip().split(":")
            return dtime(int(h), int(m))
        except (ValueError, TypeError):
            log.warning("invalid schedule %r — expected HH:MM", value)
            return None

    def _schedule_due(self) -> bool:
        """Return True if the scheduled time has arrived and hasn't fired today yet."""
        if self._schedule_time is None:
            return False
        now = datetime.now()
        today = now.date()
        if self._last_scheduled_date == today:
            return False
        if now.time() >= self._schedule_time:
            self._last_scheduled_date = today
            return True
        return False

    async def _run_loop(self) -> None:
        """Run the command, then re-run on inotify events and/or a refresh timer.

        Modes (combinable):
          watch only    — re-run whenever a watched path changes
          refresh only  — re-run every N seconds
          watch+refresh — re-run on watch event OR after N seconds, whichever first
          neither       — run once
        """
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
            has_refresh  = self.pane_cfg.refresh > 0
            has_watch    = bool(self.pane_cfg.watch)
            has_schedule = self._schedule_time is not None
            if not has_refresh and not has_watch and not has_schedule:
                return  # run-once mode

            trigger = asyncio.Event()
            if has_watch:
                self._start_inotify_watcher(trigger)

            # When schedule is the only wake-up mechanism, use a 30s poll so
            # we catch the target minute within ~30 seconds.
            _SCHEDULE_POLL = 30

            while True:
                if has_refresh and has_watch:
                    try:
                        await asyncio.wait_for(trigger.wait(), timeout=self.pane_cfg.refresh)
                    except asyncio.TimeoutError:
                        pass
                    trigger.clear()
                elif has_refresh:
                    await asyncio.sleep(self.pane_cfg.refresh)
                elif has_watch:
                    await trigger.wait()
                    trigger.clear()
                else:  # schedule only — poll every 30s
                    await asyncio.sleep(_SCHEDULE_POLL)

                if self._schedule_due():
                    await self._run_once()
                elif not has_schedule:
                    await self._run_once()
        except Exception:
            self._log.exception("unhandled error in _run_loop")
        finally:
            if self._runner is not None:
                self._runner.kill_sync()
                self._runner = None

    def _start_inotify_watcher(self, trigger: asyncio.Event) -> None:
        """Spawn a daemon thread that fires *trigger* whenever any watched path changes."""
        loop = asyncio.get_event_loop()
        paths = [Path(p).expanduser().resolve() for p in self.pane_cfg.watch]

        def _thread() -> None:
            try:
                import inotify_simple  # type: ignore
                flags = (
                    inotify_simple.flags.CLOSE_WRITE
                    | inotify_simple.flags.MOVED_TO
                    | inotify_simple.flags.MODIFY
                )
                inotify = inotify_simple.INotify()
                # Map watch-descriptor → expected filename (None = watch whole dir)
                wd_map: dict[int, str | None] = {}
                for p in paths:
                    if not p.parent.exists():
                        continue
                    watch_dir = p.parent if p.is_file() or not p.is_dir() else p
                    expected  = p.name if not p.is_dir() else None
                    wd = inotify.add_watch(str(watch_dir), flags)
                    wd_map[wd] = expected
                if not wd_map:
                    return
                while True:
                    events = inotify.read(timeout=30_000)
                    for event in events:
                        expected = wd_map.get(event.wd)
                        name     = getattr(event, "name", None) or ""
                        if expected is None or name == expected:
                            loop.call_soon_threadsafe(trigger.set)
                            break  # debounce: one trigger per read batch
            except Exception:
                self._log.warning("inotify watcher failed; falling back to refresh-only")

        threading.Thread(target=_thread, daemon=True, name=f"inotify-{self.pane_cfg.id}").start()

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
                        text = runner.render(
                            trim_trailing=self.pane_cfg.scrollable,
                            render_cursor=self.pane_cfg.render_cursor,
                        )
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
            self._skip_count += 1
            self._skip_ts.append(time.monotonic())
            return

        # Per-pane render rate cap: if max_render_hz is set, skip renders that arrive
        # faster than that rate (dirty flag stays True so the next tick catches it).
        # Interactive panes get a dynamic default cap:
        # - active/passthrough pane -> up to 30 Hz (or global rate if higher)
        # - inactive pane           -> global refresh_rate_hz
        max_hz = self.pane_cfg.max_render_hz
        if max_hz is None and self.pane_cfg.interactive:
            if self.has_class("pane-active"):
                max_hz = max(30.0, self._refresh_rate_hz)
            else:
                max_hz = self._refresh_rate_hz
        if max_hz is not None and max_hz > 0:
            now = time.monotonic()
            if now - self._last_render_ts < 1.0 / max_hz:
                self._throttle_count += 1
                return

        self._dirty = False
        self._last_render_ts = time.monotonic()
        t0 = time.perf_counter()
        try:
            rich_text = self._runner.render(
                trim_trailing=self.pane_cfg.scrollable,
                render_cursor=self.pane_cfg.render_cursor,
            )
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
                if self.pane_cfg.autoscroll or (self.pane_cfg.follow and at_bottom):
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
        skip_recent = sum(1 for t in self._skip_ts if t >= cutoff)
        skip_hz = skip_recent / 10.0
        update_ms = elapsed_ms - render_ms

        # Update bytes/sec estimate every 2s
        if self._runner:
            current_bytes = self._runner.bytes_received
            if self._bytes_sample_ts == 0.0:
                self._bytes_sample_ts = now
                self._bytes_sample_start = current_bytes
            elif now - self._bytes_sample_ts >= 2.0:
                delta = current_bytes - self._bytes_sample_start
                elapsed = now - self._bytes_sample_ts
                self._bytes_per_sec = delta / elapsed if elapsed > 0 else 0.0
                self._bytes_sample_ts = now
                self._bytes_sample_start = current_bytes

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
            "skip_hz": skip_hz,
            "throttle_count": self._throttle_count,
            "bytes_per_sec": self._bytes_per_sec,
            "visible": self.visible,
        }
        # Periodic perf summary to log (every 30s)
        if now - self._last_log_ts >= 30.0:
            self._last_log_ts = now
            pty_bytes = self._runner.bytes_received if self._runner else 0
            self._log.info(
                "perf  render=%.1f/s  skip=%.1f/s  throttle=%d  "
                "s2r=%.1fms  upd=%.1fms  tot=%.1fms  max=%.1fms  "
                "pty_updates=%d  pty_bytes=%d  bytes/s=%.0f",
                render_hz,
                skip_hz,
                self._throttle_count,
                render_ms,
                update_ms,
                elapsed_ms,
                self._render_ms_max,
                self._pty_updates,
                pty_bytes,
                self._bytes_per_sec,
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
