"""Listener configuration and durable progress, without Telegram access."""

import asyncio
import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ruamel.yaml import YAML

from module.video_monitor import MonitorConfig, MonitorState, VideoMonitor, parse_chat


def message(message_id, kind="video"):
    return SimpleNamespace(
        id=message_id,
        empty=kind == "deleted",
        video=SimpleNamespace(file_size=10) if kind == "video" else None,
        document=(
            SimpleNamespace(
                mime_type="video/mp4" if kind == "video_document" else "application/pdf"
            )
            if kind in ("video_document", "document")
            else None
        ),
    )


def config_document(start=130):
    return {
        "video_monitor": {
            "enabled": True,
            "interval": 60,
            "chats": [
                {"chat_id": -1001, "start_message_id": start},
                {"chat_id": -1002, "start_message_id": start},
            ],
        }
    }


class FakeClient:
    def __init__(self):
        self.messages = {}

    async def get_chat(self, reference):
        return SimpleNamespace(id=int(reference))

    async def history(self, chat_id, after_id, limit):
        for item in sorted(self.messages.get(chat_id, []), key=lambda m: m.id):
            if item.id > after_id:
                yield item
                limit -= 1
                if limit == 0:
                    return


class MonitorConfigTest(unittest.TestCase):
    def test_ids_usernames_links_and_independent_start_positions(self):
        config = MonitorConfig.from_config(
            {
                "video_monitor": {
                    "interval": 30,
                    "chats": [
                        {
                            "chat_id": "https://t.me/c/4342657661/129",
                            "start_message_id": 130,
                        },
                        {"chat_id": "@Example_Channel", "start_message_id": 8},
                    ],
                }
            }
        )
        self.assertEqual(
            [chat.chat_id for chat in config.chats], [-1004342657661, "example_channel"]
        )
        self.assertEqual([chat.cursor for chat in config.chats], [129, 7])
        self.assertEqual(config.interval, 30)
        self.assertTrue(config.enabled)

    def test_saved_progress_takes_precedence_over_start(self):
        document = config_document()
        document["video_monitor"]["chats"][0]["last_read_message_id"] = 160
        self.assertEqual(MonitorConfig.from_config(document).chats[0].cursor, 160)

    def test_absent_disabled_and_empty_monitor_preserve_original_mode(self):
        for document in (
            {},
            {"video_monitor": {"enabled": False}},
            {"video_monitor": {"chats": []}},
        ):
            self.assertFalse(MonitorConfig.from_config(document).enabled)

    def test_invalid_settings_fail_instead_of_selecting_unintended_history(self):
        for key, values in (
            ("start_message_id", [None, 0, -1, True, "130"]),
            ("last_read_message_id", [None, -1, True, "130"]),
        ):
            for value in values:
                document = config_document()
                document["video_monitor"]["chats"][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    MonitorConfig.from_config(document)
        for section in (
            None,
            [],
            {"enabled": "true"},
            {"interval": 0},
            {"interval": True},
            {"chats": {}},
        ):
            with self.subTest(section=section), self.assertRaises(ValueError):
                MonitorConfig.from_config({"video_monitor": section})

    def test_duplicate_chat_rejected_even_when_written_as_a_link(self):
        document = config_document()
        document["video_monitor"]["chats"] = [
            {"chat_id": -1004342657661, "start_message_id": 130},
            {"chat_id": "https://t.me/c/4342657661/129", "start_message_id": 150},
        ]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            MonitorConfig.from_config(document)

    def test_invites_and_web_client_urls_are_not_chat_ids(self):
        for value in (
            "https://t.me/+private_invite",
            "https://t.me/joinchat/private",
            "https://web.telegram.org/a/#-123",
            "https://t.me/c",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_chat(value)


class MonitorStateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.filename = Path(self.directory.name) / "config.yaml"
        self.document = config_document()
        YAML().dump(self.document, self.filename)

    def test_progress_is_saved_to_config_preserving_comments_and_other_settings(self):
        original = self.filename.read_text(encoding="utf-8")
        self.filename.write_text(
            "# keep this comment\napi_hash: placeholder_only\nchat: []\n" + original,
            encoding="utf-8",
        )
        document = YAML().load(self.filename)
        state = MonitorState(str(self.filename), document)
        state.save(-1001, 150)
        state.save(-1002, 180)
        saved = YAML().load(self.filename)
        self.assertIn("# keep this comment", self.filename.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_hash"], "placeholder_only")
        self.assertEqual(saved["chat"], [])
        self.assertEqual(saved["video_monitor"]["chats"][0]["start_message_id"], 130)
        self.assertEqual(
            saved["video_monitor"]["chats"][0]["last_read_message_id"], 150
        )
        self.assertEqual(
            saved["video_monitor"]["chats"][1]["last_read_message_id"], 180
        )
        self.assertEqual(
            document["video_monitor"]["chats"][0]["last_read_message_id"], 150
        )

    def test_atomic_write_failure_preserves_disk_and_memory(self):
        state = MonitorState(str(self.filename), self.document)
        original = self.filename.read_bytes()
        with patch(
            "module.video_monitor.os.replace", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                state.save(-1001, 130)
        self.assertEqual(state.get(-1001), 129)
        self.assertNotIn(
            "last_read_message_id", self.document["video_monitor"]["chats"][0]
        )
        self.assertEqual(self.filename.read_bytes(), original)
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)

    def test_file_bind_mount_error_is_actionable_and_preserves_config(self):
        state = MonitorState(str(self.filename), self.document)
        original = self.filename.read_bytes()
        with patch(
            "module.video_monitor.os.replace", side_effect=OSError(errno.EBUSY, "busy")
        ):
            with self.assertRaisesRegex(OSError, "Mount the config directory"):
                state.save(-1001, 130)
        self.assertEqual(self.filename.read_bytes(), original)


class VideoMonitorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.filename = Path(self.directory.name) / "config.yaml"
        self.client = FakeClient()
        self.downloads = []
        self.outcomes = {}
        self.write_config()

    def write_config(self, start=130):
        YAML().dump(config_document(start), self.filename)

    def saved_cursor(self, chat_id=-1001):
        return MonitorState(str(self.filename), YAML().load(self.filename)).get(chat_id)

    async def download(self, chat_id, item):
        self.downloads.append((chat_id, item.id))
        result = self.outcomes.get((chat_id, item.id), True)
        if isinstance(result, Exception):
            raise result
        return result

    def make_monitor(self, download=None):
        document = YAML().load(self.filename)
        config = MonitorConfig.from_config(document)
        return VideoMonitor(
            config,
            self.client,
            self.client.history,
            download or self.download,
            MonitorState(str(self.filename), document),
        )

    async def test_start_includes_requested_message_and_never_fetches_older_video(self):
        self.client.messages[-1001] = [message(128), message(129), message(130)]
        monitor = self.make_monitor()
        await monitor.poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 130)])
        self.assertEqual(self.saved_cursor(), 130)
        self.client.messages[-1001].extend([message(131), message(132, "text")])
        await monitor.poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 130), (-1001, 131)])
        self.assertEqual(self.saved_cursor(), 132)

    async def test_restart_skips_completed_downloads_and_drains_multiple_pages(self):
        self.client.messages[-1001] = [message(129), message(130)]
        await self.make_monitor().poll_chat(-1001)
        self.client.messages[-1001].extend(message(i) for i in range(131, 336))
        restored = self.make_monitor()
        pages = 1
        while await restored.poll_chat(-1001):
            pages += 1
        self.assertEqual(pages, 3)
        self.assertEqual(self.downloads, [(-1001, i) for i in range(130, 336)])
        self.assertEqual(self.saved_cursor(), 335)

    async def test_failure_does_not_commit_past_failed_message(self):
        self.write_config(1)
        self.client.messages[-1001] = [message(1, "text"), message(2), message(3)]
        self.outcomes[(-1001, 2)] = False
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.saved_cursor(), 1)
        self.outcomes[(-1001, 2)] = True
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.saved_cursor(), 3)
        self.assertIn((-1001, 2), self.downloads[2:])

    async def test_exception_is_retryable_and_channels_have_separate_progress(self):
        self.client.messages = {-1001: [message(130)], -1002: [message(130)]}
        self.outcomes[(-1001, 130)] = RuntimeError("network interrupted")
        monitor = self.make_monitor()
        await asyncio.gather(monitor.poll_chat(-1001), monitor.poll_chat(-1002))
        self.assertEqual(self.saved_cursor(-1001), 129)
        self.assertEqual(self.saved_cursor(-1002), 130)

    async def test_only_videos_and_video_documents_are_enqueued(self):
        self.write_config(1)
        self.client.messages[-1001] = [
            message(1),
            message(2, "text"),
            message(3, "document"),
            message(4, "video_document"),
            message(5, "deleted"),
        ]
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 1), (-1001, 4)])
        self.assertEqual(self.saved_cursor(), 5)

    async def test_empty_channel_waits_for_configured_start(self):
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.saved_cursor(), 129)
        self.client.messages[-1001] = [message(129)]
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [])
        self.client.messages[-1001].append(message(130))
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 130)])

    async def test_completion_is_saved_while_next_download_is_running(self):
        self.client.messages[-1001] = [message(130), message(131)]
        started = asyncio.Event()

        async def partly_blocked(_chat_id, item):
            if item.id == 131:
                started.set()
                await asyncio.Event().wait()
            return True

        task = asyncio.create_task(self.make_monitor(partly_blocked).poll_chat(-1001))
        try:
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(self.saved_cursor(), 130)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.saved_cursor(), 130)
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 131)])

    async def test_no_download_if_config_progress_cannot_be_saved(self):
        self.client.messages[-1001] = [message(130)]
        with patch(
            "module.video_monitor.os.replace", side_effect=PermissionError("read only")
        ):
            with self.assertRaises(PermissionError):
                await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [])

    async def test_old_or_duplicate_history_results_cannot_regress_cursor(self):
        monitor = self.make_monitor()

        async def unordered_history(*_args):
            for item in [message(131), message(128), message(130), message(130)]:
                yield item

        monitor.history = unordered_history
        await monitor.poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 130), (-1001, 131)])
        self.assertEqual(self.saved_cursor(), 131)

    async def test_saved_progress_is_authoritative_when_start_is_edited(self):
        document = YAML().load(self.filename)
        document["video_monitor"]["chats"][0]["start_message_id"] = 1
        document["video_monitor"]["chats"][0]["last_read_message_id"] = 129
        YAML().dump(document, self.filename)
        self.client.messages[-1001] = [message(128), message(129), message(130)]
        await self.make_monitor().poll_chat(-1001)
        self.assertEqual(self.downloads, [(-1001, 130)])


if __name__ == "__main__":
    unittest.main()
