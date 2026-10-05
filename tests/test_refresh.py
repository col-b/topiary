import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from textual.app import App

from topiary.config import PaneConfig
from topiary.panes.command import CommandPane
from topiary.panes.tabs import TabsPane


class Harness(App):
    def __init__(self, pane):
        super().__init__()
        self.pane = pane

    def compose(self):
        yield self.pane


def make_pane(kind, command=None):
    cfg = PaneConfig(id="sample", title="sample", refresh=15,
                     refresh_command=command)
    if kind is TabsPane:
        cfg.type = "tabs"
        cfg.tabs = [PaneConfig(id="inner", title="inner", command="true")]
    return kind(cfg, id="sample", start_immediately=False)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_waits_and_ignores_repeat_clicks(self):
        for kind in (CommandPane, TabsPane):
            with self.subTest(kind=kind):
                pane = make_pane(kind, "refresh-backend")
                done = asyncio.Event()

                class Process:
                    returncode = None

                    async def communicate(self):
                        await done.wait()
                        self.returncode = 0
                        return b"", b""

                with patch("topiary.panes.refresh.asyncio.create_subprocess_shell",
                           new_callable=AsyncMock, return_value=Process()) as spawn:
                    async with Harness(pane).run_test() as pilot:
                        await pilot.click("#sample", offset=(1, 0))
                        self.assertTrue(pane._refreshing)
                        self.assertIn("(refreshing...)", pane.border_title)
                        await pilot.click("#sample", offset=(1, 0))
                        self.assertEqual(spawn.await_count, 1)
                        pane._set_border_title(hover=False)
                        self.assertIn("(refreshing...)", pane.border_title)
                        done.set()
                        await pilot.pause()
                        self.assertFalse(pane._refreshing)
                        self.assertNotIn("(refreshing...)", pane.border_title)

    async def test_button_without_refresh_command_waits_for_render(self):
        for kind in (CommandPane, TabsPane):
            pane = make_pane(kind)
            async with Harness(pane).run_test() as pilot:
                target = pane if kind is CommandPane else pane._get_active_inner_pane()
                done = asyncio.Event()
                target._run_once = AsyncMock(side_effect=done.wait)
                await pilot.click("#sample", offset=(1, 0))
                self.assertTrue(pane._refreshing)
                target._run_once.assert_awaited_once()
                done.set()
                await pilot.pause()
                self.assertFalse(pane._refreshing)

    async def test_failed_command_reports_error_and_clears_title(self):
        for kind in (CommandPane, TabsPane):
            pane = make_pane(kind, "broken")
            process = AsyncMock()
            process.returncode = 7
            process.communicate.return_value = (b"backend failed", b"")
            with patch("topiary.panes.refresh.asyncio.create_subprocess_shell",
                       new_callable=AsyncMock, return_value=process):
                async with Harness(pane).run_test() as pilot:
                    with patch.object(pane, "notify") as notify:
                        await pilot.click("#sample", offset=(1, 0))
                        await pilot.pause()
                        self.assertFalse(pane._refreshing)
                        self.assertNotIn("(refreshing...)", pane.border_title)
                        self.assertIn("backend failed", notify.call_args.args[0])
                        self.assertEqual(notify.call_args.kwargs["severity"], "error")

    async def test_cancelled_refresh_terminates_backend(self):
        pane = make_pane(CommandPane, "exec sleep 30")
        spawn = asyncio.create_subprocess_shell
        started = asyncio.Future()

        async def capture_process(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            started.set_result(process)
            return process

        with patch("topiary.panes.refresh.asyncio.create_subprocess_shell",
                   side_effect=capture_process):
            async with Harness(pane).run_test() as pilot:
                pane._refreshing = True
                task = asyncio.create_task(pane._manual_refresh(None))
                process = await asyncio.wait_for(started, timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(process.returncode)
                self.assertFalse(pane._refreshing)


if __name__ == "__main__":
    unittest.main()
