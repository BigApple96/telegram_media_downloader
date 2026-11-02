"""Exercise the real downloader/queue with fake Telegram I/O."""

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ruamel.yaml import YAML

import media_downloader as downloader
from module.app import DownloadStatus, TaskNode
from module.pyrogram_extension import reset_download_cache
from module.video_monitor import MonitorConfig, MonitoredChat

history_module = importlib.import_module("module.get_chat_history_v2")


class MonitorStartupTest(unittest.TestCase):
    def test_loads_config_section_and_ignores_previous_environment_settings(self):
        document = {
            "video_monitor": {"chats": [{"chat_id": -1001, "start_message_id": 130}]}
        }
        with patch.object(downloader.app, "load_config"), patch.object(
            downloader.app, "config", document
        ), patch.object(downloader.app, "monitor_config", MonitorConfig()), patch.dict(
            os.environ, {"TDL_MONITOR_CHATS": "-100999"}
        ):
            downloader._load_config()
            self.assertEqual(downloader.app.monitor_config.chats[0].chat_id, -1001)
            self.assertEqual(
                downloader.app.monitor_config.chats[0].start_message_id, 130
            )

    def test_monitor_startup_suppresses_batch_scan_and_shutdown_config_rewrite(self):
        document = {
            "video_monitor": {"chats": [{"chat_id": -1001, "start_message_id": 130}]}
        }
        config = MonitorConfig.from_config(document)
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        previous_loop = downloader.app.loop
        asyncio.set_event_loop(loop)
        self.addCleanup(asyncio.set_event_loop, previous_loop)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "config.yaml"
            YAML().dump(document, filename)
            with patch.object(downloader.app, "loop", loop), patch.object(
                downloader.app, "config_file", str(filename)
            ), patch.object(downloader.app, "config", document), patch.object(
                downloader.app, "monitor_config", config
            ), patch.object(
                downloader.app, "is_running", True
            ), patch.object(
                downloader.app, "bot_token", ""
            ), patch.object(
                downloader.app, "pre_run"
            ), patch.object(
                downloader.app, "update_config"
            ) as rewrite, patch.object(
                downloader, "HookClient"
            ), patch.object(
                downloader, "queue", asyncio.Queue()
            ), patch.object(
                downloader, "init_web"
            ), patch.object(
                downloader, "start_server", new=AsyncMock()
            ), patch.object(
                downloader, "stop_server", new=AsyncMock()
            ), patch.object(
                downloader, "set_max_concurrent_transmissions"
            ), patch.object(
                downloader, "download_all_chat", new=AsyncMock()
            ) as bulk, patch.object(
                downloader, "VideoMonitor"
            ) as monitor, patch.object(
                downloader,
                "_exec_loop",
                side_effect=lambda: loop.run_until_complete(asyncio.sleep(0)),
            ):
                monitor.return_value.run = AsyncMock()
                downloader.main()
                monitor.return_value.run.assert_awaited_once()
                bulk.assert_not_called()
                rewrite.assert_not_called()
                self.assertEqual(YAML().load(filename), document)


class MonitorIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_download_cache()

    async def test_idle_monitor_history_never_falls_back_to_old_messages(self):
        client = Mock()
        with patch.object(
            history_module, "get_chunk_v2", new=AsyncMock(return_value=[])
        ) as chunk:
            result = [
                item
                async for item in downloader.monitor_history(client, -1001, 129, 100)
            ]
        self.assertEqual(result, [])
        client.get_chat_history.assert_not_called()
        self.assertEqual(chunk.await_args.kwargs["from_message_id"], 130)
        self.assertTrue(chunk.await_args.kwargs["reverse"])

    async def test_queue_completion_waits_for_worker_and_reports_failure(self):
        message = SimpleNamespace(id=130, empty=False)
        for status, expected in (
            (DownloadStatus.SuccessDownload, True),
            (DownloadStatus.SkipDownload, True),
            (DownloadStatus.FailedDownload, False),
        ):
            with self.subTest(status=status), patch.object(
                downloader, "queue", asyncio.Queue()
            ), patch.object(downloader.app, "is_running", True), patch.object(
                downloader, "download_task", new=AsyncMock(return_value=status)
            ) as download:
                pending = asyncio.create_task(
                    downloader.download_monitored_video(-1001, message)
                )
                await asyncio.sleep(0)
                self.assertFalse(pending.done())
                worker = asyncio.create_task(downloader.worker(Mock()))
                try:
                    self.assertEqual(await asyncio.wait_for(pending, 2), expected)
                    node = download.await_args.args[2]
                    self.assertTrue(node.monitor_download)
                    self.assertIsNone(node.upload_telegram_chat_id)
                    self.assertIsNone(node.bot)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    async def test_worker_exception_completes_waiter_instead_of_hanging(self):
        with patch.object(downloader, "queue", asyncio.Queue()), patch.object(
            downloader.app, "is_running", True
        ), patch.object(
            downloader,
            "download_task",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ):
            worker = asyncio.create_task(downloader.worker(Mock()))
            try:
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    await asyncio.wait_for(
                        downloader.download_monitored_video(
                            -1001, SimpleNamespace(id=130, empty=False)
                        ),
                        2,
                    )
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_monitor_does_not_change_batch_progress_or_global_media_selection(
        self,
    ):
        node = TaskNode(-1001)
        node.monitor_download = True
        message = SimpleNamespace(id=130, text=None, media=True)
        with patch.object(downloader.app, "media_types", ["photo"]), patch.object(
            downloader.app, "file_formats", {"video": ["mp4"]}
        ), patch.object(
            downloader.app, "set_download_id"
        ) as batch_progress, patch.object(
            downloader,
            "download_media",
            new=AsyncMock(return_value=(DownloadStatus.SkipDownload, None)),
        ) as download, patch.object(
            downloader, "upload_telegram_chat", new=AsyncMock()
        ), patch.object(
            downloader, "report_bot_download_status", new=AsyncMock()
        ):
            status = await downloader.download_task(Mock(), message, node)
            self.assertEqual(status, DownloadStatus.SkipDownload)
            self.assertEqual(download.await_args.args[2], ["video", "document"])
            self.assertEqual(
                download.await_args.args[3], {"video": ["mp4"], "document": ["all"]}
            )
            self.assertEqual(downloader.app.media_types, ["photo"])
            batch_progress.assert_not_called()

    async def test_monitor_stays_alive_without_bot_or_batch_tasks(self):
        with patch.object(downloader.app, "bot_token", ""), patch.object(
            downloader.app, "chat_download_config", {}
        ), patch.object(downloader.app, "restart_program", False), patch.object(
            downloader.app,
            "monitor_config",
            MonitorConfig(chats=(MonitoredChat(-1001, 130),)),
        ):
            running = asyncio.create_task(downloader.run_until_all_task_finish())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(running.done())
                downloader.app.restart_program = True
                await asyncio.wait_for(running, 2)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    async def test_channel_paths_are_isolated_and_incomplete_files_are_redownloaded(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final = root / "downloads" / "video" / "130 - same.mp4"
            temporary = root / "temp" / "video" / "130 - same.mp4"
            message = SimpleNamespace(id=130, video=SimpleNamespace(file_size=5))

            async def write_file(_message, file_name, **_kwargs):
                path = Path(file_name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
                return str(path)

            client = SimpleNamespace(download_media=AsyncMock(side_effect=write_file))
            with patch.object(
                downloader.app, "save_path", str(root / "downloads")
            ), patch.object(
                downloader.app, "temp_save_path", str(root / "temp")
            ), patch.object(
                downloader, "fetch_message", new=AsyncMock(return_value=message)
            ), patch.object(
                downloader,
                "_get_media_meta",
                new=AsyncMock(return_value=(str(final), str(temporary), "mp4")),
            ), patch.object(
                downloader.asyncio, "sleep", new=AsyncMock()
            ):
                paths = []
                for chat_id in (-1001, -1002):
                    node = TaskNode(chat_id)
                    node.monitor_download = True
                    status, path = await downloader.download_media(
                        client, message, ["video"], {"video": ["all"]}, node
                    )
                    self.assertEqual(status, DownloadStatus.SuccessDownload)
                    self.assertEqual(Path(path).read_bytes(), b"video")
                    paths.append(Path(path))
                self.assertNotEqual(paths[0], paths[1])
                # A complete replay is skipped; a truncated file must be fetched again.
                status, _ = await downloader.download_media(
                    client, message, ["video"], {"video": ["all"]}, node
                )
                self.assertEqual(status, DownloadStatus.SkipDownload)
                paths[1].write_bytes(b"vi")
                status, _ = await downloader.download_media(
                    client, message, ["video"], {"video": ["all"]}, node
                )
                self.assertEqual(status, DownloadStatus.SuccessDownload)
                self.assertEqual(client.download_media.await_count, 3)
                self.assertEqual(paths[1].read_bytes(), b"video")

    async def test_fetch_error_does_not_poison_download_cache(self):
        node = TaskNode(-1001)
        message = SimpleNamespace(id=130, video=None)
        with patch.object(
            downloader,
            "fetch_message",
            new=AsyncMock(side_effect=[RuntimeError("disconnected"), message]),
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await downloader.download_media(Mock(), message, ["video"], {}, node)
            status, _ = await downloader.download_media(
                Mock(), message, ["video"], {}, node
            )
            self.assertEqual(status, DownloadStatus.SkipDownload)
            self.assertEqual(fetch.await_count, 2)


if __name__ == "__main__":
    unittest.main()
