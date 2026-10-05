"""Shared feedback for manually requested pane refreshes."""
from __future__ import annotations

import asyncio
import os
import signal

from ..log import log


class RefreshFeedback:
    _refreshing = False

    def _refresh_title(self, title: str) -> str:
        if self._refreshing:
            return f"{title} [italic yellow](refreshing...)[/italic yellow]"
        return title

    def _request_refresh(self, fallback) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._set_border_title(hover=self._hover_refresh)

        async def run_refresh():
            await self._manual_refresh(fallback)

        self.run_worker(
            run_refresh,
            name=f"manual-refresh-{self.pane_cfg.id}",
        )

    async def _manual_refresh(self, fallback) -> None:
        process = None
        try:
            if self.pane_cfg.refresh_command:
                process = await asyncio.create_subprocess_shell(
                    self.pane_cfg.refresh_command,
                    cwd=os.path.expanduser(self.pane_cfg.cwd) if self.pane_cfg.cwd else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                output, _ = await process.communicate()
                if process.returncode:
                    detail = output.decode(errors="replace").strip()
                    raise RuntimeError(
                        f"exit {process.returncode}: {detail}" if detail
                        else f"exit {process.returncode}"
                    )
            elif fallback is not None:
                await fallback()
        except (OSError, RuntimeError) as error:
            log.exception("manual refresh failed for %s", self.pane_cfg.id)
            self.notify(f"{self.pane_cfg.title or self.pane_cfg.id}: {error}",
                        title="Refresh failed", severity="error")
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            self._refreshing = False
            self._set_border_title(hover=self._hover_refresh)
