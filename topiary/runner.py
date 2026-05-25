"""TerminalRunner: run a command in a PTY and maintain a pyte screen buffer."""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
from typing import Callable

import pyte
from rich.style import Style
from rich.text import Text

from .log import log

# pyte's basic-8 color names → Rich color names
_NAMED = {
    "black": "black",
    "red": "red",
    "green": "green",
    "brown": "yellow",   # ANSI "brown" = yellow
    "blue": "blue",
    "magenta": "magenta",
    "cyan": "cyan",
    "white": "white",
}

# Sentinel for a fully-default style (no color, no attributes)
_DEFAULT_KEY = (None, None, False, False, False, False, False)


def _pyte_color(color: str | None, bold: bool = False) -> str | None:
    """Convert a pyte color value to a Rich color string."""
    if not color or color == "default":
        return None
    if len(color) == 6 and all(c in "0123456789abcdef" for c in color):
        return f"#{color}"
    name = _NAMED.get(color, color)
    return f"bright_{name}" if bold else name


def _char_key(char) -> tuple:
    """Return a hashable style key for a pyte Char — cheap to compute."""
    if char.reverse:
        fg = _pyte_color(char.bg) or "default"
        bg = _pyte_color(char.fg, char.bold) or "default"
    else:
        fg = _pyte_color(char.fg, char.bold)
        bg = _pyte_color(char.bg)
    return (fg, bg, char.bold, char.italics, char.underscore, char.blink, char.strikethrough)


def _key_to_style(key: tuple) -> Style | None:
    """Build a Rich Style from a key tuple; returns None for the default style."""
    if key == _DEFAULT_KEY:
        return None
    fg, bg, bold, italics, underscore, blink, strike = key
    return Style(
        color=fg, bgcolor=bg,
        bold=bold, italic=italics,
        underline=underscore, blink=blink, strike=strike,
    )


def _render_row(line: dict, cols: int, default_key: tuple, style_cache: dict) -> Text:
    """Render a single pyte screen row to a Rich Text (no trailing newline)."""
    row_text = Text(no_wrap=True, overflow="crop")
    if not line:
        row_text.append(" " * cols)
        return row_text

    written = sorted(line.items())
    prev_x = 0
    run_key: tuple | None = None
    run_chars: list[str] = []

    def _flush() -> None:
        if not run_chars:
            return
        style = style_cache.get(run_key)
        if style is None and run_key not in style_cache:
            style = _key_to_style(run_key)   # type: ignore[arg-type]
            style_cache[run_key] = style      # type: ignore[index]
        row_text.append("".join(run_chars), style=style)
        run_chars.clear()

    for x, char in written:
        if x > prev_x:
            gap = x - prev_x
            if run_key == default_key:
                run_chars.append(" " * gap)
            else:
                _flush()
                run_key = default_key
                run_chars.append(" " * gap)
        key = _char_key(char)
        data = char.data or " "
        if key == run_key:
            run_chars.append(data)
        else:
            _flush()
            run_key = key
            run_chars.append(data)
        prev_x = x + 1

    if prev_x < cols:
        gap = cols - prev_x
        if run_key == default_key:
            run_chars.append(" " * gap)
        else:
            _flush()
            run_key = default_key
            run_chars.append(" " * gap)

    _flush()
    return row_text


def screen_to_rich(
    screen: pyte.Screen,
    trim_trailing: bool = False,
    line_cache: list | None = None,
) -> Text:
    """Convert a pyte Screen buffer to a Rich Text object.

    If line_cache is provided (a list of [Text, dirty_bool] per row),
    only dirty rows are re-rendered; clean rows reuse the cached Text.
    Call screen.dirty.clear() AFTER this function to reset pyte's tracking.
    """
    default_char = pyte.screens.Char(" ")
    default_key = _char_key(default_char)
    style_cache: dict[tuple, Style | None] = {}

    last_content_row = screen.lines - 1
    if trim_trailing:
        for y in range(screen.lines - 1, -1, -1):
            row_map = screen.buffer[y]
            if row_map and any(c.data.strip() for c in row_map.values()):
                last_content_row = y
                break
        else:
            last_content_row = 0

    # Ensure cache is right size
    if line_cache is not None:
        while len(line_cache) < screen.lines:
            line_cache.append(None)

    dirty_rows = screen.dirty  # set of row indices modified since last clear

    text = Text(no_wrap=True, overflow="crop")
    for y in range(last_content_row + 1):
        if line_cache is not None and not trim_trailing:
            cached = line_cache[y]
            if cached is not None and y not in dirty_rows:
                # Reuse cached row
                text.append_text(cached)
            else:
                row_text = _render_row(screen.buffer[y], screen.columns, default_key, style_cache)
                line_cache[y] = row_text
                text.append_text(row_text)
        else:
            text.append_text(_render_row(screen.buffer[y], screen.columns, default_key, style_cache))
        text.append("\n")

    if line_cache is not None:
        screen.dirty.clear()

    return text


