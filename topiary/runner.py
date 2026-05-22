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


def _pyte_color(color: str | None, bold: bool = False) -> str | None:
    """Convert a pyte color value to a Rich color string."""
    if not color or color == "default":
        return None
    # pyte 0.8 returns 256-color and truecolor as a 6-char lowercase hex string
    if len(color) == 6 and all(c in "0123456789abcdef" for c in color):
        return f"#{color}"
    name = _NAMED.get(color, color)
    return f"bright_{name}" if bold else name


def screen_to_rich(screen: pyte.Screen) -> Text:
    """Convert a pyte Screen buffer to a Rich Text object."""
    text = Text(no_wrap=True, overflow="crop")
    for y in range(screen.lines):
        line = screen.buffer[y]
        for x in range(screen.columns):
            char = line[x]
            if char.reverse:
                fg = _pyte_color(char.bg) or "default"
                bg = _pyte_color(char.fg, char.bold) or "default"
            else:
                fg = _pyte_color(char.fg, char.bold)
                bg = _pyte_color(char.bg)
            style = Style(
                color=fg,
                bgcolor=bg,
                bold=char.bold,
                italic=char.italics,
                underline=char.underscore,
                blink=char.blink,
                strike=char.strikethrough,
            )
            text.append(char.data or " ", style=style)
        text.append("\n")
    return text


class TerminalRunner:
    """Runs a shell command in a PTY and streams output through pyte."""

    def __init__(self, command: str, rows: int, cols: int) -> None:
        self.command = command
        self.rows = max(4, rows)
        self.cols = max(10, cols)
        self._screen = pyte.Screen(self.cols, self.rows)
        self._pyte_stream = pyte.ByteStream(self._screen)
        self._master_fd: int = -1
        self._proc: asyncio.subprocess.Process | None = None
        self._on_update: Callable | None = None
        self._done: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def exit_code(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def render(self) -> Text:
        return screen_to_rich(self._screen)

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
            cwd=os.path.expanduser(cwd) if cwd else None,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        self._loop.add_reader(self._master_fd, self._on_data)

    async def wait(self) -> int:
        """Block until the PTY is closed (process exited)."""
        if self._done:
            await self._done.wait()
        if self._proc:
            return await self._proc.wait()
        return -1

    async def stop(self) -> None:
        """Kill the process and close the PTY master."""
        self._eof()  # remove reader, set done event
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    self._proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        if self._master_fd >= 0:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = -1

    # ------------------------------------------------------------------ #
    # Resize                                                               #
    # ------------------------------------------------------------------ #

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = max(4, rows), max(10, cols)
        if rows == self.rows and cols == self.cols:
            return
        self.rows, self.cols = rows, cols
        self._screen.resize(rows, cols)
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