class TerminalRunner:
    """Runs a shell command in a PTY and streams output through pyte."""

    def __init__(self, command: str, rows: int, cols: int) -> None:
        self.command = command
        self.rows = max(4, rows)
        self.cols = max(10, cols)
        self._screen = pyte.Screen(self.cols, self.rows)
        self._pyte_stream = pyte.ByteStream(self._screen)
        self._line_cache: list = []   # per-row cached Rich Text; invalidated by screen.dirty
        self._master_fd: int = -1
        self._proc: asyncio.subprocess.Process | None = None
        self._on_update: Callable | None = None
        self._done: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.bytes_received: int = 0   # total PTY bytes; read by CommandPane for logging

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def render(self, trim_trailing: bool = False) -> Text:
        return screen_to_rich(
            self._screen,
            trim_trailing=trim_trailing,
            line_cache=None if trim_trailing else self._line_cache,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self, on_update: Callable | None = None, cwd: str | None = None) -> None:
        """Launch the command in a PTY and start reading output."""
        self._on_update = on_update
        self._done = asyncio.Event()
        self._loop = asyncio.get_running_loop()

        self._master_fd, slave_fd = pty.openpty()
        self._set_winsize(slave_fd)

        env = {**os.environ, "TERM": "xterm-256color"}
        self._proc = await asyncio.create_subprocess_shell(
            self.command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=os.path.expanduser(os.path.expandvars(cwd)) if cwd else None,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._loop.add_reader(self._master_fd, self._on_data)
        log.debug("runner start  pid=%d  rows=%d cols=%d  cmd=%.80s",
                  self._proc.pid, self.rows, self.cols, self.command)

    async def wait(self) -> int:
        """Block until the PTY is closed (process exited)."""
        if self._done:
            await self._done.wait()
        if self._proc:
            return await self._proc.wait()
        return -1

    def kill_sync(self) -> None:
        """Synchronously kill the process group and clean up all resources.

        Uses no await so CancelledError cannot interrupt it.
        Safe to call from on_unmount, __del__, or signal handlers.
        """
        # 1. Remove asyncio fd reader before closing the fd
        loop = self._loop
        if loop is not None and self._master_fd >= 0:
            try:
                loop.remove_reader(self._master_fd)
            except Exception:
                pass

        # 2. Unblock any pending wait()
        if self._done and not self._done.is_set():
            self._done.set()

        # 3. Kill the process group (start_new_session makes child a group leader)
        if self._proc is not None and self._proc.returncode is None:
            pid = self._proc.pid
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except (ProcessLookupError, OSError):
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, OSError):
                        pass

        # 4. Close PTY master fd
        if self._master_fd >= 0:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = -1

        # 5. Break reference cycle: runner._on_update -> pane._mark_dirty -> runner
        self._on_update = None

    async def stop(self) -> None:
        """kill_sync() then best-effort await process reap."""
        self.kill_sync()
        if self._proc is not None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
                pass

    # ------------------------------------------------------------------ #
    # Resize                                                               #
    # ------------------------------------------------------------------ #

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = max(4, rows), max(10, cols)
        if rows == self.rows and cols == self.cols:
            return
        self.rows, self.cols = rows, cols
        self._screen.resize(rows, cols)
        self._line_cache.clear()   # dimensions changed; cached rows are stale
        if self._master_fd >= 0:
            try:
                self._set_winsize(self._master_fd)
            except OSError:
                pass
        if self.is_running:
            try:
                self._proc.send_signal(signal.SIGWINCH)  # type: ignore[union-attr]
            except (ProcessLookupError, OSError):
                pass

    def _set_winsize(self, fd: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))

    # ------------------------------------------------------------------ #
    # Internal PTY reading                                                 #
    # ------------------------------------------------------------------ #

    def _on_data(self) -> None:
        try:
            data = os.read(self._master_fd, 16384)
            if data:
                self._pyte_stream.feed(data)
                self.bytes_received += len(data)
                if self._on_update:
                    self._on_update()
            else:
                self._eof()
        except OSError:
            self._eof()

    def _eof(self) -> None:
        if self._loop and self._master_fd >= 0:
            try:
                self._loop.remove_reader(self._master_fd)
            except Exception:
                pass
        if self._done and not self._done.is_set():
            self._done.set()
